# Auto-loaded by Python via sitecustomize when this file's parent dir is on
# PYTHONPATH. Replaces orchestrator.run_orchestrator with a deterministic
# fake so worker-lifecycle tests don't need a live LLM provider.
#
# The scripted reply is read from CASTOR_TEST_FAKE_REPLY env var (default "done").

import os
import sys
import traceback


def _install_shim():
    try:
        import orchestrator

        scripted_reply = os.environ.get("CASTOR_TEST_FAKE_REPLY", "done")

        def _fake_run_orchestrator(goal_id, ctx, system_notes=None):
            sys.stderr.write("[shim] fake_run_orchestrator fired for " + str(goal_id) + "\n")

            # Create a minimal plan so the empty-plan guard doesn't reject
            # the goal. Real orchestrators always call goal_plan_set before
            # returning — the shim must do the same.
            import db as _db
            try:
                if not (_db.get_goal_plan(goal_id) or {}).get("subtasks"):
                    _db.set_goal_plan(goal_id, [{
                        "title": "Execute task",
                        "description": "Shim-generated subtask",
                        "done_condition": {
                            "kind": "shell_returns_zero",
                            "spec": {"cmd": "true"},
                        },
                    }])
                    _db.update_subtask(goal_id, "st_1", status="completed",
                                       result_summary="shim done")
            except Exception as plan_err:
                sys.stderr.write("[shim] plan setup: " + repr(plan_err) + "\n")

            # Fire the checkpoint callback over a few rounds so checkpoints land.
            if ctx is not None and ctx.on_round_complete is not None:
                for r in range(1, 7):
                    ctx.on_round_complete(r, [{"role": "user", "content": str(r)}])
            return {
                "reply": scripted_reply,
                "rounds": 6,
                "tools_used": [],
                "cost_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }

        orchestrator.run_orchestrator = _fake_run_orchestrator
        sys.stderr.write("[shim] orchestrator.run_orchestrator replaced\n")
    except Exception as e:
        sys.stderr.write("[shim] install failed: " + repr(e) + "\n")
        traceback.print_exc()


_install_shim()
