"""
state.py — MatchState tracks all in-memory match state across messages.

The update() method is the single entry point: feed it a parsed dict and
it advances state, returning a ball-event dict when a scoreable delivery
is complete.  The "pending_event" pattern handles the two-message nature
of line snapshots (line_before captured on ball, line_after captured on
next session_line message).
"""

import uuid
from copy import deepcopy
from typing import Optional


# ---------------------------------------------------------------------------
# Batting order tier boundaries
# ---------------------------------------------------------------------------
TOP_ORDER_MAX    = 3
MIDDLE_ORDER_MAX = 6
# 7-11 → tailender


class MatchState:
    """
    Holds all mutable state for one match.

    Attributes are updated incrementally as parsed messages arrive.
    Thread-safety is NOT provided here; the Telegram bot serialises
    messages per chat_id, so a single lock at the bot layer is enough.
    """

    def __init__(self, match_id: Optional[str] = None):
        # --- Scoring state ---
        self.runs: int = 0
        self.wickets: int = 0
        self.total_balls: int = 0          # legal deliveries faced

        # Human-readable over.ball display (e.g. "5.3")
        self.overs_display: str = "0.0"

        # --- Player state ---
        self.batsmen_on_pitch: list = []   # max 2 names
        self.current_striker: Optional[str] = None
        self.current_bowler: Optional[str] = None

        # Batting order: increments every time a new batsman comes in
        # Starts at 1 (openers are positions 1 & 2 but we track arrivals)
        self.batting_order_index: int = 1

        # --- Session lines ---
        # session → {"low": int, "high": int}
        self.session_lines: dict = {
            "6over":  None,
            "20over": None,
        }

        # --- Ball history (last 12 events as short dicts) ---
        self.ball_history: list = []

        # Consecutive dot balls
        self.consecutive_dots: int = 0

        # Current run rate
        self.current_rr: float = 0.0

        # Match identifier
        self.match_id: str = match_id or self._new_id()

        # Last event label (dot / run / four / six / wicket / wide / noball)
        self.last_event: Optional[str] = None

        # ---------------------------------------------------------------
        # Pending event: holds a ball event waiting for line_after data.
        # Structure: {"event_dict": {...}, "sessions_waiting": set()}
        # ---------------------------------------------------------------
        self._pending_event: Optional[dict] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())[:8].upper()

    def classify_next_batsman(self) -> str:
        """
        Return batting tier of the batsman who will come in NEXT.

        Uses batting_order_index (already incremented on wicket fall).
        """
        idx = self.batting_order_index
        if idx <= TOP_ORDER_MAX:
            return "top_order"
        if idx <= MIDDLE_ORDER_MAX:
            return "middle_order"
        return "tailender"

    def _balls_remaining(self, session: str) -> int:
        """Balls left before the given session milestone."""
        if session == "6over":
            target_balls = 36
        elif session == "20over":
            target_balls = 120
        else:
            return 0
        remaining = target_balls - self.total_balls
        return max(remaining, 0)

    def _snapshot_line(self, session: str) -> Optional[dict]:
        """Return a deep copy of the current session line, or None."""
        line = self.session_lines.get(session)
        return deepcopy(line) if line else None

    def _add_to_history(self, event_label: str, runs: int):
        """Keep the last 12 ball events for context."""
        self.ball_history.append({"event": event_label, "runs": runs})
        if len(self.ball_history) > 12:
            self.ball_history.pop(0)

    def _update_overs_display(self):
        """Recompute the over.ball display string."""
        full_overs = self.total_balls // 6
        balls_in_over = self.total_balls % 6
        self.overs_display = f"{full_overs}.{balls_in_over}"

    # ------------------------------------------------------------------
    # Pending event handling
    # ------------------------------------------------------------------

    def _create_pending(self, event_dict: dict):
        """
        Store a ball event as pending.

        line_before_* are already set.  line_after_* will be filled
        when the next session_line message arrives.
        """
        self._pending_event = {
            "event_dict": event_dict,
            # Track which sessions still need their after-line snapshot
            "sessions_waiting": {
                s for s in ["6over", "20over"]
                if event_dict.get(f"line_before_{s}") is not None
            },
        }

    def _fill_pending_line_after(self, session: str) -> Optional[dict]:
        """
        When a session_line arrives, fill the line_after slot in the
        pending event if we're still waiting for that session.

        Returns the completed event dict once ALL sessions have been
        filled, or None if still waiting.
        """
        if self._pending_event is None:
            return None

        waiting = self._pending_event["sessions_waiting"]
        ev = self._pending_event["event_dict"]

        if session in waiting:
            ev[f"line_after_{session}"] = self._snapshot_line(session)
            waiting.discard(session)

        if not waiting:
            # All after-lines received; event is ready to return
            completed = ev
            self._pending_event = None
            return completed

        return None   # still waiting for more sessions

    # ------------------------------------------------------------------
    # Public: update state from a parsed message
    # ------------------------------------------------------------------

    def update(self, parsed: dict) -> Optional[dict]:
        """
        Advance match state from a parsed message dict.

        Returns a fully-populated event dict when a ball event is
        complete (i.e., both line_before and line_after are captured).
        Returns None otherwise.
        """
        msg_type = parsed.get("type", "unknown")

        # ---- Main score -----------------------------------------------
        if msg_type == "main_score":
            self.runs = parsed["runs"]
            self.wickets = parsed["wickets"]
            return None

        # ---- Session line ---------------------------------------------
        if msg_type == "session_line":
            session = parsed["session"]
            self.session_lines[session] = {
                "low":  parsed["low"],
                "high": parsed["high"],
            }
            # Attempt to complete any pending event waiting for this line
            return self._fill_pending_line_after(session)

        # ---- Batsman --------------------------------------------------
        if msg_type == "batsman":
            name = parsed["name"]
            if name not in self.batsmen_on_pitch:
                self.batsmen_on_pitch.append(name)
                if len(self.batsmen_on_pitch) > 2:
                    self.batsmen_on_pitch.pop(0)
            return None

        # ---- Bowler ---------------------------------------------------
        if msg_type == "bowler":
            self.current_bowler = parsed["name"]
            return None

        # ---- Strike ---------------------------------------------------
        if msg_type == "strike":
            self.current_striker = parsed["player"]
            return None

        # ---- Ball marker (over.ball 🎾 runs/wkts) --------------------
        if msg_type == "ball":
            # over.ball is informational; we track total_balls separately
            return None

        # ---- Run rate -------------------------------------------------
        if msg_type == "run_rate":
            self.current_rr = parsed["value"]
            return None

        # ---- End of over ---------------------------------------------
        if msg_type == "end_over":
            # Authoritative score update at over boundary
            self.runs = parsed["runs"]
            self.wickets = parsed["wickets"]
            return None

        # ---- BALL EVENTS (dot / run / four / six) --------------------
        if msg_type == "runs":
            return self._handle_runs(parsed["value"])

        # ---- Wicket --------------------------------------------------
        if msg_type == "wicket":
            return self._handle_wicket()

        # ---- Extra (wide / noball) -----------------------------------
        if msg_type == "extra":
            return self._handle_extra(parsed["extra"])

        return None

    # ------------------------------------------------------------------
    # Ball event handlers
    # ------------------------------------------------------------------

    def _handle_runs(self, value: int) -> Optional[dict]:
        """Process a runs delivery."""
        self.runs += value
        self.total_balls += 1
        self._update_overs_display()

        if value == 0:
            event_label = "dot"
            self.consecutive_dots += 1
        elif value == 4:
            event_label = "four"
            self.consecutive_dots = 0
        elif value == 6:
            event_label = "six"
            self.consecutive_dots = 0
        else:
            event_label = "run"
            self.consecutive_dots = 0

        self.last_event = event_label
        self._add_to_history(event_label, value)

        ev = self._build_event_dict(event_label, value)
        self._create_pending(ev)
        return None

    def _handle_wicket(self) -> Optional[dict]:
        """Process a wicket."""
        self.wickets += 1
        self.total_balls += 1
        self._update_overs_display()
        self.consecutive_dots = 0

        # Increment batting order BEFORE classifying so we get the
        # tier of the new batsman coming IN
        self.batting_order_index += 1
        next_tier = self.classify_next_batsman()

        # Remove batsman from pitch list (we don't know which one fell,
        # so remove the non-striker if possible, else just the last entry)
        if len(self.batsmen_on_pitch) > 1:
            self.batsmen_on_pitch.pop(1)
        elif self.batsmen_on_pitch:
            self.batsmen_on_pitch.pop()

        self.last_event = "wicket"
        self._add_to_history("wicket", 0)

        ev = self._build_event_dict("wicket", 0, next_batsman_tier=next_tier)
        self._create_pending(ev)
        return None

    def _handle_extra(self, extra_type: str) -> Optional[dict]:
        """Process a wide or no-ball (not a legal delivery for over count)."""
        if extra_type == "wide":
            self.runs += 1
            event_label = "wide"
        elif extra_type == "noball":
            self.runs += 1
            event_label = "noball"
        else:
            return None

        # Extras do NOT increment total_balls (not legal deliveries)
        self.consecutive_dots = 0
        self.last_event = event_label
        self._add_to_history(event_label, 1)

        ev = self._build_event_dict(event_label, 1)
        self._create_pending(ev)
        return None

    # ------------------------------------------------------------------
    # Event dict builder
    # ------------------------------------------------------------------

    def _build_event_dict(
        self,
        event_label: str,
        runs: int,
        next_batsman_tier: Optional[str] = None,
    ) -> dict:
        """Build the standard event dict with line_before snapshots."""
        return {
            "event": event_label,
            "runs": runs,
            # Before-snapshots captured NOW (before next line arrives)
            "line_before_6over":  self._snapshot_line("6over"),
            "line_before_20over": self._snapshot_line("20over"),
            # After-snapshots will be filled by _fill_pending_line_after
            "line_after_6over":   None,
            "line_after_20over":  None,
            "next_batsman_tier":  next_batsman_tier,
            "balls_remaining_6over":  self._balls_remaining("6over"),
            "balls_remaining_20over": self._balls_remaining("20over"),
        }

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return full state as a plain dict (for /status command)."""
        return {
            "match_id":           self.match_id,
            "runs":               self.runs,
            "wickets":            self.wickets,
            "total_balls":        self.total_balls,
            "overs_display":      self.overs_display,
            "batsmen_on_pitch":   self.batsmen_on_pitch,
            "current_striker":    self.current_striker,
            "current_bowler":     self.current_bowler,
            "batting_order_index":self.batting_order_index,
            "session_lines":      deepcopy(self.session_lines),
            "consecutive_dots":   self.consecutive_dots,
            "current_rr":         self.current_rr,
            "last_event":         self.last_event,
            "ball_history":       self.ball_history[-5:],  # last 5 for display
            "pending_event":      bool(self._pending_event),
        }

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, match_id: Optional[str] = None):
        """Re-initialise all state (same object, new match_id)."""
        self.__init__(match_id=match_id or self._new_id())
