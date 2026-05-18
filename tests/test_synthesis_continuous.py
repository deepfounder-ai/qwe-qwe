"""Continuous trickle synthesis: small batches every N minutes.

Replaces the once-a-day-at-03:00 model with a trickle that runs every
``synthesis_continuous_interval_min`` minutes (default 15). New memory
becomes searchable within minutes, not 24 hours. The nightly batch
remains as a catch-up for whatever the trickle missed.

Tests cover:
  - ``run_synthesis`` accepts ``max_items`` override
  - ``run_continuous`` respects the on/off setting and uses the
    continuous max
  - ``_register_synthesis_continuous`` inserts the scheduler row with
    the right cadence
  - Re-registering updates schedule when interval changes (idempotent)
  - ``_execute_routine`` dispatches the new system task name to
    ``synthesis.run_continuous``
"""
from __future__ import annotations

from unittest.mock import patch

import db
import synthesis


# ── run_synthesis max_items override ────────────────────────────────────────


def test_run_synthesis_accepts_max_items_override(qwe_temp_data_dir, monkeypatch):
    """When max_items is passed, it overrides the synthesis_max_per_run setting.
    Continuous mode relies on this — without it, every continuous fire
    would process up to 50 items per batch (matching nightly), defeating
    the trickle.
    """
    captured = {}

    def fake_pending(limit):
        captured["limit"] = limit
        return {}

    monkeypatch.setattr(synthesis.memory, "get_pending_synthesis", fake_pending)
    synthesis.run_synthesis(max_items=3)
    assert captured["limit"] == 3


def test_run_synthesis_falls_back_to_config_when_no_override(qwe_temp_data_dir,
                                                              monkeypatch):
    """Caller passes None → reads synthesis_max_per_run from config."""
    captured = {}

    def fake_pending(limit):
        captured["limit"] = limit
        return {}

    monkeypatch.setattr(synthesis.memory, "get_pending_synthesis", fake_pending)
    # Set a non-default value to confirm it's read
    db.kv_set("setting:synthesis_max_per_run", "17")
    synthesis.run_synthesis()
    assert captured["limit"] == 17


def test_run_synthesis_respects_master_enabled_flag(qwe_temp_data_dir):
    """synthesis_enabled=0 short-circuits both nightly and continuous."""
    db.kv_set("setting:synthesis_enabled", "0")
    result = synthesis.run_synthesis()
    assert "disabled" in result.lower()


# ── run_continuous wrapper ──────────────────────────────────────────────────


def test_run_continuous_uses_small_batch(qwe_temp_data_dir, monkeypatch):
    """run_continuous reads synthesis_continuous_max_per_run, not nightly's
    bigger cap."""
    captured = {}

    def fake_pending(limit):
        captured["limit"] = limit
        return {}

    monkeypatch.setattr(synthesis.memory, "get_pending_synthesis", fake_pending)
    db.kv_set("setting:synthesis_continuous_max_per_run", "4")
    synthesis.run_continuous()
    assert captured["limit"] == 4


def test_run_continuous_respects_its_own_enable_flag(qwe_temp_data_dir):
    """Disabling JUST the continuous mode shouldn't disable nightly. Returns
    a distinct status string so logs are clear."""
    db.kv_set("setting:synthesis_continuous_enabled", "0")
    result = synthesis.run_continuous()
    assert "Continuous" in result and "disabled" in result.lower()


def test_run_continuous_inherits_master_disable(qwe_temp_data_dir):
    """When synthesis_enabled=0 (the master switch), run_continuous still
    returns a disabled message via run_synthesis's check. Both off means off."""
    db.kv_set("setting:synthesis_enabled", "0")
    db.kv_set("setting:synthesis_continuous_enabled", "1")
    result = synthesis.run_continuous()
    assert "disabled" in result.lower()


# ── Scheduler registration ──────────────────────────────────────────────────


def test_register_synthesis_continuous_inserts_row(qwe_temp_data_dir):
    """The cron row lands in scheduled_tasks with the right name and schedule."""
    import scheduler
    scheduler._ensure_table()
    db.kv_set("setting:synthesis_enabled", "1")
    db.kv_set("setting:synthesis_continuous_enabled", "1")
    db.kv_set("setting:synthesis_continuous_interval_min", "10")

    scheduler._register_synthesis_continuous()

    row = db.fetchone(
        "SELECT name, task, schedule, enabled, repeat FROM scheduled_tasks "
        "WHERE name=?",
        (scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME,)
    )
    assert row is not None
    assert row[0] == "__synthesis_continuous__"
    assert row[1] == "__synthesis_continuous__"
    assert row[2] == "every 10m"
    assert row[3] == 1  # enabled
    assert row[4] == 1  # repeat


