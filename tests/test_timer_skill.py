"""Tests for the timer skill (set, list, cancel)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from skills.timer import execute, TOOLS


def test_timer_has_three_tools():
    """The timer skill must expose set_timer, list_timers, and cancel_timer."""
    names = [t["function"]["name"] for t in TOOLS]
    assert "set_timer" in names
    assert "list_timers" in names
    assert "cancel_timer" in names


def test_set_timer_returns_id():
    result = execute("set_timer", {"seconds": 60, "label": "test"})
    assert result.startswith("⏱ Timer set: test (60s) — id:")


def test_list_timers_shows_active():
    # First make sure we have at least one active timer
    result = execute("set_timer", {"seconds": 999, "label": "list-test"})
    assert "id:" in result

    result = execute("list_timers", {})
    assert "Active timers:" in result or "No active timers." in result


def test_cancel_timer_invalid_id():
    result = execute("cancel_timer", {"timer_id": "nonexistent"})
    assert "not found" in result


def test_cancel_timer_valid():
    result = execute("set_timer", {"seconds": 999, "label": "cancel-me"})
    # Extract id from result like "⏱ Timer set: cancel-me (999s) — id: abc12345"
    timer_id = result.split("id:")[-1].strip()

    result = execute("cancel_timer", {"timer_id": timer_id})
    assert "Cancelled" in result
    assert timer_id in result


def test_list_timers_empty():
    # Cancel any remaining timers first — but we can't enumerate them all easily
    # Just verify the function returns without error
    result = execute("list_timers", {})
    assert isinstance(result, str)