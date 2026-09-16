"""
test_all.py — pytest test suite for the cricket betting alert bot.

Run with:  pytest test_all.py -v
"""

import os
import sys
import tempfile
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

from parser import parse_line, _parse_bookie_high
from state import MatchState
import engine
import db as db_module


# ===========================================================================
# PARSER TESTS  (15 tests)
# ===========================================================================

class TestParser:

    # 1. Main score
    def test_main_score_basic(self):
        result = parse_line("50-4 🇩🇪 AMSTERDAM 🇩🇪")
        assert result["type"] == "main_score"
        assert result["runs"] == 50
        assert result["wickets"] == 4

    # 2. Batsman
    def test_batsman(self):
        result = parse_line("JASON ROY 🏏🏏")
        assert result["type"] == "batsman"
        assert result["name"] == "JASON ROY"

    # 3. Bowler
    def test_bowler(self):
        result = parse_line("D-PAYNE 🎾")
        assert result["type"] == "bowler"
        assert result["name"] == "D-PAYNE"

    # 4. Ball in over
    def test_ball_in_over(self):
        result = parse_line("0.1 🎾 0/0")
        assert result["type"] == "ball"
        assert result["over"] == 0
        assert result["ball"] == 1
        assert result["bowler_runs"] == 0
        assert result["bowler_wickets"] == 0

    # 5. Strike indicator
    def test_striker(self):
        result = parse_line("JASON ROY ON STRIKE ✔️")
        assert result["type"] == "strike"
        assert result["player"] == "JASON ROY"

    # 6. Runs — zero (dot)
    def test_runs_zero(self):
        result = parse_line("0")
        assert result["type"] == "runs"
        assert result["value"] == 0

    # 7. Runs — four
    def test_runs_four(self):
        result = parse_line("4")
        assert result["type"] == "runs"
        assert result["value"] == 4

    # 8. Runs — six
    def test_runs_six(self):
        result = parse_line("6")
        assert result["type"] == "runs"
        assert result["value"] == 6

    # 9. Wicket
    def test_wicket(self):
        result = parse_line("🚾 WKT GYA WKT 🚾")
        assert result["type"] == "wicket"

    # 10. Wide ball
    def test_wide(self):
        result = parse_line("WIDE BALL ✔️✔️")
        assert result["type"] == "extra"
        assert result["extra"] == "wide"

    # 11. No ball
    def test_noball(self):
        result = parse_line("NO BALL ✔️✔️")
        assert result["type"] == "extra"
        assert result["extra"] == "noball"

    # 12. Run rate
    def test_run_rate(self):
        result = parse_line("RUN RATE PER OVER 🔥👉 3.00")
        assert result["type"] == "run_rate"
        assert result["value"] == pytest.approx(3.00)

    # 13. End of over
    def test_end_over(self):
        result = parse_line("1 OVER 6/1 📟📟")
        assert result["type"] == "end_over"
        assert result["over"] == 1
        assert result["runs"] == 6
        assert result["wickets"] == 1

    # 14. Bookie line shorthand — "47-8" → low=47, high=48
    def test_bookie_line_shorthand_small(self):
        result = parse_line("47-8 👈🏻🖲 6 OVER 🖲")
        assert result["type"] == "session_line"
        assert result["session"] == "6over"
        assert result["low"] == 47
        assert result["high"] == 48

    # 15. Bookie line shorthand — "166-8" → low=166, high=168
    def test_bookie_line_shorthand_large(self):
        result = parse_line("166-8 👈🏻🖲 20 OVER 🖲")
        assert result["type"] == "session_line"
        assert result["session"] == "20over"
        assert result["low"] == 166
        assert result["high"] == 168

    # Bonus: full high "48-50"
    def test_bookie_line_full_high(self):
        result = parse_line("48-50 👈🏻🖲 6 OVER 🖲")
        assert result["type"] == "session_line"
        assert result["low"] == 48
        assert result["high"] == 50

    # Bonus: parse_bookie_high helper directly
    def test_bookie_high_helper_shorthand(self):
        assert _parse_bookie_high(47, 8) == 48

    def test_bookie_high_helper_full(self):
        assert _parse_bookie_high(48, 50) == 50

    # Bonus: unknown message
    def test_unknown(self):
        result = parse_line("some random text without pattern")
        assert result["type"] == "unknown"


