"""Per-fire routine run history + missed-run detection.

v0.17.32 added a routine_runs table so users can see whether a routine
actually fired vs was skipped because the server was offline at the
scheduled time. Before this, ``last_run`` only reflected the most
recent attempt — a weekly routine that failed to fire three weeks
running looked identical to one that had fired yesterday.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def sched(qwe_temp_data_dir):
    import importlib
    import sys
    for m in ("config", "db", "scheduler"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    s = sys.modules["scheduler"]
    s._callbacks.clear()
    s._ROUTINE_FIRE_LOCKS.clear()
    return s


# ── Per-fire logging ─────────────────────────────────────────────────


def test_successful_fire_logs_ok_row(sched, monkeypatch):
    """A successful agent.run records a routine_runs row with status=ok."""
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run",
                         lambda u, **kw: _FakeResult("all good"))

    r = sched.add("alpha", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "alpha"][0]
    thread_id = r["thread_id"]
    sched._execute_routine("ping", "alpha", cron_id, thread_id)

    runs = sched.list_runs(cron_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["duration_ms"] >= 0


def test_crashed_fire_logs_err_row(sched, monkeypatch):
    import agent
    monkeypatch.setattr(agent, "run", lambda u, **kw: (_ for _ in ()).throw(RuntimeError("nope")))

    r = sched.add("crashy", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "crashy"][0]
    sched._execute_routine("ping", "crashy", cron_id, r["thread_id"])

    runs = sched.list_runs(cron_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "err"
    assert "nope" in runs[0]["error"]


def test_concurrent_fire_logs_skipped(sched, monkeypatch):
    """When the per-thread lock is held, the second call logs status=skipped."""
    import agent
    import threading as _th

    # Long-running fake agent.run to keep the lock held while we fire a second
    hold = _th.Event()

    class _FakeResult:
        def __init__(self, r): self.reply = r

    def _slow(u, **kw):
        hold.wait(timeout=5)
        return _FakeResult("ok")
    monkeypatch.setattr(agent, "run", _slow)

    r = sched.add("busy", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "busy"][0]
    tid = r["thread_id"]

    # Start the slow fire
    th = _th.Thread(target=lambda: sched._execute_routine("ping", "busy", cron_id, tid))
    th.start()
    # Wait a tick for the lock to actually be acquired
    time.sleep(0.1)

    # Second fire should log skipped and return ""
    out = sched._execute_routine("ping", "busy", cron_id, tid)
    assert out == ""

    hold.set()
    th.join(timeout=10)

    runs = sched.list_runs(cron_id)
    statuses = [r["status"] for r in runs]
    assert "skipped" in statuses
    assert "ok" in statuses


# ── Missed-run detection ─────────────────────────────────────────────


def test_detect_missed_runs_first_boot_is_noop(sched):
    """On the very first boot (no stamp yet), don't invent historical
    misses — just stamp and move on."""
    import db
    db.kv_set("scheduler:last_check", "")  # clear
    n = sched.detect_missed_runs()
    assert n == 0


def test_detect_missed_logs_rows_for_offline_gap(sched, monkeypatch):
    """When next_run is well before last_check's future → server was
    offline across scheduled slots → log each as missed."""
    import db
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run", lambda u, **kw: _FakeResult("ok"))

    sched.add("hourly", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "hourly"][0]

    now = time.time()
    # Simulate: last_check was 5h30m ago (server was offline that long).
    # next_run is 30m from now — 5 hourly slots were missed in the gap.
    db.kv_set("scheduler:last_check", str(now - 5.5 * 3600))
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?",
               (now + 1800, cron_id))

    n = sched.detect_missed_runs()
    # Expect the 5 hourly slots that fell in the gap
    assert n == 5
    runs = sched.list_runs(cron_id, limit=20)
    assert all(r["status"] == "missed" for r in runs)
    assert len(runs) == 5


def test_detect_missed_caps_at_ten(sched, monkeypatch):
    """A very long outage doesn't spam hundreds of rows — capped at 10
    per routine so the user doesn't have to scroll forever."""
    import db
    sched.add("frequent", "ping", "every 10m", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "frequent"][0]

    now = time.time()
    # 24 hours offline → 144 missed 10-min slots → should cap at 10
    db.kv_set("scheduler:last_check", str(now - 24 * 3600))
    db.execute("UPDATE scheduled_tasks SET next_run=? WHERE id=?",
               (now + 60, cron_id))

    sched.detect_missed_runs()
    runs = sched.list_runs(cron_id, limit=50)
    assert len(runs) == 10
    assert all(r["status"] == "missed" for r in runs)


