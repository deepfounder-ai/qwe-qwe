"""End-to-end test for the acceptance gate.

Stitches together the three workstreams the gate is built from:

  A — goal_validators.run_validator runs against the real filesystem
  B — db.set_goal_plan stores done_condition, db.update_subtask honors it
  C — goal_runner re-enters the orchestrator with a remediation note on
      first failure, succeeds on the second attempt.

Scenario:
  1. Goal created with one subtask: "Create docs/report.md with a
     '## Findings' section". done_condition = regex_in_file checking
     the section header.
  2. First orchestrator pass: writes the file BUT without the heading
     (simulating premature completion / capitulation). Calls
     subtask_update("st_1", "completed").
  3. Gate fires. Validator reports failure (regex not found). Goal
     stays in flight; runner builds a remediation system_note and
     re-enters orchestrator with attempt=2.
  4. Second pass: orchestrator sees the remediation note in its message
     stream. We make the mock LLM "respond" by rewriting the file with
     the heading. Calls subtask_update("st_1", "completed") again.
  5. Gate re-runs. Validator passes. Goal marked done.

The mocked LLM is just a Python callable swapped in for
``orchestrator.run_orchestrator``; we drive plan/file state ourselves
based on the attempt count. This proves the GATE WIRING works — the
loop, the remediation injection, the second-attempt success path — not
that any particular LLM passes the gate.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import config
import db
import goal_runner
import orchestrator


def test_e2e_first_attempt_fails_second_attempt_passes(qwe_temp_data_dir, monkeypatch):
    workspace = Path(config.DATA_DIR) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = workspace / "report.md"
    if report_path.exists():
        report_path.unlink()

    # ── Goal with one subtask carrying a real regex_in_file done_condition ──
    goal_id = db.create_goal(
        user_input="Write docs/report.md with a Findings section",
        source="cli",
    )
    db.set_goal_plan(goal_id, [{
        "title": "Author report.md with the Findings section",
        "description": "Write workspace/report.md including a '## Findings' heading",
        "done_condition": {
            "kind": "regex_in_file",
            "spec": {"path": "report.md", "pattern": r"^## Findings\b"},
        },
    }])

    # ── Mocked orchestrator: behavior depends on attempt count ──
    attempt_log: list[dict] = []

    def _fake_run_orchestrator(*, goal_id, ctx, system_notes=None, **kw):
        attempt = len(attempt_log) + 1
        notes = list(system_notes or [])
        attempt_log.append({"attempt": attempt, "system_notes": notes})

        if attempt == 1:
            # First pass — orchestrator "writes" the file BUT FORGETS the heading.
            # This is the capitulation case: it'll call subtask_update("completed")
            # and try to walk off feeling done. The gate must catch this.
            report_path.write_text(
                "# Report\n\n"
                "Some prose without the required findings heading.\n"
            )
            db.update_subtask(
                goal_id, "st_1", status="completed",
                result_summary="report written",
            )
            return {
                "reply": "Done — wrote report.md.",
                "rounds": 1, "tools_used": ["write_file", "subtask_update"],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            }

        # Second pass — we should have received a system_note explaining the failure.
        # Verify the gate actually injected one with the remediation text.
        assert notes, "second attempt must receive system_notes from the gate"
        note_text = "\n".join(notes)
        assert "ACCEPTANCE GATE" in note_text, (
            f"system_note didn't carry the gate header: {note_text[:200]!r}"
        )
        assert "st_1" in note_text, "remediation should name the failing subtask"
        assert "Findings" in note_text or "regex" in note_text.lower(), (
            "remediation should hint at the validator failure"
        )

        # Now the orchestrator "fixes" the file with the missing heading and
        # re-marks the subtask completed. Validator will pass on the next gate.
        report_path.write_text(
            "# Report\n\n"
            "Some prose.\n\n"
            "## Findings\n\n"
            "- All requirements met.\n"
        )
        db.update_subtask(
            goal_id, "st_1", status="completed",
            result_summary="added Findings section",
        )
        return {
            "reply": "Added the Findings section and re-marked complete.",
            "rounds": 1, "tools_used": ["write_file", "subtask_update"],
            "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
        }

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run_orchestrator)

    # ── Run the goal ──
    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())

    # ── Assertions ──
    # 1. Orchestrator was invoked exactly twice.
    assert len(attempt_log) == 2, f"expected 2 attempts, got {len(attempt_log)}"
    # 2. First attempt got NO system_notes (fresh start).
    assert attempt_log[0]["system_notes"] == []
    # 3. Second attempt DID get a system_note with the gate remediation.
    assert attempt_log[1]["system_notes"], "gate must inject remediation on retry"

    # 4. Goal was marked done (not failed, not paused, not stuck running).
    g = db.get_goal(goal_id)
    assert g["status"] == "done", (
        f"goal should be done after second attempt passes; got {g['status']!r}"
    )

    # 5. Plan reflects the validated state.
    plan = db.get_goal_plan(goal_id)
    st = plan["subtasks"][0]
    assert st["status"] == "completed"
    assert st["validation_passed"] is True
    assert st["last_validation_failure"] is None

    # 6. The actual file on disk has the heading the gate required.
    assert "## Findings" in report_path.read_text()

    # 7. Timeline shows the gate firing on attempt 1.
    events = db.get_goal_events(goal_id)
    types = [e["event_type"] for e in events]
    assert "acceptance_gate_blocked" in types, (
        f"acceptance_gate_blocked event must be logged; got events: {types}"
    )
    # And: subtask was reset to pending by the gate after failure, then
    # re-marked completed on attempt 2 — so the timeline shows both.
    assert types.count("subtask_completed") >= 1, (
        f"subtask_completed event(s) expected; got {types}"
    )


def test_e2e_exhausted_gate_marks_goal_failed(qwe_temp_data_dir, monkeypatch):
    """If the orchestrator never makes the validator pass, after
    MAX_GATE_ATTEMPTS the goal lands in ``failed`` with reason
    ``acceptance_gate_exhausted``. This is the safety stop —
    the orchestrator can't loop forever pretending to fix something."""
    workspace = Path(config.DATA_DIR) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [{
        "title": "Create unreachable.txt",
        "description": "Must exist",
        "done_condition": {
            "kind": "files_exist",
            "spec": {"paths": [str(workspace / "unreachable.txt")]},
        },
    }])

    # Orchestrator NEVER creates the file. Every attempt completes the
    # subtask without producing the artifact — gate keeps failing.
    attempts = []

    def _fake_run_orchestrator(*, goal_id, ctx, system_notes=None, **kw):
        attempts.append(1)
        db.update_subtask(goal_id, "st_1", status="completed",
                          result_summary="claimed done")
        return {"reply": "done?", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run_orchestrator)
    # Force a low cap so we don't spin too long; goal_runner reads via config.get
    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 3)

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())

    # Orchestrator was called exactly MAX_GATE_ATTEMPTS times.
    assert len(attempts) == 3, f"expected 3 attempts (cap), got {len(attempts)}"

    g = db.get_goal(goal_id)
    assert g["status"] == "failed", (
        f"goal must be failed after gate exhaustion; got {g['status']!r}"
    )
    assert "acceptance_gate_exhausted" in (g.get("error") or ""), (
        f"goal.error should mention acceptance_gate_exhausted; got {g.get('error')!r}"
    )

    # And the file STILL doesn't exist on disk — sanity check that we
    # never silently passed it.
    assert not (workspace / "unreachable.txt").exists()