# ===========================================================================
# STATE TESTS
# ===========================================================================

class TestMatchState:

    def _feed(self, state: MatchState, messages: list):
        """Feed a list of raw message strings; return list of events."""
        events = []
        for msg in messages:
            parsed = parse_line(msg)
            ev = state.update(parsed)
            if ev:
                events.append(ev)
        return events

    def test_initial_state(self):
        s = MatchState()
        assert s.runs == 0
        assert s.wickets == 0
        assert s.total_balls == 0
        assert s.batting_order_index == 1

    def test_main_score_updates(self):
        s = MatchState()
        s.update(parse_line("50-4 🇩🇪 AMSTERDAM 🇩🇪"))
        assert s.runs == 50
        assert s.wickets == 4

    def test_bowler_updates(self):
        s = MatchState()
        s.update(parse_line("D-PAYNE 🎾"))
        assert s.current_bowler == "D-PAYNE"

    def test_striker_updates(self):
        s = MatchState()
        s.update(parse_line("JASON ROY ON STRIKE ✔️"))
        assert s.current_striker == "JASON ROY"

    def test_run_rate_updates(self):
        s = MatchState()
        s.update(parse_line("RUN RATE PER OVER 🔥👉 7.50"))
        assert s.current_rr == pytest.approx(7.50)

    def test_wicket_increments_order(self):
        s = MatchState()
        before = s.batting_order_index
        s.update(parse_line("🚾 WKT GYA WKT 🚾"))
        assert s.batting_order_index == before + 1
        assert s.wickets == 1

    def test_consecutive_dots_tracked(self):
        s = MatchState()
        for _ in range(3):
            s.update(parse_line("0"))
        assert s.consecutive_dots == 3

    def test_consecutive_dots_reset_on_runs(self):
        s = MatchState()
        for _ in range(3):
            s.update(parse_line("0"))
        s.update(parse_line("4"))
        assert s.consecutive_dots == 0

    def test_session_line_captured(self):
        s = MatchState()
        s.update(parse_line("47-8 👈🏻🖲 6 OVER 🖲"))
        assert s.session_lines["6over"]["low"] == 47
        assert s.session_lines["6over"]["high"] == 48

    def test_line_before_snapshot_on_runs(self):
        """line_before is captured at ball time, not after."""
        s = MatchState()
        s.update(parse_line("47-8 👈🏻🖲 6 OVER 🖲"))
        # A runs event should store line_before
        s.update(parse_line("4"))   # triggers pending event
        assert s._pending_event is not None
        ev = s._pending_event["event_dict"]
        assert ev["line_before_6over"]["low"] == 47

    def test_pending_event_completed_by_session_line(self):
        """line_after is set when next session_line arrives."""
        s = MatchState()
        s.update(parse_line("47-8 👈🏻🖲 6 OVER 🖲"))
        s.update(parse_line("4"))          # creates pending
        completed = s.update(parse_line("48-50 👈🏻🖲 6 OVER 🖲"))
        assert completed is not None
        assert completed["line_after_6over"]["low"] == 48

    def test_classify_top_order(self):
        s = MatchState()
        s.batting_order_index = 2
        assert s.classify_next_batsman() == "top_order"

    def test_classify_middle_order(self):
        s = MatchState()
        s.batting_order_index = 5
        assert s.classify_next_batsman() == "middle_order"

    def test_classify_tailender(self):
        s = MatchState()
        s.batting_order_index = 8
        assert s.classify_next_batsman() == "tailender"

    def test_total_balls_increments(self):
        s = MatchState()
        s.update(parse_line("0"))
        s.update(parse_line("1"))
        s.update(parse_line("4"))
        assert s.total_balls == 3

    def test_extras_dont_increment_balls(self):
        s = MatchState()
        s.update(parse_line("WIDE BALL ✔️✔️"))
        assert s.total_balls == 0   # wide = no legal delivery
        s.update(parse_line("NO BALL ✔️✔️"))
        assert s.total_balls == 0

    def test_snapshot_keys(self):
        s = MatchState()
        snap = s.snapshot()
        for key in ["match_id", "runs", "wickets", "total_balls",
                    "overs_display", "current_striker", "current_bowler",
                    "session_lines", "consecutive_dots", "current_rr"]:
            assert key in snap

    def test_reset_clears_state(self):
        s = MatchState()
        s.runs = 55
        s.wickets = 3
        old_id = s.match_id
        s.reset()
        assert s.runs == 0
        assert s.wickets == 0
        assert s.match_id != old_id   # new id generated


