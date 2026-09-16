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
    "run":     0,   # 1 run = neutral
    "four":   +3,
    "six":    +5,
    "wide":   +1,
    "noball": +2,
}

# runs value → event override for 2 and 3 run deliveries
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

# Minimum meaningful movement — below this in both fair and actual,
# there is nothing to signal even for priority events
NEGLIGIBLE_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Fair change
# ---------------------------------------------------------------------------

def fair_change(event: dict, state: MatchState) -> float:
    """
    Compute the fair line change for the given ball event.

    Parameters
    ----------
    event : dict
        Ball event dict produced by MatchState.update() (via pending).
    state : MatchState
        Current match state (used for RR and balls_remaining in wicket formula).

    Returns
    -------
    float
        Expected line movement (positive = line should rise,
        negative = line should drop).
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

        base_drop      = tier-based constant
        balls_factor   = balls_remaining_6over / 36
        rr_factor      = current_rr / 8.5
        wicket_drop    = base_drop * balls_factor * rr_factor
        fair_change    = -wicket_drop

    Uses 6over balls_remaining as proxy for session importance.
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

    Checks both 6over and 20over sessions.  Returns the FIRST signal
    found (6over takes priority).  Returns None if no signal.

    Signal logic
    ------------
    fair < 0 (line should fall):
        actual > fair  → bookie line didn't fall enough → YES_OVER
        actual < fair  → bookie line fell more than fair → NOT_UNDER

    fair > 0 (line should rise):
        actual < fair  → bookie line didn't rise enough → NOT_UNDER
        actual > fair  → bookie line rose more than expected → YES_OVER

    Anti-spam gate
    --------------
    Always emit for: wicket, four, six, consecutive_dots >= 3
    Otherwise only emit when deviation > DEVIATION_THRESHOLD

    Early exit
    ----------
    If both fair_change and actual_change are near zero (abs < 0.5),
    there is nothing meaningful to signal regardless of event type.
    """
    ev_type = event["event"]
    fair = fair_change(event, state)

    # ----------------------------------------------------------------
    # Priority flag: these event types always bypass the deviation gate.
    # Evaluated once here so it applies consistently across all sessions.
    # ----------------------------------------------------------------
    is_priority = (
        ev_type in ("wicket", "four", "six")
        or state.consecutive_dots >= 3
    )

    for session in ["6over", "20over"]:
        line_before = event.get(f"line_before_{session}")
        line_after  = event.get(f"line_after_{session}")

        # Skip sessions where we don't have both snapshots
        if line_before is None or line_after is None:
            continue

        before_mid = (line_before["low"] + line_before["high"]) / 2
        after_mid  = (line_after["low"]  + line_after["high"])  / 2

        actual_change = after_mid - before_mid
        deviation = abs(fair - actual_change)

        # ----------------------------------------------------------------
        # FIX 1 — Early exit when both movements are negligible.
        #
        # If fair_change ≈ 0 AND actual_change ≈ 0 there is no meaningful
        # divergence to act on, even for priority events.  This catches the
        # case where a near-zero-RR wicket produces fair ≈ 0 and the line
        # also doesn't move (actual = 0), which previously fell through to
        # _classify_signal() and incorrectly returned YES_OVER because
        # actual_change(0) > fair(-0.001) evaluated as True.
        # ----------------------------------------------------------------
        if abs(fair) < NEGLIGIBLE_THRESHOLD and abs(actual_change) < NEGLIGIBLE_THRESHOLD:
            continue  # nothing meaningful in this session; try next

        # ----------------------------------------------------------------
        # FIX 2 — Priority check runs BEFORE the deviation filter.
        #
        # is_priority is already computed above.  Non-priority events are
        # silenced when deviation <= threshold.  Priority events skip this
        # gate entirely so consecutive dots / big hits always fire.
        # ----------------------------------------------------------------
        if not is_priority and deviation <= DEVIATION_THRESHOLD:
            continue

        # Determine signal direction
        signal_type = _classify_signal(fair, actual_change)
        if signal_type is None:
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
            return "YES_OVER"   # didn't fall enough → over-value
        if actual_change < fair:
            return "NOT_UNDER"  # fell too much → under-value
        return None             # moved exactly as expected

    # fair > 0: line should rise
    if actual_change < fair:
        return "NOT_UNDER"  # didn't rise enough
    if actual_change > fair:
        return "YES_OVER"   # rose more than expected
    return None
