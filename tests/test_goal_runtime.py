"""Phase 1 of long-running agent runtime: goals queue + lease + checkpoints.

Tests at this layer use the real SQLite database (via qwe_temp_data_dir), no
mocked LLM — these are storage / concurrency contracts, not agent behaviour.
The agent.run integration is covered indirectly through tests/test_integration.py
and explicitly through tests/test_worker_lifecycle.py (added later when worker
subprocesses are exercised).
"""
from __future__ import annotations

import concurrent.futures
import time

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────────────────────────


def test_migration_011_creates_three_tables(qwe_temp_data_dir):
    """All three goal-runtime tables exist after the migration runs."""
    import db
    conn = db._get_conn()
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"goals", "goal_checkpoints", "goal_events"}.issubset(tables)


def test_goal_columns_match_design(qwe_temp_data_dir):
    """The columns used by db.create_goal / get_goal / claim_next_goal exist."""
    import db
    conn = db._get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()}
    expected = {
        "id", "thread_id", "source", "user_input", "status", "plan", "result",
        "error", "budget_usd", "budget_seconds", "cost_usd", "started_at",
        "finished_at", "created_at", "worker_id", "lease_expires_at", "meta",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
#  create_goal / get_goal / list_goals
# ─────────────────────────────────────────────────────────────────────────────


def test_create_goal_returns_id_and_sets_pending(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="scrape leads", source="web", thread_id="t1")
    assert gid.startswith("g_")
    g = db.get_goal(gid)
    assert g["status"] == "pending"
    assert g["source"] == "web"
    assert g["user_input"] == "scrape leads"
    assert g["thread_id"] == "t1"
    assert g["worker_id"] is None
    assert g["cost_usd"] == 0.0


def test_create_goal_logs_creation_event(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    events = db.get_goal_events(gid)
    types = [e["event_type"] for e in events]
    assert "goal_created" in types


def test_list_goals_filters_by_status(qwe_temp_data_dir):
    import db
    g1 = db.create_goal(user_input="a", source="cli")
    g2 = db.create_goal(user_input="b", source="cli")
    db.mark_goal_done(g1, result="done")
    pending = db.list_goals(status="pending")
    done = db.list_goals(status="done")
    assert [g["id"] for g in pending] == [g2]
    assert [g["id"] for g in done] == [g1]


def test_get_goal_returns_none_for_missing(qwe_temp_data_dir):
    import db
    assert db.get_goal("g_nope") is None


# ─────────────────────────────────────────────────────────────────────────────
#  claim_next_goal: atomicity, ordering, lease expiry
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_next_goal_picks_oldest_pending_first(qwe_temp_data_dir):
    import db
    older = db.create_goal(user_input="first", source="cli")
    # Force monotonically increasing created_at by sleeping; SQLite's REAL
    # timestamp gives microsecond precision but tests should be unambiguous.
    time.sleep(0.01)
    newer = db.create_goal(user_input="second", source="cli")

    claimed = db.claim_next_goal("worker_A", lease_sec=60)
    assert claimed == older

    claimed2 = db.claim_next_goal("worker_B", lease_sec=60)
    assert claimed2 == newer

    # No more pending goals
    assert db.claim_next_goal("worker_C", lease_sec=60) is None


def test_claim_next_goal_sets_lease_and_started_at(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    t0 = time.time()
    claimed = db.claim_next_goal("worker_X", lease_sec=60)
    assert claimed == gid
    g = db.get_goal(gid)
    assert g["status"] == "running"
    assert g["worker_id"] == "worker_X"
    assert g["lease_expires_at"] is not None
    assert g["lease_expires_at"] > t0
    assert g["started_at"] is not None


def test_claim_next_goal_concurrent_workers_dont_double_claim(qwe_temp_data_dir):
    """Two threads racing to claim the same goal — exactly ONE wins."""
    import db
    gid = db.create_goal(user_input="race", source="cli")

    results: list = []
    barrier = __import__("threading").Barrier(8)

    def _claim(name: str):
        # Each thread needs its own DB connection — db._get_conn is thread-local
        barrier.wait()  # release all threads at the same instant
        return db.claim_next_goal(name, lease_sec=60)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_claim, f"worker_{i}") for i in range(8)]
        results = [f.result() for f in futures]

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}: {results}"
    assert winners[0] == gid


