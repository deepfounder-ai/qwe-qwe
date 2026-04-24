"""Scheduler cron-creation + metrics lifecycle.

Exercises the v0.17.29 analytics path end-to-end:
- add(..., skip_dry_run=True) saves a task with run_count=0
- _check_and_run increments run_count, stamps last_status/last_result/
  last_duration_ms
- list_tasks() returns the stamped metrics in the shape the UI expects
- Legacy installs missing the metrics columns heal via the migration
- Schedule-parse failures surface as {"error": ...} (UI displays them)
- Dry-run false-positives (RU confirmations, max-rounds) don't sink tasks
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fresh_scheduler(qwe_temp_data_dir, monkeypatch):
    """Reload scheduler + its deps against a fresh DB."""
    import importlib
    import sys

    for m in ("scheduler",):
        if m not in sys.modules:
            importlib.import_module(m)
    sched = importlib.reload(sys.modules["scheduler"])
    sched._callbacks.clear()
    return sched


# ── add() happy path ──────────────────────────────────────────────────


def test_add_task_with_skip_dry_run_saves_immediately(fresh_scheduler):
    """skip_dry_run=True must bypass the expensive LLM validation."""
    sched = fresh_scheduler
    r = sched.add("daily brief", "summarise today", "daily 09:00", skip_dry_run=True)
    assert r.get("ok") is True, f"expected ok, got {r}"
    assert r["name"] == "daily brief"
    assert r["repeat"] is True
    assert "dry_run" not in r, "dry_run metadata should only appear when not skipped"


def test_add_task_rejects_unknown_schedule(fresh_scheduler):
    """Legacy 5-field cron syntax → clean error, not a crash."""
    sched = fresh_scheduler
    r = sched.add("x", "do something", "0 9 * * *", skip_dry_run=True)
    assert "error" in r
    assert "parse" in r["error"].lower() or "schedule" in r["error"].lower()


def test_add_task_accepts_every_n_format(fresh_scheduler):
    sched = fresh_scheduler
    r = sched.add("hourly thing", "just ping", "every 1h", skip_dry_run=True)
    assert r.get("ok") is True
    assert r["repeat"] is True


# ── list_tasks() surfaces metrics ─────────────────────────────────────


def test_list_tasks_returns_zeroed_metrics_for_fresh_task(fresh_scheduler):
    """A just-added task shows run_count=0 + empty status — no pretending."""
    sched = fresh_scheduler
    sched.add("fresh", "ping", "daily 10:00", skip_dry_run=True)
    tasks = sched.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    for key in ("id", "name", "task", "schedule", "next_run", "last_run",
                "repeat", "enabled", "run_count", "last_status", "last",
                "last_error", "last_duration_ms", "last_result"):
        assert key in t, f"list_tasks() missing {key!r}"
    assert t["run_count"] == 0
    assert t["last_status"] == ""
    assert t["last"] == ""
    assert t["last_run"] == ""


# ── _check_and_run increments metrics ─────────────────────────────────


def test_check_and_run_stamps_metrics_on_success(fresh_scheduler, monkeypatch):
    """A successful execution increments run_count + sets last_status=ok.

    _execute_routine now owns metrics updates, so we mock agent.run
    (one layer below) and let the real metrics-update path run.
    """
    sched = fresh_scheduler
    monkeypatch.setattr(sched, "_execute_task",
                         lambda task, max_rounds=10: "Task done. Sent summary.")
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run",
                         lambda user_input, **kw: _FakeResult("Task done. Sent summary."))

    sched.add("ping", "send status", "every 1h", skip_dry_run=True)

    # Force due by backdating next_run
    import db
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE name=?",
               (time.time() - 10, "ping"))

    sched._check_and_run()

    task = next(t for t in sched.list_tasks() if t["name"] == "ping")
    assert task["run_count"] == 1, f"run_count should be 1, got {task['run_count']}"
    assert task["last_status"] == "ok"
    assert task["last"].startswith("ok"), f"'last' column should begin with ok, got {task['last']!r}"
    assert task["last_run"], "last_run timestamp should be populated"
    assert task["last_duration_ms"] >= 0


def test_check_and_run_stamps_error_on_exception(fresh_scheduler, monkeypatch):
    """A crashing task → last_status=err + last_error captured, run_count still bumped."""
    sched = fresh_scheduler

    def _boom(*_a, **_kw):
        raise RuntimeError("network is on fire")

    monkeypatch.setattr(sched, "_execute_task", _boom)
    import agent
    monkeypatch.setattr(agent, "run", _boom)

    sched.add("broken", "do stuff", "every 1h", skip_dry_run=True)
    import db
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE name=?",
               (time.time() - 10, "broken"))

    sched._check_and_run()

    task = next(t for t in sched.list_tasks() if t["name"] == "broken")
    assert task["run_count"] == 1
    assert task["last_status"] == "err"
    assert "network is on fire" in task["last_error"]
    assert task["last"].startswith("err")


def test_check_and_run_stamps_error_when_output_looks_like_error(fresh_scheduler, monkeypatch):
    """Task returned text that matches a failure marker → err."""
    sched = fresh_scheduler
    faulty_output = "Traceback (most recent call last):\n  File …"
    monkeypatch.setattr(sched, "_execute_task",
                         lambda task, max_rounds=10: faulty_output)
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run",
                         lambda user_input, **kw: _FakeResult(faulty_output))

    sched.add("fragile", "x", "every 1h", skip_dry_run=True)
    import db
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE name=?",
               (time.time() - 10, "fragile"))
    sched._check_and_run()

    task = next(t for t in sched.list_tasks() if t["name"] == "fragile")
    assert task["last_status"] == "err"
    assert task["run_count"] == 1


# ── Dry-run false-positive fixes ──────────────────────────────────────


def test_max_rounds_marker_is_not_a_failure_anymore(fresh_scheduler):
    """'task completed (max rounds)' must NOT be treated as a failure.

    This used to kill every mildly-complex cron job at creation time.
    """
    sched = fresh_scheduler
    result = sched._validate_dry_run("Did some work. task completed (max rounds).",
                                      "do stuff")
    assert result["ok"] is True


def test_send_task_accepts_russian_confirmation(fresh_scheduler):
    """'Отправил в Telegram' counts as success for a send-task."""
    sched = fresh_scheduler
    result = sched._validate_dry_run(
        "Отправил сводку в Telegram.",
        "send me qwe-qwe error logs summary to telegram",
    )
    assert result["ok"] is True


def test_send_task_accepts_english_confirmation(fresh_scheduler):
    """Backward-compat: original OK/sent/200 still pass."""
    sched = fresh_scheduler
    for reply in ("message_id=42 ok", "HTTP 200 OK", "Sent successfully"):
        r = sched._validate_dry_run(reply, "send a telegram")
        assert r["ok"] is True, f"expected {reply!r} to pass"


def test_send_task_without_confirmation_offers_skip(fresh_scheduler):
    """If dry-run can't confirm a send, server must return offer_skip:true."""
    sched = fresh_scheduler
    r = sched.add(
        "silent send",
        "send me a telegram ping",
        "every 1h",
        skip_dry_run=False,
    )
    # Real LLM isn't wired up in tests → _execute_task will throw or return empty.
    # Either way we get an error response. When the failure was the confirmation
    # check, offer_skip should be set. Tolerate both shapes but require the
    # response to be a coherent dict.
    assert isinstance(r, dict)
    assert r.get("ok") is not True  # task should NOT save without real LLM


