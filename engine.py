"""
engine.py — Fair-value computation and signal detection.

fair_change()   → how much the line SHOULD move for a given delivery
detect_signal() → compare fair change vs actual bookie line movement
                  and emit YES_OVER / NOT_UNDER alerts
"""

from typing import Optional
from state import MatchState


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-delivery fair change table (except wicket which uses a formula)
FAIR_CHANGE_TABLE = {
    "dot":    -1,
    "run":     0,   # 1 run = neutral (2/3 handled below)
    "four":   +3,
    "six":    +5,
    "wide":   +1,
    "noball": +2,
}

# runs value → extra fair change for 2 and 3 run deliveries
RUNS_EXTRA_TABLE = {
    2: +1,
    3: +2,
}

# Wicket base drops per tier
WICKET_BASE_DROP = {
    "top_order":    7.0,
    "middle_order": 5.0,
    "tailender":    2.5,
}

# Deviation threshold for non-priority events
DEVIATION_THRESHOLD = 2.0

# Negligible movement threshold
NEGLIGIBLE_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Fair change
# ---------------------------------------------------------------------------

def fair_change(event: dict, state: MatchState) -> float:
    """
    Compute the fair line change for the given ball event.

    Returns float:
      positive = line should rise
      negative = line should drop
    """
    ev_type = event["event"]

    # ---- Wicket uses a dynamic formula ----
    if ev_type == "wicket":
        return _wicket_fair_change(event, state)

    # ---- 2 or 3 run deliveries ----
    if ev_type == "run":
        runs = event.get("runs", 1)
        if runs in RUNS_EXTRA_TABLE:
            return float(RUNS_EXTRA_TABLE[runs])
        return 0.0  # 1 run = neutral

    # ---- Standard table lookup ----
    return float(FAIR_CHANGE_TABLE.get(ev_type, 0))


def _wicket_fair_change(event: dict, state: MatchState) -> float:
    """
    Wicket fair change formula:

        base_drop    = tier-based constant
        balls_factor = balls_remaining_6over / 36
        rr_factor    = current_rr / 8.5
        wicket_drop  = base_drop * balls_factor * rr_factor
        fair_change  = -wicket_drop
    """
    tier = event.get("next_batsman_tier") or "middle_order"
    base_drop = WICKET_BASE_DROP.get(tier, WICKET_BASE_DROP["middle_order"])

    balls_remaining = event.get("balls_remaining_6over", 18)
    balls_factor = balls_remaining / 36

    rr = state.current_rr if state.current_rr > 0 else 6.0
    rr_factor = rr / 8.5

    wicket_drop = base_drop * balls_factor * rr_factor
    return -round(wicket_drop, 4)


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def detect_signal(event: dict, state: MatchState) -> Optional[dict]:
    """
    Compare fair change vs actual bookie line movement.

    Checks both 6over and 20over sessions.
    Returns the FIRST signal found (6over takes priority).
    Returns None if no signal.

    Signal logic:
      fair < 0 (line should fall):
          actual > fair  → didn't fall enough → YES_OVER
          actual < fair  → fell more than fair → NOT_UNDER

      fair > 0 (line should rise):
          actual < fair  → didn't rise enough → NOT_UNDER
          actual > fair  → rose more than expected → YES_OVER

    Anti-spam:
      Always emit for: wicket, four, six, consecutive_dots >= 3
      Otherwise only emit when deviation > DEVIATION_THRESHOLD

    Early exit:
      If both fair_change and actual_change are near zero,
      there is nothing meaningful to signal regardless of event type.
    """
    ev_type = event["event"]
    fair = fair_change(event, state)

    # Priority events always bypass the deviation gate
    is_priority = (
        ev_type in ("wicket", "four", "six")
        or getattr(state, "consecutive_dots", 0) >= 3
    )

    for session in ["6over", "20over"]:
        line_before = event.get(f"line_before_{session}")
        line_after  = event.get(f"line_after_{session}")

        # Skip sessions without both snapshots
        if line_before is None or line_after is None:
            continue

        before_mid = (line_before["low"] + line_before["high"]) / 2
        after_mid  = (line_after["low"]  + line_after["high"])  / 2

        actual_change = after_mid - before_mid
        deviation = abs(fair - actual_change)

        # FIX 1 — Early exit when both movements are negligible
        if abs(fair) < NEGLIGIBLE_THRESHOLD and abs(actual_change) < NEGLIGIBLE_THRESHOLD:
            continue

        # FIX 2 — Priority check runs BEFORE deviation filter
        if not is_priority and deviation <= DEVIATION_THRESHOLD:
            continue

        # Determine signal direction
signal_type = _classify_signal(fair, actual_change)

# Priority events with directional fair change always emit,
# even if deviation == 0 (line moved as expected). This catches
# 3+ dot streaks and other momentum signals regardless of bookie
# response.
if signal_type is None:
    if is_priority and fair != 0:
        signal_type = "YES_OVER" if fair > 0 else "NOT_UNDER"
    else:
        continue

        return {
            "signal":        signal_type,
            "session":       session,
            "event":         ev_type,
            "line_before":   before_mid,
            "line_after":    after_mid,
            "fair_change":   round(fair, 2),
            "actual_change": round(actual_change, 2),
            "deviation":     round(deviation, 2),
            "context": {
                "score":             f"{state.runs}/{state.wickets}",
                "overs":             state.overs_display,
                "RR":                round(state.current_rr, 2),
                "striker":           state.current_striker,
                "bowler":            state.current_bowler,
                "next_batsman_tier": event.get("next_batsman_tier"),
            },
        }

    return None


def _classify_signal(fair: float, actual_change: float) -> Optional[str]:
    """
    Classify whether a signal exists and what type.
    Returns "YES_OVER", "NOT_UNDER", or None.
    """
    if fair == 0:
        return None  # neutral ball → no signal

    if fair < 0:
        # Line should fall
        if actual_change > fair:
            return "YES_OVER"
        if actual_change < fair:
            return "NOT_UNDER"
        return None  # moved exactly as expected

    # fair > 0: line should rise
    if actual_change < fair:
        return "NOT_UNDER"
    if actual_change > fair:
        return "YES_OVER"
    return None