def test_claim_next_goal_takes_over_expired_lease(qwe_temp_data_dir):
    """If a worker dies, lease expires, another worker takes over."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    # Worker A claims with very short lease, then "dies" (does nothing else).
    assert db.claim_next_goal("worker_A", lease_sec=1) == gid
    # Wait past the lease.
    time.sleep(1.2)
    # Worker B should now be able to claim the same goal.
    assert db.claim_next_goal("worker_B", lease_sec=60) == gid

    g = db.get_goal(gid)
    assert g["worker_id"] == "worker_B"
    # The takeover should be visible in the event log.
    events = db.get_goal_events(gid)
    types = [e["event_type"] for e in events]
    assert "worker_lost" in types
    assert "resumed" in types


def test_claim_next_goal_does_not_take_done_goals(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.mark_goal_done(gid, result="finished")
    assert db.claim_next_goal("worker_X", lease_sec=60) is None


# ─────────────────────────────────────────────────────────────────────────────
#  Heartbeat
# ─────────────────────────────────────────────────────────────────────────────


def test_heartbeat_refreshes_lease(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.claim_next_goal("worker_A", lease_sec=10)
    g1 = db.get_goal(gid)
    time.sleep(0.05)
    assert db.heartbeat_goal(gid, "worker_A", lease_sec=60) is True
    g2 = db.get_goal(gid)
    assert g2["lease_expires_at"] > g1["lease_expires_at"]


def test_heartbeat_fails_after_takeover(qwe_temp_data_dir):
    """Worker A heartbeats — if worker B has taken over, A gets False."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.claim_next_goal("worker_A", lease_sec=1)
    time.sleep(1.2)
    db.claim_next_goal("worker_B", lease_sec=60)
    # Now worker A tries to heartbeat — should fail (different worker_id owns it).
    assert db.heartbeat_goal(gid, "worker_A", lease_sec=60) is False


def test_release_worker_leases_on_restart(qwe_temp_data_dir):
    """Worker startup releases any leases it held in a previous life."""
    import db
    g1 = db.create_goal(user_input="a", source="cli")
    g2 = db.create_goal(user_input="b", source="cli")
    db.claim_next_goal("worker_X", lease_sec=60)
    db.claim_next_goal("worker_X", lease_sec=60)
    # Worker X reboots → release its leases
    n = db.release_worker_leases("worker_X")
    assert n == 2
    # Both goals now paused, claimable again.
    assert db.get_goal(g1)["status"] == "paused"
    assert db.get_goal(g2)["status"] == "paused"
    # A fresh worker can claim them.
    assert db.claim_next_goal("worker_Y", lease_sec=60) in (g1, g2)


# ─────────────────────────────────────────────────────────────────────────────
#  Status transitions
# ─────────────────────────────────────────────────────────────────────────────


