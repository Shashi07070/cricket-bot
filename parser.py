"""
parser.py — Parse Telegram cricket group messages into structured dicts.

Each message type is identified by regex patterns and emoji markers.
Returns a dict with a "type" field describing what was parsed.
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Strip leading/trailing whitespace."""
    return text.strip()


def _parse_bookie_high(low: int, second: int) -> int:
    """
    Apply shorthand rule for bookie line high value.

    If second number < 20 AND low > 20:
        high = (low // 10) * 10 + second
        e.g. 47-8  → (47//10)*10 + 8 = 48
        e.g. 166-8 → (166//10)*10 + 8 = 168
    Else:
        high = second directly
        e.g. 48-50 → high = 50
    """
    if second < 20 and low > 20:
        base = (low // 10) * 10
        high = base + second
        # Edge case: if computed high < low, bump by 10
        if high < low:
            high += 10
        return high
    return second


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Main score: "50-4 🇩🇪 AMSTERDAM 🇩🇪"
# Looks for digits-digits followed by a country flag or team name
_RE_MAIN_SCORE = re.compile(
    r'^(\d+)-(\d+)\s+[\U0001F1E0-\U0001F1FF]',
    re.UNICODE
)

# Bookie session line: "47-8 👈🏻🖲 6 OVER 🖲" or "166-8 👈🏻🖲 20 OVER 🖲"
_RE_SESSION_LINE = re.compile(
    r'^(\d+)-(\d+)\s+👈.*?(\d+)\s+OVER',
    re.UNICODE | re.IGNORECASE
)

# Batsman: "JASON ROY 🏏🏏"
_RE_BATSMAN = re.compile(
    r'^([A-Z][A-Z\s\-\.\']+?)\s+🏏🏏',
    re.UNICODE
)

# Bowler: "D-PAYNE 🎾"  (single cricket ball emoji, NOT the ball-in-over pattern)
_RE_BOWLER = re.compile(
    r'^([A-Z][A-Z\s\-\.\']+?)\s+🎾\s*$',
    re.UNICODE
)

# Ball in over: "0.1 🎾 0/0"
_RE_BALL = re.compile(
    r'^(\d+)\.(\d+)\s+🎾\s+(\d+)/(\d+)\s*$',
    re.UNICODE
)

# Strike indicator: "JASON ROY ON STRIKE ✔️"
_RE_STRIKE = re.compile(
    r'^([A-Z][A-Z\s\-\.\']+?)\s+ON STRIKE',
    re.UNICODE | re.IGNORECASE
)

# Runs this ball: standalone single digit (0-9)
_RE_RUNS = re.compile(r'^\s*(\d)\s*$')

# Wicket: "🚾 WKT GYA WKT 🚾" or any message containing 🚾
_RE_WICKET = re.compile(r'🚾', re.UNICODE)

# Wide ball
_RE_WIDE = re.compile(r'WIDE\s*BALL', re.IGNORECASE)

# No ball
_RE_NOBALL = re.compile(r'NO\s*BALL', re.IGNORECASE)

# Run rate: "RUN RATE PER OVER 🔥👉 3.00"
_RE_RR = re.compile(
    r'RUN\s*RATE.*?(\d+\.\d+)',
    re.IGNORECASE
)

# End of over: "1 OVER 6/1 📟📟"
_RE_END_OVER = re.compile(
    r'^(\d+)\s+OVER\s+(\d+)/(\d+)\s+📟📟',
    re.UNICODE | re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_line(text: str) -> dict:
    """
    Parse a single Telegram message line into a structured dict.

    Parameters
    ----------
    text : str
        Raw message text from the Telegram group.

    Returns
    -------
    dict
        Always contains "type" key. See module docstring for all types.
    """
    if not text:
        return {"type": "unknown", "raw": text}

    t = _clean(text)

    # ---- End of over (must check before main score because it contains digits) ----
    m = _RE_END_OVER.match(t)
    if m:
        return {
            "type": "end_over",
            "over": int(m.group(1)),
            "runs": int(m.group(2)),
            "wickets": int(m.group(3)),
        }

    # ---- Wicket (emoji check first — simple) ----
    if _RE_WICKET.search(t):
        return {"type": "wicket"}

    # ---- Wide ball ----
    if _RE_WIDE.search(t):
        return {"type": "extra", "extra": "wide"}

    # ---- No ball ----
    if _RE_NOBALL.search(t):
        return {"type": "extra", "extra": "noball"}

    # ---- Bookie session line ----
    m = _RE_SESSION_LINE.match(t)
    if m:
        low = int(m.group(1))
        second = int(m.group(2))
        over_num = int(m.group(3))
        high = _parse_bookie_high(low, second)
        session = f"{over_num}over"
        return {
            "type": "session_line",
            "session": session,
            "low": low,
            "high": high,
        }

    # ---- Main score (country flag follows digits-digits) ----
    m = _RE_MAIN_SCORE.match(t)
    if m:
        return {
            "type": "main_score",
            "runs": int(m.group(1)),
            "wickets": int(m.group(2)),
        }

    # ---- Ball in over: "0.1 🎾 0/0" ----
    m = _RE_BALL.match(t)
    if m:
        return {
            "type": "ball",
            "over": int(m.group(1)),
            "ball": int(m.group(2)),
            "bowler_runs": int(m.group(3)),
            "bowler_wickets": int(m.group(4)),
        }

    # ---- Bowler: single 🎾 at end ----
    m = _RE_BOWLER.match(t)
    if m:
        return {
            "type": "bowler",
            "name": m.group(1).strip(),
        }

    # ---- Batsman: 🏏🏏 ----
    m = _RE_BATSMAN.match(t)
    if m:
        return {
            "type": "batsman",
            "name": m.group(1).strip(),
        }

    # ---- Striker ----
    m = _RE_STRIKE.match(t)
    if m:
        return {
            "type": "strike",
            "player": m.group(1).strip(),
        }

    # ---- Run rate ----
    m = _RE_RR.search(t)
    if m:
        return {
            "type": "run_rate",
            "value": float(m.group(1)),
        }

    # ---- Runs this ball (standalone digit) ----
    m = _RE_RUNS.match(t)
    if m:
        return {
            "type": "runs",
            "value": int(m.group(1)),
        }

    # ---- Fallback ----
    return {"type": "unknown", "raw": t}
