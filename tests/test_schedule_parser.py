"""Schedule-DSL parser coverage.

v0.17.30 added weekly day-of-week schedules ("mon,wed,fri 09:00",
"weekdays 08:30", "выходные 10:00") and every-N-days ("every 2 days
09:00") because users asked for "через день в X:XX" and "по будням".

These tests pin the parsed (next_run_ts, repeat_interval) tuples
against synthetic ``now`` values so day-of-week math is deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture
def sched(qwe_temp_data_dir):
    import importlib
    import sys
    if "scheduler" not in sys.modules:
        importlib.import_module("scheduler")
    return importlib.reload(sys.modules["scheduler"])


# ── Legacy patterns still work ────────────────────────────────────────


def test_parse_in_5m_is_one_off(sched, monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 1_000_000.0)
    monkeypatch.setattr(sched, "time", _t)
    ts, interval = sched._parse_schedule("in 5m")
    assert ts == pytest.approx(1_000_000.0 + 300)
    assert interval == 0


def test_parse_every_30m_repeats(sched):
    ts, interval = sched._parse_schedule("every 30m")
    assert interval == 1800
    assert ts > 0


def test_parse_daily_09_00(sched):
    ts, interval = sched._parse_schedule("daily 09:00")
    assert interval == 86400
    # Must project back through the SAME timezone the parser used —
    # _tz() may be UTC, fixed offset, or IANA zone depending on config.
    tz = sched._tz()
    assert datetime.fromtimestamp(ts, tz=tz).hour == 9


# ── New: every N days at HH:MM ────────────────────────────────────────


def test_parse_every_2_days(sched):
    ts, interval = sched._parse_schedule("every 2 days 09:00")
    assert interval == 2 * 86400
    assert ts > 0


def test_parse_every_3_days(sched):
    ts, interval = sched._parse_schedule("every 3 days 14:30")
    assert interval == 3 * 86400
    # Next fire wall-clock (in parser's timezone) should be at 14:30
    dt = datetime.fromtimestamp(ts, tz=sched._tz())
    assert (dt.hour, dt.minute) == (14, 30)


# ── New: weekly by day-of-week ────────────────────────────────────────


@pytest.mark.parametrize("dsl,expected_days", [
    ("mon 09:00", {0}),
    ("tue 09:00", {1}),
    ("sun 09:00", {6}),
    ("mon,wed,fri 09:00", {0, 2, 4}),
    ("sat,sun 10:00", {5, 6}),
    ("weekdays 08:30", {0, 1, 2, 3, 4}),
    ("weekends 12:00", {5, 6}),
    ("будни 09:00", {0, 1, 2, 3, 4}),
    ("выходные 12:00", {5, 6}),
    ("пн,ср,пт 09:00", {0, 2, 4}),
])
def test_parse_weekly(sched, dsl, expected_days):
    """Each weekly spec parses with repeat=7d and lands on the right weekday."""
    ts, interval = sched._parse_schedule(dsl)
    assert ts is not None, f"parser rejected {dsl!r}"
    assert interval == 7 * 86400, f"expected weekly repeat for {dsl!r}, got {interval}"
    fire_wd = datetime.fromtimestamp(ts, tz=sched._tz()).weekday()
    assert fire_wd in expected_days, (
        f"{dsl!r} fires on weekday {fire_wd}, expected one of {expected_days}"
    )


def test_parse_weekly_respects_time(sched):
    ts, _ = sched._parse_schedule("mon,wed,fri 14:30")
    dt = datetime.fromtimestamp(ts, tz=sched._tz())
    assert (dt.hour, dt.minute) == (14, 30)


def test_parse_weekly_next_run_is_future(sched):
    """Parser must pick the next matching weekday >= now, never a past slot."""
    import time as _t
    ts, _ = sched._parse_schedule("weekdays 09:00")
    assert ts > _t.time()


# ── Invalid inputs cleanly reject ─────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "0 9 * * *",            # 5-field cron, wrong grammar
    "every fortnight",      # no support
    "mon-fri 09:00",        # range syntax, not supported
    "mon at 9",             # wrong time format
    "",                     # empty
])
def test_parse_rejects_garbage(sched, bad):
    ts, _ = sched._parse_schedule(bad)
    assert ts is None, f"parser should reject {bad!r} but returned ts={ts}"


# ── Post-fire reschedule picks next real slot, not now+interval ──────


def test_weekly_reschedule_hops_to_next_matching_day(sched, monkeypatch):
    """After firing on a Mon in 'mon,wed,fri 09:00', next_run must be Wed
    — not 7 days later. The pre-0.17.30 bug was adding the raw interval.
    """
    import time as _t

    # Pin "now" to a Monday at 09:05 (just after first fire)
    mon_9_05 = datetime(2026, 4, 20, 9, 5, 0,
                         tzinfo=timezone.utc).timestamp()  # Mon
    monkeypatch.setattr(sched, "time", _Clock(mon_9_05))

    ts, _ = sched._parse_schedule("mon,wed,fri 09:00")
    fire_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Expect Wednesday (2 days later), NOT next Monday
    assert fire_dt.weekday() == 2, (
        f"expected Wednesday, got weekday {fire_dt.weekday()} ({fire_dt})"
    )


class _Clock:
    """Minimal stand-in for the ``time`` module used by `_parse_schedule`."""
    def __init__(self, fixed: float):
        self._t = fixed
    def time(self) -> float:
        return self._t
