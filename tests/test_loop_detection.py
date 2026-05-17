"""Tests for multi-period loop detection in agent_loop._detect_loop_period()."""
from agent_loop import _detect_loop_period


class TestDetectLoopPeriod:
    def test_period_1_detected(self):
        # A → A
        assert _detect_loop_period(["a", "a"]) == 1

    def test_period_1_with_history(self):
        assert _detect_loop_period(["x", "y", "a", "a"]) == 1

    def test_period_2_detected(self):
        # A → B → A → B
        assert _detect_loop_period(["a", "b", "a", "b"]) == 2

    def test_period_3_detected(self):
        # A → B → C → A → B → C
        assert _detect_loop_period(["a", "b", "c", "a", "b", "c"]) == 3

    def test_no_repeat(self):
        assert _detect_loop_period(["a", "b", "c", "d"]) is None

    def test_too_short_for_period_2(self):
        # Only 3 items — not enough for period-2 (needs 4)
        assert _detect_loop_period(["a", "b", "a"]) is None

    def test_too_short_for_any(self):
        assert _detect_loop_period(["a"]) is None
        assert _detect_loop_period([]) is None

    def test_period_1_preferred_over_period_2(self):
        # A → A → A → A could match period-1 or period-2; period-1 should win
        assert _detect_loop_period(["a", "a", "a", "a"]) == 1

    def test_period_2_not_confused_with_period_1(self):
        # A → B → A → B — only period-2, not period-1
        sigs = ["a", "b", "a", "b"]
        period = _detect_loop_period(sigs)
        assert period == 2
        # Verify period-1 wouldn't match (last two differ)
        assert sigs[-1] != sigs[-2]