def test_register_synthesis_continuous_idempotent(qwe_temp_data_dir):
    """Calling registration twice with same settings → still one row."""
    import scheduler
    scheduler._ensure_table()
    db.kv_set("setting:synthesis_enabled", "1")
    db.kv_set("setting:synthesis_continuous_enabled", "1")

    scheduler._register_synthesis_continuous()
    scheduler._register_synthesis_continuous()

    rows = db.fetchall(
        "SELECT id FROM scheduled_tasks WHERE name=?",
        (scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME,)
    )
    assert len(rows) == 1


def test_register_synthesis_continuous_updates_schedule_on_interval_change(
        qwe_temp_data_dir):
    """If the user changes the interval setting, re-registering rewrites
    schedule + next_run so the cron starts firing at the new cadence."""
    import scheduler
    scheduler._ensure_table()
    db.kv_set("setting:synthesis_enabled", "1")
    db.kv_set("setting:synthesis_continuous_enabled", "1")

    db.kv_set("setting:synthesis_continuous_interval_min", "15")
    scheduler._register_synthesis_continuous()

    db.kv_set("setting:synthesis_continuous_interval_min", "30")
    scheduler._register_synthesis_continuous()

    row = db.fetchone(
        "SELECT schedule FROM scheduled_tasks WHERE name=?",
        (scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME,)
    )
    assert row[0] == "every 30m"


def test_register_synthesis_continuous_skips_when_disabled(qwe_temp_data_dir):
    """If synthesis_continuous_enabled=0, no row is created."""
    import scheduler
    scheduler._ensure_table()
    db.kv_set("setting:synthesis_enabled", "1")
    db.kv_set("setting:synthesis_continuous_enabled", "0")

    scheduler._register_synthesis_continuous()

    rows = db.fetchall(
        "SELECT id FROM scheduled_tasks WHERE name=?",
        (scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME,)
    )
    assert len(rows) == 0


def test_register_synthesis_continuous_skips_when_master_disabled(
        qwe_temp_data_dir):
    """If synthesis_enabled=0 (master), no continuous registration either."""
    import scheduler
    scheduler._ensure_table()
    db.kv_set("setting:synthesis_enabled", "0")
    db.kv_set("setting:synthesis_continuous_enabled", "1")

    scheduler._register_synthesis_continuous()

    rows = db.fetchall(
        "SELECT id FROM scheduled_tasks WHERE name=?",
        (scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME,)
    )
    assert len(rows) == 0


def test_register_synthesis_continuous_default_interval_is_15(qwe_temp_data_dir):
    """Without setting an interval explicitly, default is 15 minutes."""
    import scheduler
    scheduler._ensure_table()
    db.kv_set("setting:synthesis_enabled", "1")
    db.kv_set("setting:synthesis_continuous_enabled", "1")
    # Don't set interval — should fall back to default

    scheduler._register_synthesis_continuous()

    row = db.fetchone(
        "SELECT schedule FROM scheduled_tasks WHERE name=?",
        (scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME,)
    )
    assert row[0] == "every 15m"


# ── _execute_routine dispatch ───────────────────────────────────────────────


def test_execute_routine_dispatches_continuous_synthesis(qwe_temp_data_dir):
    """When the scheduler fires the __synthesis_continuous__ task,
    _execute_routine routes to synthesis.run_continuous() — not the
    LLM-based generic task handler.
    """
    import scheduler
    with patch.object(synthesis, "run_continuous",
                      return_value="continuous result") as mock_cont:
        # _execute_task is the low-level dispatcher used by the scheduler loop
        result = scheduler._execute_task(scheduler.SYNTHESIS_CONTINUOUS_TASK_NAME)

    mock_cont.assert_called_once()
    assert result == "continuous result"


def test_execute_routine_dispatches_nightly_synthesis(qwe_temp_data_dir):
    """The nightly __synthesis__ task still routes to run_synthesis (no
    continuous flag) — regression check that we didn't break the existing
    cron path."""
    import scheduler
    with patch.object(synthesis, "run_synthesis",
                      return_value="nightly result") as mock_night:
        result = scheduler._execute_task(scheduler.SYNTHESIS_TASK_NAME)

    mock_night.assert_called_once_with()
    assert result == "nightly result"
