"""Goal runner — execute one durable goal to completion (or pause/fail).

Bridges the asyncio worker loop to the synchronous orchestrator. Each call
to :func:`run` corresponds to one ``goal`` claimed off the queue: load
the latest checkpoint, invoke the orchestrator inside the default thread
pool, then mark the goal done/paused/failed based on outcome.

Phase 1 of this module used a thin wrapper around the chat-style
``agent.run()``. Phase 2 (this version) delegates to :mod:`orchestrator`
which:

    - uses ``prompts/orchestrator.md`` instead of ``soul.py``
    - has access only to plan-management + lightweight tools
    - dispatches heavy work to subagents via ``dispatch_subagent``
      (Phase 2c)

The goal_runner itself doesn't know or care which orchestrator strategy
is in use — that's encapsulated in :func:`orchestrator.run_orchestrator`.
"""
from __future__ import annotations

import asyncio
import threading

import config
import db
import logger
import orchestrator
from turn_context import TurnContext

_log = logger.get("goal_runner")


def _checkpoint_interval() -> int:
    """Rounds between checkpoints. Configurable via EDITABLE_SETTINGS."""
    try:
        v = config.get("checkpoint_round_interval")
        return max(1, int(v)) if v else 3
    except (TypeError, ValueError):
        return 3


async def run(goal_id: str, shutdown_event: asyncio.Event) -> None:
    """Run one goal until terminal status.

    Never raises — all errors are caught and recorded on the goal row so the
    worker poll loop can keep going.
    """
    goal = db.get_goal(goal_id)
    if not goal:
        _log.warning(f"goal {goal_id} not found, skipping")
        return

    if goal["status"] in db.GOAL_TERMINAL_STATUSES:
        _log.info(f"goal {goal_id} already in terminal status {goal['status']}, skipping")
        return

    checkpoint = db.load_latest_checkpoint(goal_id)
    start_round = (checkpoint["round_num"] + 1) if checkpoint else 0
    if checkpoint:
        _log.info(f"resuming {goal_id} from round {checkpoint['round_num']}")
    else:
        _log.info(f"starting {goal_id} fresh")
        db.log_goal_event(goal_id, "goal_started",
                          {"input_preview": goal["user_input"][:200]})

    # Bridge asyncio shutdown_event → threading.Event so the sync agent loop
    # (which runs in an executor) can poll it and exit cleanly. The watcher
    # task is returned so we can cancel it in finally — without that, every
    # goal that completes normally leaks a pending watcher (asyncio warns:
    # "Task pending ... wait_for=<Future pending>" until interpreter exit).
    abort_event, _watcher_task = _bridge_shutdown_to_threading(shutdown_event)

    ctx = TurnContext(
        source=goal["source"],
        abort_event=abort_event,
        goal_id=goal_id,
        on_round_complete=_make_checkpoint_callback(goal_id, start_round),
    )

    try:
        # orchestrator.run_orchestrator is synchronous — run it in the default
        # executor so this coroutine doesn't block the worker's poll loop /
        # heartbeat task.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: orchestrator.run_orchestrator(goal_id=goal_id, ctx=ctx),
        )
    except asyncio.CancelledError:
        # Cooperative cancellation — checkpoint preserved by on_round_complete.
        _log.info(f"goal {goal_id} cancelled during run; marking paused")
        db.mark_goal_paused(goal_id, reason="worker_cancelled")
        raise
    except Exception as e:
        _log.exception(f"goal {goal_id} crashed: {e}")
        db.mark_goal_failed(goal_id, error=f"{type(e).__name__}: {e}")
        return
    finally:
        # Always tear down the shutdown-event watcher so it doesn't outlive
        # the goal. Cancel + suppress CancelledError so cleanup never raises.
        if not _watcher_task.done():
            _watcher_task.cancel()
            try:
                await _watcher_task
            except (asyncio.CancelledError, Exception):
                pass

    # Close the per-goal browser session (frees the Chrome process). The
    # user_data_dir on disk stays — if the user re-creates the goal or it
    # resumes, login cookies / localStorage are preserved.
    try:
        import skills.browser as _bs
        _bs._close_session(goal_id)
    except Exception:
        # Best-effort — never block goal completion on browser cleanup.
        _log.exception(f"failed to close browser session for {goal_id}")

    # Did the shutdown_event fire while the orchestrator was running? If yes
    # the loop may have returned early after abort — treat as paused.
    if shutdown_event.is_set():
        db.mark_goal_paused(goal_id, reason="worker_shutdown")
        return

    reply = (result.get("reply") if isinstance(result, dict) else "") or ""

    # ── Plan-completion backstop ──
    # The orchestrator wrote a final reply, which usually means it's done.
    # But sometimes it stops without explicitly marking every subtask
    # (we've seen it leave st_2 as `pending` with attempts=7 while writing
    # a perfectly good summary). Auto-skip any still-pending or in_progress
    # subtasks with a clear reason so the plan reflects reality.
    plan = db.get_goal_plan(goal_id)
    if plan and plan.get("subtasks"):
        unfinished = [
            st for st in plan["subtasks"]
            if st.get("status") in ("pending", "in_progress")
        ]
        if unfinished:
            _log.warning(
                f"goal {goal_id}: orchestrator finished but {len(unfinished)} "
                f"subtask(s) still pending/in_progress; auto-skipping"
            )
            for st in unfinished:
                # in_progress → orchestrator was working on it but stopped;
                # call it "skipped" not "failed" since we don't have a hard
                # error to report, just incomplete work.
                try:
                    db.update_subtask(
                        goal_id, st["id"],
                        status="skipped",
                        result_summary=(
                            "Auto-skipped: orchestrator wrote a final summary "
                            "without explicitly closing this subtask. See "
                            "goal.result for what was accomplished."
                        ),
                    )
                except Exception:
                    _log.exception(
                        f"failed to auto-skip {st['id']} on {goal_id}"
                    )

    db.mark_goal_done(goal_id, result=reply)