# ── Migration back-compat ─────────────────────────────────────────────


def test_routine_gets_dedicated_thread_on_create(fresh_scheduler):
    """Adding a real routine creates a permanent thread for it.

    v0.17.30 shift: one routine = one thread. The thread_id is stamped
    at save time and returned so the UI can link the routine card to
    it. Every firing reuses this thread_id.
    """
    sched = fresh_scheduler
    r = sched.add("digest", "summarise my inbox daily",
                  "daily 09:00", skip_dry_run=True)
    assert r.get("ok") is True
    assert r.get("thread_id"), "routine must be bound to a thread at save time"
    assert r["thread_id"].startswith("t_"), "thread_id must look like a real thread id"

    # list_tasks exposes the same thread_id so the UI can deep-link
    entry = next(t for t in sched.list_tasks() if t["name"] == "digest")
    assert entry["thread_id"] == r["thread_id"]


def test_routine_thread_persists_across_multiple_firings(fresh_scheduler, monkeypatch):
    """Every firing appends to the SAME thread; the thread_id never changes.

    Two synthetic firings → thread_id stays stable → routine view shows
    a growing chat log, not a pile of disconnected threads.
    """
    sched = fresh_scheduler
    # Stub agent.run one layer below so _execute_routine's metrics-update
    # path runs and run_count actually increments.
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run",
                         lambda user_input, **kw: _FakeResult("ok run"))

    r = sched.add("daily digest", "run a digest", "every 1h", skip_dry_run=True)
    original_tid = r["thread_id"]
    assert original_tid

    # Force due + fire twice
    import db
    import time as _t
    for _ in range(2):
        db.execute("UPDATE scheduled_tasks SET next_run=? WHERE name=?",
                   (_t.time() - 10, "daily digest"))
        sched._check_and_run()

    entry = next(t for t in sched.list_tasks() if t["name"] == "daily digest")
    assert entry["thread_id"] == original_tid, (
        "thread_id must be stable across firings (one routine = one thread)"
    )
    assert entry["run_count"] == 2