def test_detect_missed_skips_non_routines(sched, monkeypatch):
    """Heartbeat / synthesis are infrastructure; detect_missed should
    leave them alone — they self-correct on the next tick."""
    import db
    # Simulate heartbeat registered with past next_run
    hb_next = time.time() - 3600  # 1h late
    db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) "
        "VALUES (?, ?, ?, ?, 1, 1)",
        (sched.HEARTBEAT_TASK_NAME, sched.HEARTBEAT_TASK_NAME, "every 30m", hb_next),
    )
    db.kv_set("scheduler:last_check", str(time.time() - 3 * 3600))

    sched.detect_missed_runs()
    hb_id = db.fetchone("SELECT id FROM scheduled_tasks WHERE name=?",
                        (sched.HEARTBEAT_TASK_NAME,))[0]
    assert sched.list_runs(hb_id) == []  # no missed rows for system tasks


# ── list_tasks surfaces recent breakdown ─────────────────────────────


def test_routine_fire_does_not_persist_fake_user_message(sched, monkeypatch):
    """Each fire must NOT inflate the thread with a fake 'user typed the
    task' row. Only the assistant reply persists.

    Background: users were complaining that scheduled fires made the
    routine thread look like they had typed the same task 50 times.
    Phase: agent.run(save_user_msg=False) skips the user-row save; the
    LLM still sees the task in its messages array via _build_messages.
    """
    import agent
    import db

    # Fake agent.run that mimics ONLY the assistant-save side effect —
    # i.e. honours save_user_msg=False by intentionally NOT saving a user.
    class _FakeResult:
        def __init__(self, r): self.reply = r

    captured = {"save_user_msg": None}

    def _fake_run(user_input, thread_id=None, source="cli",
                   save_user_msg=True, **kw):
        captured["save_user_msg"] = save_user_msg
        # Mimic real agent.run: persist only what the flag tells us to
        if save_user_msg:
            db.save_message("user", user_input, thread_id=thread_id)
        db.save_message("assistant", "ok done", thread_id=thread_id)
        return _FakeResult("ok done")

    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("clean", "summarise yesterday", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "clean"][0]
    tid = r["thread_id"]

    sched._execute_routine("summarise yesterday", "clean", cron_id, tid)

    # The routine path passed save_user_msg=False
    assert captured["save_user_msg"] is False, (
        "_execute_routine must call agent.run(save_user_msg=False)"
    )

    # DB has only the assistant turn — no fake user message
    rows = db.fetchall(
        "SELECT role FROM messages WHERE thread_id=? ORDER BY id",
        (tid,),
    )
    roles = [r[0] for r in rows]
    assert "user" not in roles, (
        f"routine fire persisted a user message: {roles}"
    )
    assert "assistant" in roles


def test_routine_fire_lets_user_clarifications_persist(sched, monkeypatch):
    """Real user clarifications typed into a routine thread must still
    persist normally. Only routine-injected user turns are skipped."""
    import agent
    import db

    class _FakeResult:
        def __init__(self, r): self.reply = r

    def _fake_run(user_input, thread_id=None, source="cli",
                   save_user_msg=True, **kw):
        # Mimic the same conditional save behaviour as the real run()
        if save_user_msg:
            db.save_message("user", user_input, thread_id=thread_id)
        db.save_message("assistant", "noted", thread_id=thread_id)
        return _FakeResult("noted")

    monkeypatch.setattr(agent, "run", _fake_run)

    r = sched.add("with-clarif", "do thing", "every 1h", skip_dry_run=True)
    tid = r["thread_id"]

    # 1. Routine fire — no user message
    sched._execute_routine("do thing", "with-clarif",
                            [t["id"] for t in sched.list_tasks() if t["name"] == "with-clarif"][0],
                            tid)
    # 2. User types a clarification (via the regular agent.run path,
    #    save_user_msg defaults to True)
    agent.run("actually skip weekends", thread_id=tid, source="web")

    rows = db.fetchall(
        "SELECT role, content FROM messages WHERE thread_id=? ORDER BY id",
        (tid,),
    )
    # Real user clarification persists; fire's task does not
    user_contents = [c for role, c in rows if role == "user"]
    assert "actually skip weekends" in user_contents
    assert "do thing" not in user_contents


def test_list_tasks_includes_recent_counts(sched, monkeypatch):
    import agent

    class _FakeResult:
        def __init__(self, r): self.reply = r
    monkeypatch.setattr(agent, "run",
                         lambda u, **kw: _FakeResult("done"))

    r = sched.add("counted", "ping", "every 1h", skip_dry_run=True)
    cron_id = [t["id"] for t in sched.list_tasks() if t["name"] == "counted"][0]
    sched._execute_routine("ping", "counted", cron_id, r["thread_id"])
    sched._execute_routine("ping", "counted", cron_id, r["thread_id"])

    entry = next(t for t in sched.list_tasks() if t["id"] == cron_id)
    assert "recent" in entry
    assert entry["recent"]["counts"]["ok"] == 2
    assert entry["recent"]["series"] == ["ok", "ok"]