def test_mark_goal_done_clears_lease(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.claim_next_goal("worker_A", lease_sec=60)
    db.mark_goal_done(gid, result="finished")
    g = db.get_goal(gid)
    assert g["status"] == "done"
    assert g["result"] == "finished"
    assert g["worker_id"] is None
    assert g["lease_expires_at"] is None
    assert g["finished_at"] is not None


def test_mark_goal_failed_records_error(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.claim_next_goal("worker_A", lease_sec=60)
    db.mark_goal_failed(gid, error="boom")
    g = db.get_goal(gid)
    assert g["status"] == "failed"
    assert g["error"] == "boom"
    assert g["worker_id"] is None


def test_mark_goal_paused_keeps_resumable(qwe_temp_data_dir):
    """Paused goals can be claimed again by claim_next_goal."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.claim_next_goal("worker_A", lease_sec=60)
    db.mark_goal_paused(gid, reason="test")
    g = db.get_goal(gid)
    assert g["status"] == "paused"
    # Re-claimable.
    assert db.claim_next_goal("worker_B", lease_sec=60) == gid


def test_mark_goal_aborted_is_terminal(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.mark_goal_aborted(gid, reason="user")
    assert db.get_goal(gid)["status"] == "aborted"
    # Aborted goals are NOT re-claimable.
    assert db.claim_next_goal("worker_X", lease_sec=60) is None


# ─────────────────────────────────────────────────────────────────────────────
#  Checkpoints
# ─────────────────────────────────────────────────────────────────────────────


def test_save_and_load_checkpoint_round_trip(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    messages = [
        {"role": "system", "content": "you are X"},
        {"role": "user", "content": "do thing"},
        {"role": "assistant", "content": "doing"},
    ]
    plan = {"subtasks": [{"id": "st_1", "status": "pending"}]}
    facts = {"login_url": "https://example.com/login"}

    db.save_checkpoint(gid, round_num=3, messages=messages, plan=plan, facts=facts)

    cp = db.load_latest_checkpoint(gid)
    assert cp is not None
    assert cp["round_num"] == 3
    assert cp["messages"] == messages
    assert cp["plan"] == plan
    assert cp["facts"] == facts


def test_load_latest_checkpoint_returns_newest(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    for r in (3, 6, 9, 12, 15):
        db.save_checkpoint(gid, round_num=r, messages=[{"role": "user", "content": str(r)}])

    cp = db.load_latest_checkpoint(gid)
    assert cp["round_num"] == 15
    assert cp["messages"][0]["content"] == "15"


def test_checkpoint_pruning_keeps_latest_n(qwe_temp_data_dir):
    """After many writes, only the latest CHECKPOINT_RETENTION rows remain."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    for r in range(1, 21):  # 20 checkpoints
        db.save_checkpoint(gid, round_num=r, messages=[{"role": "user", "content": str(r)}])

    conn = db._get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM goal_checkpoints WHERE goal_id=?", (gid,)
    ).fetchone()[0]
    assert count == db.CHECKPOINT_RETENTION

    # The 5 latest round numbers should be 16..20.
    rounds = [r[0] for r in conn.execute(
        "SELECT round_num FROM goal_checkpoints WHERE goal_id=? ORDER BY round_num",
        (gid,),
    ).fetchall()]
    assert rounds == [16, 17, 18, 19, 20]


def test_load_checkpoint_returns_none_for_missing(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    assert db.load_latest_checkpoint(gid) is None


def test_checkpoint_truncates_oversized_blob(qwe_temp_data_dir, monkeypatch):
    """A message list that would blow past the cap is truncated, not crashed.

    Lowers the cap to 4 KB via monkeypatch and uses random (incompressible)
    content so gzip can't shrink the test data under the cap.
    """
    import db
    import os as _os
    monkeypatch.setattr(db, "MAX_CHECKPOINT_BLOB_BYTES", 4 * 1024)  # 4 KB
    gid = db.create_goal(user_input="x", source="cli")
    big_msgs = [
        {"role": "system", "content": "system prompt"},
    ] + [
        # Random base64 — incompressible, ~1 KB each × 50 = 50 KB raw.
        {"role": "user", "content": _os.urandom(800).hex()}
        for _ in range(50)
    ]
    db.save_checkpoint(gid, round_num=1, messages=big_msgs)
    cp = db.load_latest_checkpoint(gid)
    # System prompt MUST be preserved.
    assert cp["messages"][0] == {"role": "system", "content": "system prompt"}
    # Total fewer messages than we sent in.
    assert len(cp["messages"]) < len(big_msgs)


# ─────────────────────────────────────────────────────────────────────────────
#  Event log
# ─────────────────────────────────────────────────────────────────────────────


def test_log_goal_event_appends(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.log_goal_event(gid, "subagent_dispatched", {"type": "browser"})
    events = db.get_goal_events(gid)
    types = [e["event_type"] for e in events]
    assert "subagent_dispatched" in types
    sa = [e for e in events if e["event_type"] == "subagent_dispatched"][0]
    assert sa["payload"]["type"] == "browser"


def test_get_goal_events_oldest_first(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.log_goal_event(gid, "a", {})
    db.log_goal_event(gid, "b", {})
    db.log_goal_event(gid, "c", {})
    events = db.get_goal_events(gid)
    # First event is goal_created (from create_goal), then a/b/c
    types = [e["event_type"] for e in events]
    # Filter the bootstrap event
    user_types = [t for t in types if t != "goal_created"]
    assert user_types == ["a", "b", "c"]


def test_log_goal_event_never_raises_on_bad_payload(qwe_temp_data_dir):
    """A payload that can't be JSON-serialised must not break the caller."""
    import db
    gid = db.create_goal(user_input="x", source="cli")

    class Unserialisable:
        pass

    # Should not raise; we don't care if the event lands or not.
    db.log_goal_event(gid, "weird", {"obj": Unserialisable()})


# ─────────────────────────────────────────────────────────────────────────────
#  goal_runner integration (light)
# ─────────────────────────────────────────────────────────────────────────────


def test_checkpoint_callback_respects_interval(qwe_temp_data_dir, monkeypatch):
    """The callback only writes a checkpoint when round % interval == 0."""
    import goal_runner
    import db
    gid = db.create_goal(user_input="x", source="cli")
    monkeypatch.setattr(goal_runner, "_checkpoint_interval", lambda: 3)
    cb = goal_runner._make_checkpoint_callback(gid, start_round=0)

    # round 1 → no checkpoint
    cb(1, [{"role": "user", "content": "r1"}])
    assert db.load_latest_checkpoint(gid) is None

    # round 2 → no
    cb(2, [{"role": "user", "content": "r2"}])
    assert db.load_latest_checkpoint(gid) is None

    # round 3 → yes
    cb(3, [{"role": "user", "content": "r3"}])
    cp = db.load_latest_checkpoint(gid)
    assert cp is not None
    assert cp["round_num"] == 3
    assert cp["messages"][0]["content"] == "r3"


def test_checkpoint_callback_respects_start_round_offset(qwe_temp_data_dir, monkeypatch):
    """When resuming from round 7, the next checkpoint should be at global round 9."""
    import goal_runner
    import db
    gid = db.create_goal(user_input="x", source="cli")
    monkeypatch.setattr(goal_runner, "_checkpoint_interval", lambda: 3)
    cb = goal_runner._make_checkpoint_callback(gid, start_round=7)

    # New round 1 → global round 8 → not a multiple of 3
    cb(1, [{"role": "user", "content": "r1"}])
    assert db.load_latest_checkpoint(gid) is None

    # New round 2 → global round 9 → checkpoint!
    cb(2, [{"role": "user", "content": "r2"}])
    cp = db.load_latest_checkpoint(gid)
    assert cp is not None
    assert cp["round_num"] == 9


# ─────────────────────────────────────────────────────────────────────────────
#  TurnContext extensions
# ─────────────────────────────────────────────────────────────────────────────


def test_turn_context_has_goal_id_and_callback():
    """TurnContext exposes the new Phase 1 fields with sane defaults."""
    from turn_context import TurnContext
    ctx = TurnContext()
    assert ctx.goal_id is None
    assert ctx.on_round_complete is None


def test_turn_context_emit_round_complete_calls_callback():
    from turn_context import TurnContext
    calls = []
    ctx = TurnContext(on_round_complete=lambda n, m: calls.append((n, len(m))))
    ctx.emit_round_complete(5, [{"role": "user"}, {"role": "assistant"}])
    assert calls == [(5, 2)]


def test_turn_context_emit_round_complete_swallows_callback_errors():
    """Callback that raises must not propagate — checkpoint is best-effort."""
    from turn_context import TurnContext

    def _boom(n, m):
        raise RuntimeError("checkpoint disk full")

    ctx = TurnContext(on_round_complete=_boom)
    # Must not raise.
    ctx.emit_round_complete(1, [])


# ─────────────────────────────────────────────────────────────────────────────
#  Bridge: no leaked tasks when goal completes normally
# ─────────────────────────────────────────────────────────────────────────────


def test_bridge_shutdown_returns_task_handle(qwe_temp_data_dir):
    """_bridge_shutdown_to_threading must return BOTH the threading.Event
    AND the watcher task so callers can cancel it in finally. Without the
    task handle every completed goal leaked one pending Task per run,
    which asyncio surfaced as: 'Task pending ... wait_for=<Future pending>'.
    """
    import asyncio as _aio
    import goal_runner

    async def _check():
        shutdown = _aio.Event()
        evt, task = goal_runner._bridge_shutdown_to_threading(shutdown)
        # Contract: returns a 2-tuple (threading.Event, asyncio.Task).
        import threading as _t
        assert isinstance(evt, _t.Event)
        assert isinstance(task, _aio.Task)
        # Cleanup so this test itself doesn't leak.
        task.cancel()
        try:
            await task
        except _aio.CancelledError:
            pass

    _aio.run(_check())


def test_bridge_watcher_cancellable_without_leak(qwe_temp_data_dir):
    """When the goal completes WITHOUT setting shutdown_event, the watcher
    task must be cancellable. After cancel + await, the task is fully
    done and no asyncio warning about pending tasks remains.
    """
    import asyncio as _aio
    import goal_runner

    async def _check():
        shutdown = _aio.Event()
        evt, task = goal_runner._bridge_shutdown_to_threading(shutdown)
        # Simulate "goal completed normally" — shutdown event was never set.
        # Caller is responsible for tearing the watcher down.
        assert not task.done()
        task.cancel()
        try:
            await task
        except _aio.CancelledError:
            pass
        assert task.done()
        # threading.Event was NOT set because shutdown didn't fire — abort
        # signal stays clean for the next goal.
        assert not evt.is_set()

    _aio.run(_check())


def test_bridge_watcher_fires_threading_event(qwe_temp_data_dir):
    """Happy path: when shutdown_event is set, watcher mirrors it to the
    threading.Event so the sync agent loop sees the abort signal."""
    import asyncio as _aio
    import goal_runner

    async def _check():
        shutdown = _aio.Event()
        evt, task = goal_runner._bridge_shutdown_to_threading(shutdown)
        # Trigger the shutdown
        shutdown.set()
        # Wait for the watcher to run + mirror
        await task
        assert evt.is_set()
        assert task.done()

    _aio.run(_check())
