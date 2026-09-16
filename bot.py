"""
bot.py — Telegram bot wiring using pyTelegramBotAPI (telebot).

Reads every message in the group, parses it, updates match state,
detects signals, sends alerts, and handles slash commands.

Environment variables (from .env):
    BOT_TOKEN   — Telegram bot token from BotFather
    GROUP_ID    — Target group chat ID (negative integer)
"""

import logging
import os
import traceback
from datetime import date
from typing import Dict

import telebot
from dotenv import load_dotenv

import db
from engine import detect_signal
from parser import parse_line
from state import MatchState

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID  = int(os.getenv("GROUP_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
db.init_db()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Per-chat state store
# ---------------------------------------------------------------------------

# chat_id → MatchState
_states: Dict[int, MatchState] = {}


def get_state(chat_id: int) -> MatchState:
    """Return existing state for chat or create a new one."""
    if chat_id not in _states:
        _states[chat_id] = MatchState()
        logger.info("New MatchState created for chat %s", chat_id)
    return _states[chat_id]


# ---------------------------------------------------------------------------
# Message formatting helpers
# ---------------------------------------------------------------------------

def _fmt_alert(signal: dict, alert_id: int) -> str:
    """Format a signal dict as a Markdown alert message."""
    ctx   = signal["context"]
    tier  = ctx.get("next_batsman_tier")
    tier_line = f"\nNext batsman tier: `{tier}`" if tier else ""

    return (
        f"🚨 *{signal['signal']}* — `{signal['session']}`\n"
        f"Event: `{signal['event']}`\n"
        f"Line: `{signal['line_before']}` → `{signal['line_after']}`\n"
        f"Fair: `{signal['fair_change']:+.1f}` | "
        f"Actual: `{signal['actual_change']:+.1f}` | "
        f"Deviation: `{signal['deviation']:.1f}`\n\n"
        f"Score: `{ctx['score']}` @ `{ctx['overs']}` (RR `{ctx['RR']}`)\n"
        f"Striker: `{ctx.get('striker', '?')}` | "
        f"Bowler: `{ctx.get('bowler', '?')}`"
        f"{tier_line}\n\n"
        f"_Alert #{alert_id} logged_"
    )


def _fmt_stats(stats: dict, title: str) -> str:
    """Format get_stats() output as a Markdown message."""
    def pct_line(label: str, d: dict) -> str:
        return (
            f"  {label}: {d['total']} alerts | {d['won']} won | {d['pct']}%"
        )

    bs   = stats["by_session"]
    be   = stats["by_event"]
    bsig = stats["by_signal"]

    lines = [
        f"📊 *{title}*\n",
        f"Total alerts: {stats['total']}",
        f"Won: {stats['won']} ✅",
        f"Lost: {stats['lost']} ❌",
        f"Pending: {stats['pending']} ⏳",
        f"\nAccuracy: *{stats['accuracy']:.1f}%*\n",
        "*By session:*",
        pct_line("6over",  bs.get("6over",  {})),
        pct_line("20over", bs.get("20over", {})),
        "\n*By event:*",
        pct_line("Wicket", be.get("wicket", {})),
        pct_line("Four",   be.get("four",   {})),
        pct_line("Six",    be.get("six",    {})),
        pct_line("3+Dots", be.get("dot",    {})),
        "\n*By signal:*",
        pct_line("YES/OVER",  bsig.get("YES_OVER",  {})),
        pct_line("NOT/UNDER", bsig.get("NOT_UNDER", {})),
        f"\nBest: `{stats.get('best', 'N/A')}`",
        f"Worst: `{stats.get('worst', 'N/A')}`",
    ]
    return "\n".join(lines)


def _fmt_snapshot(snap: dict) -> str:
    """Format a state snapshot for /status."""
    lines_str = "\n".join(
        f"  {k}: {v}" for k, v in snap["session_lines"].items()
    )
    return (
        f"🏏 *Match Status* — `{snap['match_id']}`\n\n"
        f"Score: `{snap['runs']}/{snap['wickets']}`\n"
        f"Overs: `{snap['overs_display']}`\n"
        f"Balls: `{snap['total_balls']}`\n"
        f"RR: `{snap['current_rr']}`\n\n"
        f"Striker: `{snap.get('current_striker', '?')}`\n"
        f"Bowler: `{snap.get('current_bowler', '?')}`\n"
        f"Batsmen: `{', '.join(snap['batsmen_on_pitch'])}`\n\n"
        f"Session lines:\n{lines_str}\n\n"
        f"Consecutive dots: `{snap['consecutive_dots']}`\n"
        f"Last event: `{snap['last_event']}`\n"
        f"Pending event: `{snap['pending_event']}`"
    )


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------

@bot.message_handler(
    func=lambda msg: msg.chat.id == GROUP_ID and msg.text is not None,
    content_types=["text"],
)
def handle_group_message(message: telebot.types.Message):
    """
    Process every text message in the group.

    Steps:
    1. Parse message text.
    2. Update match state (may return a completed event).
    3. If event, run signal detection.
    4. If signal, save and send alert.
    5. If end_over, resolve pending alerts and send summary.
    """
    chat_id = message.chat.id
    text    = message.text.strip()

    try:
        state  = get_state(chat_id)
        parsed = parse_line(text)

        # Feed to state machine
        event = state.update(parsed)

        # --- Ball event completed? ---
        if event and event.get("event"):
            signal = detect_signal(event, state)
            if signal:
                try:
                    alert_id = db.save_alert(signal, state.match_id)
                    bot.send_message(
                        GROUP_ID,
                        _fmt_alert(signal, alert_id),
                        parse_mode="Markdown",
                    )
                    logger.info(
                        "Alert #%d sent: %s %s",
                        alert_id, signal["signal"], signal["session"],
                    )
                except Exception:
                    logger.error(
                        "Failed to save/send alert:\n%s", traceback.format_exc()
                    )

        # --- End of over? Auto-resolve ---
        if parsed.get("type") == "end_over":
            over = parsed["over"]
            _handle_end_of_over(chat_id, over)

    except Exception:
        logger.error(
            "Error processing message '%s':\n%s", text, traceback.format_exc()
        )


def _handle_end_of_over(chat_id: int, over: int):
    """Resolve alerts and send summary when a session milestone over is reached."""
    state = get_state(chat_id)

    if over == 6:
        session = "6over"
        actual  = state.runs
    elif over == 20:
        session = "20over"
        actual  = state.runs
    else:
        return  # Not a session boundary

    try:
        summary = db.resolve_session_alerts(state.match_id, session, actual)
        msg = (
            f"📋 *{session} session resolved*\n"
            f"Actual score: `{actual}`\n"
            f"Alerts resolved: `{summary['total']}`\n"
            f"Won: `{summary['won']}` ✅  Lost: `{summary['lost']}` ❌"
        )
        bot.send_message(GROUP_ID, msg, parse_mode="Markdown")
    except Exception:
        logger.error("Failed to resolve session:\n%s", traceback.format_exc())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: telebot.types.Message):
    """Send usage/help message."""
    help_text = (
        "🏏 *Cricket Betting Alert Bot*\n\n"
        "I live in the group and read scoreline + bookie line messages.\n\n"
        "*Commands:*\n"
        "`/newmatch`   — Start a new match (resets state)\n"
        "`/reset`      — Clear current state\n"
        "`/status`     — Current match snapshot\n\n"
        "`/accuracytoday` — Today's accuracy report\n"
        "`/accuracyall`   — All-time accuracy report\n"
        "`/pending`    — List pending alerts\n"
        "`/alert <id>` — Details of one alert\n"
        "`/result <id> <win|loss|void>` — Manual result override\n"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")


@bot.message_handler(commands=["newmatch"])
def cmd_newmatch(message: telebot.types.Message):
    """Start a new match — reset state and generate new match_id."""
    chat_id = message.chat.id
    state   = get_state(chat_id)
    old_id  = state.match_id
    state.reset()
    logger.info("New match started: %s (was %s)", state.match_id, old_id)
    bot.reply_to(
        message,
        f"✅ New match started.\nMatch ID: `{state.match_id}`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["reset"])
def cmd_reset(message: telebot.types.Message):
    """Reset state without changing match_id."""
    chat_id  = message.chat.id
    state    = get_state(chat_id)
    match_id = state.match_id
    state.reset(match_id=match_id)
    bot.reply_to(
        message,
        f"♻️ State reset. Match ID: `{match_id}`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["status"])
def cmd_status(message: telebot.types.Message):
    """Send current match state snapshot."""
    state = get_state(message.chat.id)
    bot.reply_to(message, _fmt_snapshot(state.snapshot()), parse_mode="Markdown")


@bot.message_handler(commands=["accuracytoday"])
def cmd_accuracy_today(message: telebot.types.Message):
    """Today's accuracy stats."""
    today = date.today().isoformat()
    stats = db.get_stats(date_filter=today)
    title = f"TODAY'S ACCURACY — {today}"
    bot.reply_to(message, _fmt_stats(stats, title), parse_mode="Markdown")


@bot.message_handler(commands=["accuracyall"])
def cmd_accuracy_all(message: telebot.types.Message):
    """All-time accuracy stats."""
    stats = db.get_stats()
    bot.reply_to(message, _fmt_stats(stats, "ALL-TIME ACCURACY"), parse_mode="Markdown")


@bot.message_handler(commands=["pending"])
def cmd_pending(message: telebot.types.Message):
    """List all pending alerts."""
    alerts = db.get_pending_all()
    if not alerts:
        bot.reply_to(message, "✅ No pending alerts.")
        return

    lines = ["⏳ *Pending Alerts*\n"]
    for a in alerts:
        lines.append(
            f"#{a['id']} | {a['match_id']} | {a['session']} | "
            f"{a['signal']} | {a['score']} @ {a['overs']}"
        )
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["alert"])
def cmd_alert(message: telebot.types.Message):
    """Show details of one alert: /alert <id>"""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/alert <id>`", parse_mode="Markdown")
        return

    try:
        alert_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ ID must be an integer.")
        return

    alert = db.get_alert(alert_id)
    if not alert:
        bot.reply_to(message, f"❌ Alert #{alert_id} not found.")
        return

    text = (
        f"📌 *Alert #{alert_id}*\n\n"
        f"Match: `{alert['match_id']}`\n"
        f"Session: `{alert['session']}`\n"
        f"Signal: `{alert['signal']}`\n"
        f"Event: `{alert['event']}`\n"
        f"Line: `{alert['line_before']}` → `{alert['line_after']}`\n"
        f"Fair: `{alert['fair_change']:+.1f}` | "
        f"Actual: `{alert['actual_change']:+.1f}` | "
        f"Dev: `{alert['deviation']:.1f}`\n\n"
        f"Score: `{alert['score']}` @ `{alert['overs']}`\n"
        f"Striker: `{alert['striker']}` | Bowler: `{alert['bowler']}`\n\n"
        f"Result: `{alert['result']}`\n"
        f"Resolved: `{alert.get('resolved_at', 'No')}`\n"
        f"Session end score: `{alert.get('session_end_score', '?')}`"
    )
    bot.reply_to(message, text, parse_mode="Markdown")


@bot.message_handler(commands=["result"])
def cmd_result(message: telebot.types.Message):
    """Manual result override: /result <id> <win|loss|void>"""
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(
            message,
            "Usage: `/result <id> <win|loss|void>`",
            parse_mode="Markdown",
        )
        return

    try:
        alert_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ ID must be an integer.")
        return

    result_raw = parts[2].upper()
    if result_raw not in ("WIN", "LOSS", "VOID"):
        bot.reply_to(message, "❌ Result must be win, loss, or void.")
        return

    alert = db.get_alert(alert_id)
    if not alert:
        bot.reply_to(message, f"❌ Alert #{alert_id} not found.")
        return

    db.resolve_alert(alert_id, result_raw, notes="Manual override")
    bot.reply_to(
        message,
        f"✅ Alert #{alert_id} marked as `{result_raw}`",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Bot starting… (group: %s)", GROUP_ID)
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