# ===========================================================================
# ENGINE TESTS
# ===========================================================================

class TestEngine:

    def _make_state(self, rr=6.0, dots=0):
        s = MatchState()
        s.current_rr = rr
        s.consecutive_dots = dots
        return s

    def _make_event(self, ev_type, runs=0, tier=None, br6=18, br20=96,
                    lb6=None, la6=None, lb20=None, la20=None):
        return {
            "event": ev_type,
            "runs":  runs,
            "next_batsman_tier": tier,
            "balls_remaining_6over":  br6,
            "balls_remaining_20over": br20,
            "line_before_6over":  lb6,
            "line_after_6over":   la6,
            "line_before_20over": lb20,
            "line_after_20over":  la20,
        }

    # --- fair_change tests ---

    def test_fair_change_dot(self):
        ev = self._make_event("dot")
        s  = self._make_state()
        assert engine.fair_change(ev, s) == -1.0

    def test_fair_change_one_run(self):
        ev = self._make_event("run", runs=1)
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 0.0

    def test_fair_change_two_runs(self):
        ev = self._make_event("run", runs=2)
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 1.0

    def test_fair_change_three_runs(self):
        ev = self._make_event("run", runs=3)
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 2.0

    def test_fair_change_four(self):
        ev = self._make_event("four")
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 3.0

    def test_fair_change_six(self):
        ev = self._make_event("six")
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 5.0

    def test_fair_change_wide(self):
        ev = self._make_event("wide")
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 1.0

    def test_fair_change_noball(self):
        ev = self._make_event("noball")
        s  = self._make_state()
        assert engine.fair_change(ev, s) == 2.0

    def test_fair_change_wicket_top_order(self):
        """top_order, balls_remaining=18, RR=9 → approx -3.97"""
        ev = self._make_event("wicket", tier="top_order", br6=18)
        s  = self._make_state(rr=9.0)
        fc = engine.fair_change(ev, s)
        # base=7, balls_factor=18/36=0.5, rr_factor=9/8.5≈1.059
        expected = -(7.0 * 0.5 * (9.0 / 8.5))
        assert fc == pytest.approx(expected, abs=0.01)

    def test_fair_change_wicket_tailender(self):
        ev = self._make_event("wicket", tier="tailender", br6=36)
        s  = self._make_state(rr=6.0)
        fc = engine.fair_change(ev, s)
        expected = -(2.5 * (36/36) * (6.0/8.5))
        assert fc == pytest.approx(expected, abs=0.01)

    def test_fair_change_wicket_middle_order(self):
        ev = self._make_event("wicket", tier="middle_order", br6=18)
        s  = self._make_state(rr=8.5)
        fc = engine.fair_change(ev, s)
        # base=5, bf=0.5, rr_f=1.0
        assert fc == pytest.approx(-2.5, abs=0.01)

    # --- detect_signal tests ---

    def _signal_test(self, ev_type, tier, lb_low, lb_high,
                     la_low, la_high, rr=6.0, dots=0, br6=18):
        s = self._make_state(rr=rr, dots=dots)
        ev = self._make_event(
            ev_type, tier=tier, br6=br6,
            lb6={"low": lb_low, "high": lb_high},
            la6={"low": la_low, "high": la_high},
        )
        return engine.detect_signal(ev, s)

    def test_signal_wicket_yes_over(self):
        """
        Wicket: line 47-48 → 46-47.
        before_mid=47.5, after_mid=46.5, actual=-1.
        fair ≈ -(5 * 0.5 * (6/8.5)) ≈ -1.76
        actual(-1) > fair(-1.76) → YES_OVER
        """
        sig = self._signal_test(
            "wicket", "middle_order",
            47, 48, 46, 47, rr=6.0, br6=18
        )
        assert sig is not None
        assert sig["signal"] == "YES_OVER"

    def test_signal_wicket_no_signal_on_exact_move(self):
        """
        Near-zero RR wicket → fair ≈ 0, line doesn't move → actual = 0.
        Both abs(fair) < 0.5 and abs(actual) < 0.5 → early exit → None.
        """
        s = MatchState()
        s.current_rr = 0.01   # near-zero → wicket_drop ≈ 0
        ev = self._make_event(
            "wicket", tier="tailender", br6=18,
            lb6={"low": 47, "high": 48},
            la6={"low": 47, "high": 48},   # no movement
        )
        sig = engine.detect_signal(ev, s)
        # fair ≈ 0, actual = 0 → both negligible → None
        assert sig is None

    def test_signal_four_not_under(self):
        """
        Four: line 47-48 → 49-50.
        fair=+3, actual=(49.5-47.5)=+2 → actual < fair → NOT_UNDER
        """
        sig = self._signal_test(
            "four", None,
            47, 48, 49, 50, rr=6.0
        )
        assert sig is not None
        assert sig["signal"] == "NOT_UNDER"

    def test_signal_four_yes_over(self):
        """
        Four: line 47-48 → 51-52.
        fair=+3, actual=+4 → actual > fair → YES_OVER
        """
        sig = self._signal_test(
            "four", None,
            47, 48, 51, 52, rr=6.0
        )
        assert sig is not None
        assert sig["signal"] == "YES_OVER"

    def test_signal_three_dots_triggers(self):
        """
        3 consecutive dots with small deviation STILL triggers (priority).
        dot: fair=-1, line moves -0.5 (before=47.5, after=47.0)
        actual=-0.5, deviation=abs(-1 - -0.5)=0.5 → below threshold BUT
        is_priority=True (dots>=3) → bypasses gate → YES_OVER
        (actual(-0.5) > fair(-1) → YES_OVER)
        """
        s = self._make_state(rr=6.0, dots=3)
        ev = self._make_event(
            "dot", br6=18,
            lb6={"low": 47, "high": 48},
            la6={"low": 46, "high": 47},
        )
        sig = engine.detect_signal(ev, s)
        assert sig is not None   # priority event bypasses deviation gate

    def test_signal_below_threshold_no_signal(self):
        """Small deviation on a non-priority event → no signal."""
        s = self._make_state(rr=6.0, dots=0)
        ev = self._make_event(
            "run", runs=1, br6=18,
            lb6={"low": 47, "high": 48},
            la6={"low": 47, "high": 48},   # no movement, fair=0 → deviation=0
        )
        sig = engine.detect_signal(ev, s)
        assert sig is None

    def test_signal_missing_lines_returns_none(self):
        """If line_before or line_after is None, skip that session."""
        s = self._make_state()
        ev = self._make_event("four", lb6=None, la6=None)
        sig = engine.detect_signal(ev, s)
        assert sig is None

    def test_signal_context_populated(self):
        """Signal dict has context sub-dict with score etc."""
        sig = self._signal_test("four", None, 47, 48, 49, 50)
        assert sig is not None
        ctx = sig["context"]
        assert "score" in ctx
        assert "overs" in ctx
        assert "RR" in ctx


