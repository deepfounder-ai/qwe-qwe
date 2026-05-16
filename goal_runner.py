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

**Acceptance gate (added 2026-05-16).** After every orchestrator return,
we run :func:`goal_validators.run_validator` over every subtask's
``done_condition``. Failures inject a remediation block as a
``system_note`` and re-enter the orchestrator (up to
``MAX_GATE_ATTEMPTS`` rounds). Exhaustion → ``mark_goal_failed`` with
``error="acceptance_gate_exhausted: ..."``. This replaces the old
auto-skip backstop, which silently masked failed work.
"""
from __future__ import annotations

import asyncio
import threading

import config
import db
import goal_validators
import logger
import orchestrator
from turn_context import TurnContext

_log = logger.get("goal_runner")


# Default cap on acceptance-gate re-entries. Overridable via
# config.get("acceptance_gate_max_attempts"). Three attempts is enough
# for the model to fix routine issues without burning unbounded budget;
# the goal is marked ``failed`` after that so a human can intervene.
MAX_GATE_ATTEMPTS = 3


def _gate_max_attempts() -> int:
    """Resolve the gate cap from config, fall back to module default.

    ``config.get`` raises ``KeyError`` for unregistered settings. The
    callable is wrapped so a missing/typo'd key never breaks the
    runner — we just use the module default.
    """
    try:
        v = config.get("acceptance_gate_max_attempts")
        return max(1, int(v)) if v else MAX_GATE_ATTEMPTS
    except (KeyError, TypeError, ValueError):
        return MAX_GATE_ATTEMPTS


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

    # ── Acceptance-gate loop ──
    # Each iteration: invoke the orchestrator, then validate every
    # subtask's done_condition. Failures build a remediation note and
    # re-enter; once every condition passes, exit the loop and proceed
    # to mark_goal_done. Exhaustion → mark_goal_failed.
    max_attempts = _gate_max_attempts()
    gate_attempt = 0
    system_notes: list[str] = []
    final_result: dict | None = None

    try:
        loop = asyncio.get_running_loop()
        while True:
            # Shutdown check on EVERY loop iteration — if it fires mid-gate
            # retry the goal goes back into the queue as paused, no run.
            if shutdown_event.is_set():
                db.mark_goal_paused(goal_id, reason="worker_shutdown")
                return

            gate_attempt += 1
            try:
                final_result = await loop.run_in_executor(
                    None,
                    lambda notes=tuple(system_notes): orchestrator.run_orchestrator(
                        goal_id=goal_id, ctx=ctx, system_notes=list(notes),
                    ),
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

            # The orchestrator may have returned because the shutdown fired
            # mid-round (abort_event propagated). Treat as paused — no point
            # running the gate when work was interrupted.
            if shutdown_event.is_set():
                db.mark_goal_paused(goal_id, reason="worker_shutdown")
                return

            # Run validators across the current plan.
            plan = db.get_goal_plan(goal_id) or {}
            failures: list[tuple[str, str]] = []
            for st in plan.get("subtasks", []):
                cond = st.get("done_condition")
                if not cond:
                    # Be defensive — older plans / agent-created plans may not
                    # have a done_condition. The gate skips them; the goal
                    # can still complete on the orchestrator's say-so.
                    continue
                try:
                    passed, remediation = goal_validators.run_validator(cond)
                except Exception as e:  # noqa: BLE001 — last-resort guard
                    # run_validator is contracted not to raise; if it does
                    # (e.g. stub mid-merge), treat as failure with a
                    # diagnostic remediation so the orchestrator sees it.
                    passed = False
                    remediation = f"validator crashed: {type(e).__name__}: {e}"
                if not passed:
                    failures.append((st["id"], remediation))
                    try:
                        db.update_subtask(
                            goal_id, st["id"],
                            validation_passed=False,
                            last_validation_failure=remediation,
                        )
                    except Exception:
                        _log.exception(
                            f"failed to record validation failure on {st['id']}"
                        )
                else:
                    # Mark passed only if it wasn't already (cheap UI flip).
                    if not st.get("validation_passed"):
                        try:
                            db.update_subtask(
                                goal_id, st["id"],
                                validation_passed=True,
                            )
                        except Exception:
                            _log.exception(
                                f"failed to record validation pass on {st['id']}"
                            )

            if not failures:
                # All gates passed — exit loop and proceed to mark_done below.
                break

            # Failures — log + decide whether to retry or give up.
            try:
                db.log_goal_event(goal_id, "acceptance_gate_blocked", {
                    "attempt": gate_attempt,
                    "failure_count": len(failures),
                    "failures": [
                        {"subtask_id": sid, "remediation": rem[:300]}
                        for sid, rem in failures
                    ],
                })
            except Exception:
                _log.exception(
                    f"failed to log acceptance_gate_blocked for {goal_id}"
                )

            if gate_attempt >= max_attempts:
                db.mark_goal_failed(
                    goal_id,
                    error=(
                        f"acceptance_gate_exhausted: {len(failures)} "
                        f"subtask(s) still failing after {max_attempts} attempts"
                    ),
                )
                return

            # Build the remediation note for the next orchestrator round.
            note_lines = [
                "ACCEPTANCE GATE: The following subtasks have NOT met their done_condition.",
                (
                    "You CANNOT finish the goal until every condition passes. "
                    "Address each one and re-run subtask_update with "
                    "status=completed once your work makes the validator pass."
                ),
                "",
            ]
            for sid, rem in failures:
                note_lines.append(f"- {sid}: {rem}")
            system_notes = ["\n".join(note_lines)]
            # Loop: re-enter the orchestrator with the new notes.
    finally:
        # Always tear down the shutdown-event watcher so it doesn't outlive
        # the goal. Cancel + suppress CancelledError so cleanup never raises.
        if not _watcher_task.done():
            _watcher_task.cancel()
            try:
                await _watcher_task
            except (asyncio.CancelledError, Exception):
                pass

        # Close the per-goal browser session (frees Chrome + releases the
        # SingletonLock on the profile dir). MUST be in finally so it runs
        # on CancelledError, crashes, mark_goal_failed paths too —
        # otherwise transient goal failures accumulate zombie Chromes.
        # The user_data_dir on disk stays — if the goal is recreated or
        # resumed, login cookies / localStorage survive.
        try:
            import skills.browser as _bs
        except ImportError:
            # Playwright not installed → no browser session ever existed →
            # nothing to clean up. Not an error.
            pass
        else:
            try:
                _bs._close_session(goal_id)
            except Exception:
                _log.exception(f"failed to close browser session for {goal_id}")

        # Auto-attach workspace files written during this goal as outputs.
        # Orchestrators don't always remember to call goal_attach_output for
        # every file they wrote — this is the runtime safety net. Runs on
        # EVERY terminal path (done / failed / paused / cancelled) so
        # partial progress is visible in the UI even when the orchestrator
        # capitulated mid-goal. Dedups vs already-attached outputs.
        try:
            new_ids = db.auto_attach_workspace_outputs(goal_id)
            if new_ids:
                _log.info(
                    f"auto-attached {len(new_ids)} workspace file(s) "
                    f"as outputs for {goal_id}"
                )
        except Exception:
            _log.exception(f"workspace auto-attach failed for {goal_id}")

    # Gate passed — proceed to mark the goal done.
    reply = (
        final_result.get("reply") if isinstance(final_result, dict) else ""
    ) or ""
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
