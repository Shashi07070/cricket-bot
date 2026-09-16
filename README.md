# Cricket Betting Alert Bot 🏏

A Telegram bot that monitors cricket match score feeds and bookie
session lines, detects fair-value deviations ball-by-ball, and sends
real-time YES/OVER and NOT/UNDER alerts to your group.

---

## Architecture

```
parser.py   ← parse raw Telegram text into typed dicts
state.py    ← maintain in-memory MatchState per chat
engine.py   ← compute fair value and detect signals
db.py       ← persist alerts to SQLite; track accuracy
bot.py      ← Telegram wiring, commands, alert formatting
test_all.py ← full pytest suite
```

---

## Setup on Termux (Android)

```bash
# 1. Install Python
pkg update && pkg install python

# 2. Clone / copy project files into a folder
mkdir cricket-bot && cd cricket-bot
# (copy all files here)

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env file
cp .env.example .env
nano .env          # fill in BOT_TOKEN and GROUP_ID
```

---

## BotFather Setup

1. Open Telegram → search **@BotFather**
2. Send `/newbot` → follow prompts → copy the **token**
3. Open your bot → **Bot Settings** → **Group Privacy** → **Disable**
   (allows the bot to read all group messages without being mentioned)
4. Add the bot to your cricket scoring group

---

## Get GROUP_ID

1. Add **@userinfobot** to the group
2. Send any message in the group
3. The bot replies with `Chat ID: -100XXXXXXXXXX`
4. Copy that negative number into your `.env` as `GROUP_ID`

---

## Run the Bot

```bash
python bot.py
```

The bot starts polling Telegram. Keep this terminal open
(or use `nohup python bot.py &` for background).

---

## Run Tests

```bash
pytest test_all.py -v
```

Expected output: all tests pass with descriptive names.

---

## Command Reference

| Command | Description |
|---|---|
| `/start` | Show help / usage message |
| `/newmatch` | Reset state and start a new match |
| `/reset` | Clear state (keep same match ID) |
| `/status` | Show current score, lines, RR, batsmen |
| `/accuracytoday` | Today's win/loss accuracy report |
| `/accuracyall` | All-time accuracy report |
| `/pending` | List all unresolved alerts |
| `/alert <id>` | Show full details of one alert |
| `/result <id> <win\|loss\|void>` | Manual result override |

---

## Message Flow

```
Group message arrives
        │
        ▼
   parse_line()          → typed dict
        │
        ▼
  state.update()         → event dict (when ball complete)
        │
        ▼
 detect_signal()         → signal dict | None
        │
        ▼
  db.save_alert()        → alert_id
        │
        ▼
bot.send_message()       → formatted alert in group

[at end of over 6 / over 20]
        │
        ▼
resolve_session_alerts() → WIN / LOSS each pending alert
        │
        ▼
  send summary message
```

---

## Input Format Reference

| Message | Parsed As |
|---|---|
| `50-4 🇩🇪 AMSTERDAM 🇩🇪` | main_score runs=50 wickets=4 |
| `JASON ROY 🏏🏏` | batsman name=JASON ROY |
| `D-PAYNE 🎾` | bowler name=D-PAYNE |
| `0.1 🎾 0/0` | ball over=0 ball=1 |
| `JASON ROY ON STRIKE ✔️` | strike player=JASON ROY |
| `0` / `4` / `6` | runs value=N |
| `🚾 WKT GYA WKT 🚾` | wicket |
| `WIDE BALL ✔️✔️` | extra wide |
| `NO BALL ✔️✔️` | extra noball |
| `RUN RATE PER OVER 🔥👉 3.00` | run_rate value=3.0 |
| `1 OVER 6/1 📟📟` | end_over over=1 runs=6 wkts=1 |
| `47-8 👈🏻🖲 6 OVER 🖲` | session_line 6over low=47 high=48 |
| `166-8 👈🏻🖲 20 OVER 🖲` | session_line 20over low=166 high=168 |

---

## Bookie Line Shorthand Rule

```
"47-8"  → low=47, high=48  (8 is last digit of 48)
"166-8" → low=166, high=168
"48-50" → low=48, high=50  (second ≥ 20 → use directly)
```

---

## Fair Value Table

| Event | Fair Change |
|---|---|
| Dot | −1 |
| 1 run | 0 |
| 2 runs | +1 |
| 3 runs | +2 |
| Four | +3 |
| Six | +5 |
| Wide | +1 |
| No Ball | +2 |
| Wicket | −(base × balls_factor × rr_factor) |

**Wicket formula:**
```
base_drop    = {top_order: 7, middle_order: 5, tailender: 2.5}
balls_factor = balls_remaining_6over / 36
rr_factor    = current_rr / 8.5
drop         = base_drop × balls_factor × rr_factor
fair_change  = −drop
```

---

## Signal Logic

```
fair < 0 (line should fall):
    actual > fair  → YES_OVER   (line didn't fall enough)
    actual < fair  → NOT_UNDER  (line fell too much)

fair > 0 (line should rise):
    actual < fair  → NOT_UNDER  (line didn't rise enough)
    actual > fair  → YES_OVER   (line rose more than expected)
```

---

## Alert Format

```
🚨 YES_OVER — 6over
Event: wicket
Line: 47.5 → 46.5
Fair: -3.5 | Actual: -1.0 | Deviation: 2.5

Score: 48/3 @ 4.2 (RR 7.20)
Striker: JASON ROY | Bowler: D-PAYNE
Next batsman tier: middle_order

Alert #42 logged
```

---

## Database

Alerts are stored in `alerts.db` (SQLite, auto-created on first run).

```sql
SELECT * FROM alerts ORDER BY id DESC LIMIT 10;
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Bot not reading messages | Disable group privacy in BotFather |
| `BOT_TOKEN not set` | Check `.env` file exists in same directory |
| No alerts sending | Confirm `GROUP_ID` is correct (negative integer) |
| Database locked | Only one `python bot.py` process at a time |