# ===========================================================================
# DB TESTS
# ===========================================================================

class TestDB:

    @pytest.fixture(autouse=True)
    def tmp_db(self, tmp_path):
        """Each test gets its own temp SQLite file."""
        self.db_path = str(tmp_path / "test.db")
        db_module.init_db(self.db_path)
        yield

    def _sample_signal(self, signal_type="YES_OVER", session="6over"):
        return {
            "signal":        signal_type,
            "session":       session,
            "event":         "wicket",
            "line_before":   47.5,
            "line_after":    46.5,
            "fair_change":   -2.5,
            "actual_change": -1.0,
            "deviation":     1.5,
            "context": {
                "score":   "45/3",
                "overs":   "4.3",
                "RR":      6.0,
                "striker": "TEST PLAYER",
                "bowler":  "TEST BOWLER",
                "next_batsman_tier": "middle_order",
            },
        }

    def test_save_alert_returns_id(self):
        sig = self._sample_signal()
        aid = db_module.save_alert(sig, "MATCH01", self.db_path)
        assert isinstance(aid, int)
        assert aid >= 1

    def test_get_alert_by_id(self):
        sig = self._sample_signal()
        aid = db_module.save_alert(sig, "MATCH01", self.db_path)
        row = db_module.get_alert(aid, self.db_path)
        assert row is not None
        assert row["id"] == aid
        assert row["signal"] == "YES_OVER"

    def test_resolve_alert_updates_result(self):
        sig = self._sample_signal()
        aid = db_module.save_alert(sig, "MATCH01", self.db_path)
        db_module.resolve_alert(aid, "WIN", session_end_score=50, db_path=self.db_path)
        row = db_module.get_alert(aid, self.db_path)
        assert row["result"] == "WIN"
        assert row["session_end_score"] == 50

    def test_get_pending_alerts_filters_by_match_session(self):
        sig1 = self._sample_signal("YES_OVER",  "6over")
        sig2 = self._sample_signal("NOT_UNDER", "20over")
        db_module.save_alert(sig1, "MATCH01", self.db_path)
        db_module.save_alert(sig2, "MATCH01", self.db_path)

        pending_6  = db_module.get_pending_alerts("MATCH01", "6over",  self.db_path)
        pending_20 = db_module.get_pending_alerts("MATCH01", "20over", self.db_path)
        assert len(pending_6)  == 1
        assert len(pending_20) == 1

    def test_resolve_session_alerts_win_loss(self):
        # YES_OVER: WIN if actual > line_after(46.5), else LOSS
        sig_over  = self._sample_signal("YES_OVER",  "6over")   # line_after=46.5
        sig_under = self._sample_signal("NOT_UNDER", "6over")   # line_after=46.5

        id1 = db_module.save_alert(sig_over,  "MATCH01", self.db_path)
        id2 = db_module.save_alert(sig_under, "MATCH01", self.db_path)

        # actual=50 > 46.5 → YES_OVER=WIN, NOT_UNDER=LOSS
        summary = db_module.resolve_session_alerts("MATCH01", "6over", 50, self.db_path)
        assert summary["won"] == 1
        assert summary["lost"] == 1

        r1 = db_module.get_alert(id1, self.db_path)
        r2 = db_module.get_alert(id2, self.db_path)
        assert r1["result"] == "WIN"
        assert r2["result"] == "LOSS"

    def test_resolve_session_alerts_not_under_win(self):
        sig = self._sample_signal("NOT_UNDER", "6over")   # line_after=46.5
        aid = db_module.save_alert(sig, "MATCH01", self.db_path)
        # actual=40 < 46.5 → NOT_UNDER = WIN
        db_module.resolve_session_alerts("MATCH01", "6over", 40, self.db_path)
        row = db_module.get_alert(aid, self.db_path)
        assert row["result"] == "WIN"

    def test_get_today_alerts(self):
        sig = self._sample_signal()
        db_module.save_alert(sig, "MATCH01", self.db_path)
        today = db_module.get_today_alerts(self.db_path)
        assert len(today) >= 1

    def test_get_stats_aggregates(self):
        # Save 3 alerts, resolve all via session
        for _ in range(3):
            sig = self._sample_signal()
            db_module.save_alert(sig, "M1", self.db_path)

        db_module.resolve_session_alerts("M1", "6over", 55, self.db_path)

        stats = db_module.get_stats(db_path=self.db_path)
        assert stats["total"] == 3
        assert stats["won"] + stats["lost"] + stats["pending"] == 3
        assert 0.0 <= stats["accuracy"] <= 100.0

    def test_get_stats_by_session(self):
        sig6  = self._sample_signal("YES_OVER",  "6over")
        sig20 = self._sample_signal("NOT_UNDER", "20over")
        db_module.save_alert(sig6,  "M2", self.db_path)
        db_module.save_alert(sig20, "M2", self.db_path)

        stats = db_module.get_stats(db_path=self.db_path)
        assert stats["by_session"]["6over"]["total"]  == 1
        assert stats["by_session"]["20over"]["total"] == 1

    def test_get_stats_empty(self):
        stats = db_module.get_stats(db_path=self.db_path)
        assert stats["total"] == 0
        assert stats["accuracy"] == 0.0

    def test_manual_override_void(self):
        sig = self._sample_signal()
        aid = db_module.save_alert(sig, "MATCH01", self.db_path)
        db_module.resolve_alert(aid, "VOID", db_path=self.db_path)
        row = db_module.get_alert(aid, self.db_path)
        assert row["result"] == "VOID"