def _make_checkpoint_callback(goal_id: str, start_round: int):
    """Build the on_round_complete callback that persists every N rounds.

    The callback runs inside the agent_loop thread (the executor where
    agent.run is running). SQLite is thread-safe with per-thread connections
    (db._local.conn), so the write happens on its own connection.
    """
    interval = _checkpoint_interval()

    def _cb(round_num: int, messages: list[dict]) -> None:
        global_round = start_round + round_num
        if global_round <= 0 or (global_round % interval) != 0:
            return
        try:
            db.save_checkpoint(
                goal_id,
                global_round,
                subtask_index=-1,  # no plan yet in Phase 1
                messages=messages,
                plan={},
                facts={},
            )
            db.log_goal_event(goal_id, "checkpoint_saved",
                              {"round": global_round, "messages": len(messages)})
        except Exception:
            _log.exception(f"checkpoint failed for {goal_id} round {global_round}")

    return _cb


def _bridge_shutdown_to_threading(
    shutdown_event: asyncio.Event,
) -> "tuple[threading.Event, asyncio.Task]":
    """Return ``(threading.Event, watcher_task)``.

    The threading.Event fires whenever ``shutdown_event`` is set, so the
    sync agent loop (which lives in a thread executor and only knows
    about ``threading.Event`` for aborts via ``shell`` / ``http_request``)
    can poll it. The watcher_task is returned so the caller can cancel
    it in a ``finally`` — without that, every goal that completes WITHOUT
    setting shutdown_event leaks a pending task. Asyncio surfaces those
    as warnings: "Task pending ... wait_for=<Future pending>".
    """
    evt = threading.Event()

    async def _watcher() -> None:
        try:
            await shutdown_event.wait()
        except asyncio.CancelledError:
            return
        evt.set()

    task = asyncio.create_task(_watcher())
    return evt, task