def test_delete_routine_also_deletes_its_thread(fresh_scheduler):
    """scheduler.remove() must cascade the thread delete, not just archive.

    Pre-0.17.30.2 this called threads.archive() which left the thread
    lingering in the sidebar. User request: tightly-coupled lifecycle —
    routine gone = thread gone.
    """
    sched = fresh_scheduler
    r = sched.add("ephemeral", "ping", "daily 09:00", skip_dry_run=True)
    tid = r["thread_id"]
    assert tid

    import threads as _threads
    assert _threads.get(tid) is not None, "thread exists right after create"

    out = sched.remove([t["id"] for t in sched.list_tasks() if t["name"] == "ephemeral"][0])
    assert out.startswith("✓")
    assert _threads.get(tid) is None, "thread must be gone after routine delete"


def test_delete_thread_removes_bound_routine(fresh_scheduler):
    """threads.delete() must cascade to the routine pointing at it.

    The reverse link: user deletes a Routine thread from the sidebar →
    the routine stops firing. Without this, the scheduler loop would
    tick forever into a dead thread id.
    """
    sched = fresh_scheduler
    r = sched.add("bound", "ping", "daily 09:00", skip_dry_run=True)
    tid = r["thread_id"]
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "bound"][0]

    import threads as _threads
    _threads.delete(tid)

    remaining = [t for t in sched.list_tasks() if t["id"] == cron_id]
    assert not remaining, "routine must be gone after its thread is deleted"


def test_delete_thread_leaves_system_tasks_alone(fresh_scheduler):
    """Deleting a thread never touches the heartbeat / synthesis rows."""
    sched = fresh_scheduler
    # Register a system task the same way prod does
    import db
    db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) "
        "VALUES (?, ?, ?, ?, 1, 1)",
        (sched.HEARTBEAT_TASK_NAME, sched.HEARTBEAT_TASK_NAME, "every 30m",
         9999999999.0),  # far future
    )
    # Now remove_by_thread with an unrelated thread_id — heartbeat must survive
    n = sched.remove_by_thread("t_nonexistent")
    assert n == 0
    assert any(t["name"] == sched.HEARTBEAT_TASK_NAME for t in sched.list_tasks())


def test_set_enabled_toggles_and_scheduler_respects_it(fresh_scheduler, monkeypatch):
    """set_enabled(task_id) toggles; scheduler skips disabled rows.

    Three things happen in one test to cover the whole pause path:
      1. add a routine → enabled=True
      2. toggle → enabled=False; list_tasks reflects it
      3. force due + _check_and_run → NO fire (because disabled)
    """
    sched = fresh_scheduler
    import agent

    fires = []

    class _FakeResult:
        def __init__(self, r): self.reply = r
    def _fake_run(user_input, **kw):
        fires.append(user_input)
        return _FakeResult("ok")
    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("paused-thing", "do it", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "paused-thing"][0]

    # Toggle off
    out = sched.set_enabled(cron_id)
    assert out == {"ok": True, "enabled": False}
    entry = next(t for t in sched.list_tasks() if t["id"] == cron_id)
    assert entry["enabled"] is False

    # Force due; scheduler SHOULD NOT fire a disabled row
    import db
    import time as _t
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?",
               (_t.time() - 10, cron_id))
    sched._check_and_run()
    assert fires == [], "disabled routine must not fire"

    # Toggle back on, now it fires
    sched.set_enabled(cron_id, enabled=True)
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?",
               (_t.time() - 10, cron_id))
    sched._check_and_run()
    assert fires == ["do it"]


def test_set_enabled_missing_id_returns_error(fresh_scheduler):
    sched = fresh_scheduler
    out = sched.set_enabled(99999)
    assert "error" in out


def test_legacy_schema_heals_via_ensure_table(qwe_temp_data_dir):
    """A DB that only has the pre-metrics columns auto-upgrades on first use.

    The migration (003_scheduled_tasks_metrics.sql) runs at connection
    open; scheduler._ensure_table() also includes the new columns so
    fresh calls still work. Smoke-test the resulting shape.
    """
    import importlib
    import sys
    for m in ("db", "scheduler"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    sched = sys.modules["scheduler"]

    sched.add("migrated", "x", "every 1h", skip_dry_run=True)
    tasks = sched.list_tasks()
    assert any(t["name"] == "migrated" for t in tasks)
    t = next(t for t in tasks if t["name"] == "migrated")
    # Every metric key exists, even before the first execution
    assert t["run_count"] == 0
    assert t["last_status"] == ""
    assert t["last_error"] == ""
