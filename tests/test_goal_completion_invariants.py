"""Plan/goal completion invariants — fixes from live LinkedIn lead-gen run.

Live run exposed two bugs:

1. Orchestrator dispatched ``dispatch_subagent(subtask_id="st_2b")`` —
   `st_2b` doesn't exist in the plan. `update_subtask` silently returned
   None, the dispatch still ran, and the plan's `st_2.attempts` froze at
   the wrong value forever. UI looked stuck while the agent was actually
   busy under a fabricated ID.

2. Orchestrator wrote a perfectly good final summary (with 20 real lead
   profiles) but didn't call `subtask_update` to close `st_2`/`st_3`/
   `st_4` first. `goal_runner.run()` then called `mark_goal_done` →
   goal status flipped to DONE while plan still showed 3 subtasks
   pending. Inconsistent state visible in the UI.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
#  dispatch_subagent rejects fabricated subtask IDs
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_subagent_rejects_unknown_subtask_id(qwe_temp_data_dir):
    """A dispatch with subtask_id NOT in the plan returns a clear error
    string listing the valid IDs — instead of silently running and leaving
    the plan inconsistent."""
    import db
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    result = tools.execute("dispatch_subagent", {
        "type": "browser",
        "prompt": "go",
        "subtask_id": "st_2b",  # hallucinated — only st_1 + st_2 exist
    })
    assert "Error" in result
    assert "st_2b" in result
    # Tells the orchestrator what IDs are actually valid
    assert "st_1" in result and "st_2" in result
    # And how to fix it
    assert "goal_plan_set" in result


def test_dispatch_subagent_accepts_valid_subtask_id(qwe_temp_data_dir, monkeypatch):
    """Sanity: a valid subtask_id flows through normally."""
    import db
    import subagent
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    captured: dict = {}

    def _fake_run(**kw):
        captured.update(kw)
        return "subagent done"

    monkeypatch.setattr(subagent, "run_subagent", _fake_run)

    result = tools.execute("dispatch_subagent", {
        "type": "browser",
        "prompt": "go",
        "subtask_id": "st_1",
    })
    assert result == "subagent done"
    assert captured["subtask_id"] == "st_1"


def test_dispatch_subagent_without_plan_still_works(qwe_temp_data_dir, monkeypatch):
    """A goal with no plan yet (orchestrator dispatching before goal_plan_set?)
    must not crash on the validation lookup."""
    import db
    import subagent
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    # No db.set_goal_plan() — plan is None
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    monkeypatch.setattr(subagent, "run_subagent",
                        lambda **kw: "ok")

    result = tools.execute("dispatch_subagent", {
        "type": "research",
        "prompt": "x",
        "subtask_id": "anything",
    })
    # No validation when there's no plan → dispatch succeeds
    assert result == "ok"


# ─────────────────────────────────────────────────────────────────────────────
#  goal_runner backstop: auto-skip pending subtasks before marking done
# ─────────────────────────────────────────────────────────────────────────────


def test_goal_runner_skips_pending_subtasks_before_done(qwe_temp_data_dir):
    """If the orchestrator returns a final reply but the plan still has
    pending/in_progress subtasks, goal_runner auto-marks them as skipped
    (with a clear reason) before flipping the goal to done. End state:
    no inconsistent (done goal, pending subtask) combinations."""
    import asyncio
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
        {"title": "C", "description": ""},
    ])
    # Mark st_1 done, leave st_2 in_progress, st_3 pending — exactly what
    # the LinkedIn run looked like at completion.
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="A done")
    db.update_subtask(goal_id, "st_2", status="in_progress",
                      result_summary="working on B")
    # Now stub run_orchestrator so goal_runner.run sees a "successful" finish
    # without us launching a real LLM.

    def _fake_orch(**kw):
        return {
            "reply": "Final summary: did A, partial B, C not attempted.",
            "rounds": 5, "tools_used": [], "cost_usd": 0.0,
            "prompt_tokens": 0, "completion_tokens": 0,
        }
    import unittest.mock
    with unittest.mock.patch.object(orchestrator, "run_orchestrator",
                                     side_effect=_fake_orch):
        async def _go():
            shutdown = asyncio.Event()
            await goal_runner.run(goal_id, shutdown)
        asyncio.run(_go())

    # Goal is done with the reply
    g = db.get_goal(goal_id)
    assert g["status"] == "done"
    assert "Final summary" in g["result"]

    # Plan: st_1 stays completed, st_2 + st_3 auto-skipped with clear reason
    plan = db.get_goal_plan(goal_id)
    statuses = {st["id"]: st["status"] for st in plan["subtasks"]}
    assert statuses == {
        "st_1": "completed",
        "st_2": "skipped",
        "st_3": "skipped",
    }
    # Auto-skip reason is recognisable so users can grep for it later
    for st in plan["subtasks"]:
        if st["status"] == "skipped":
            assert "orchestrator wrote a final summary" in st["result_summary"]


def test_goal_runner_preserves_already_terminal_subtasks(qwe_temp_data_dir):
    """The backstop must NOT touch subtasks that already reached a terminal
    status (completed/failed/skipped) — only the pending/in_progress ones."""
    import asyncio
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
        {"title": "C", "description": ""},
    ])
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="A explicitly done")
    db.update_subtask(goal_id, "st_2", status="failed",
                      result_summary="B blocked by captcha")
    # st_3 stays pending

    def _fake_orch(**kw):
        return {"reply": "done", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    import unittest.mock
    with unittest.mock.patch.object(orchestrator, "run_orchestrator",
                                     side_effect=_fake_orch):
        async def _go():
            shutdown = asyncio.Event()
            await goal_runner.run(goal_id, shutdown)
        asyncio.run(_go())

    plan = db.get_goal_plan(goal_id)
    by_id = {st["id"]: st for st in plan["subtasks"]}
    # st_1 stays completed with its original summary
    assert by_id["st_1"]["status"] == "completed"
    assert by_id["st_1"]["result_summary"] == "A explicitly done"
    # st_2 stays failed with its original summary
    assert by_id["st_2"]["status"] == "failed"
    assert by_id["st_2"]["result_summary"] == "B blocked by captcha"
    # Only st_3 (was pending) got auto-skipped
    assert by_id["st_3"]["status"] == "skipped"


def test_goal_runner_complete_plan_no_skip_messages(qwe_temp_data_dir):
    """When the plan is already complete (orchestrator did mark every subtask),
    the backstop is a no-op — no spurious skipped entries appear."""
    import asyncio
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [{"title": "A", "description": ""}])
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="A done")

    def _fake_orch(**kw):
        return {"reply": "all done", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    import unittest.mock
    with unittest.mock.patch.object(orchestrator, "run_orchestrator",
                                     side_effect=_fake_orch):
        async def _go():
            shutdown = asyncio.Event()
            await goal_runner.run(goal_id, shutdown)
        asyncio.run(_go())

    plan = db.get_goal_plan(goal_id)
    # No mutations — original result_summary intact
    assert plan["subtasks"][0]["result_summary"] == "A done"
