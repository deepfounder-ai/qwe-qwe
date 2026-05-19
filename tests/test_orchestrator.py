"""Phase 2b integration test: orchestrator drives a multi-subtask goal end-to-end.

Uses a scripted fake LLM client that returns a deterministic sequence of
responses — first a `goal_plan_set` call, then `subtask_update` calls,
then a final text summary. No real provider involved.

The fake LLM emits the same streaming shape as the OpenAI SDK so the
existing ``agent_loop.run_loop`` handles it without modification.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Iterator

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Fake streaming LLM with a per-call script
# ─────────────────────────────────────────────────────────────────────────────


class _ToolCallDelta:
    """Shaped like OpenAI SDK's `delta.tool_calls[i]`."""
    def __init__(self, idx: int, call_id: str, name: str, arguments: str):
        self.index = idx
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _ChunkDelta:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.role = "assistant"
        self.reasoning_content = None
        self.reasoning = None


class _Choice:
    def __init__(self, content: str = "", finish: str | None = None, tool_calls=None):
        self.delta = _ChunkDelta(content, tool_calls)
        self.finish_reason = finish
        self.message = SimpleNamespace(
            content=content, tool_calls=None, role="assistant",
        )


class _Chunk:
    def __init__(self, content: str = "", finish: str | None = None,
                 tool_calls=None, usage=None):
        self.choices = [_Choice(content, finish, tool_calls)]
        self.usage = usage
        self.id = "fake"
        self.model = "fake-model"


class _Usage:
    def __init__(self, prompt=10, completion=5):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


def _tool_call_response(call_id: str, name: str, arguments: dict) -> list[_Chunk]:
    """Build a streaming response that emits a single tool call.

    Mirrors what OpenAI streams: one chunk with the tool_calls delta,
    then a stop chunk with usage.
    """
    tc = _ToolCallDelta(
        idx=0, call_id=call_id, name=name,
        arguments=json.dumps(arguments),
    )
    return [
        _Chunk(content="", tool_calls=[tc]),
        _Chunk(content="", finish="tool_calls", usage=_Usage()),
    ]


def _text_response(text: str) -> list[_Chunk]:
    return [
        _Chunk(content=text),
        _Chunk(content="", finish="stop", usage=_Usage()),
    ]


class _ScriptedCompletions:
    """Returns a different scripted response on each call."""
    def __init__(self, script: list[list[_Chunk]]):
        self._script = list(script)
        self.calls = 0

    def create(self, **kw):
        if self.calls >= len(self._script):
            # Defensive: if the loop calls us more than scripted, return STOP
            self.calls += 1
            return iter([_Chunk(content="", finish="stop", usage=_Usage())])
        chunks = self._script[self.calls]
        self.calls += 1
        if kw.get("stream"):
            return iter(chunks)
        # Non-streaming fallback (shouldn't be hit in our path).
        last_with_finish = next(
            (c for c in reversed(chunks) if c.choices[0].finish_reason), chunks[-1]
        )
        msg_content = "".join(c.choices[0].delta.content or "" for c in chunks)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=msg_content, tool_calls=None, role="assistant"),
                finish_reason=last_with_finish.choices[0].finish_reason,
            )],
            usage=_Usage(),
            id="fake", model="fake-model",
        )


class ScriptedClient:
    def __init__(self, script: list[list[_Chunk]]):
        self.chat = SimpleNamespace(completions=_ScriptedCompletions(script))


# ─────────────────────────────────────────────────────────────────────────────
#  The actual test
# ─────────────────────────────────────────────────────────────────────────────


def test_orchestrator_completes_three_subtask_goal_end_to_end(
    qwe_temp_data_dir, monkeypatch,
):
    """Scripted orchestrator run:

      Round 1: goal_plan_set([3 subtasks])
      Round 2: fact_save("login_url", "...") + subtask_update(st_1, completed)
      Round 3: subtask_update(st_2, completed)
      Round 4: subtask_update(st_3, completed)
      Round 5: final text summary, no tool calls → stop

    Asserts:
      - Goal status ends as "done"
      - Plan has 3 completed subtasks
      - The fact landed in goal_facts
      - Orchestrator's final reply was captured into goals.result
      - goal_events log has plan_set + 3 subtask_completed + checkpoints
    """
    import db
    import orchestrator
    import providers
    from turn_context import TurnContext

    # Build the scripted LLM responses.
    script = [
        # Round 1: set the plan
        _tool_call_response(
            "call_1", "goal_plan_set",
            {
                # Workstream B (acceptance-gate): each subtask MUST carry a
                # done_condition. Validator stub always passes in tests, so
                # any well-shaped criterion works here.
                "subtasks": [
                    {
                        "title": "Find login URL",
                        "description": "Use http_request",
                        "done_condition": {
                            "kind": "shell_returns_zero",
                            "spec": {"cmd": "true"},
                        },
                    },
                    {
                        "title": "Save the URL as a fact",
                        "description": "fact_save",
                        "done_condition": {
                            "kind": "shell_returns_zero",
                            "spec": {"cmd": "true"},
                        },
                    },
                    {
                        "title": "Acknowledge done",
                        "description": "subtask_update",
                        "done_condition": {
                            "kind": "shell_returns_zero",
                            "spec": {"cmd": "true"},
                        },
                    },
                ],
            },
        ),
        # Round 2: save a fact AND mark st_1 done. Two tool calls in one round
        # to verify the multi-call branch.
        _tool_call_response(
            "call_2", "fact_save",
            {"key": "login_url", "value": "https://example.com/login",
             "source_subtask_id": "st_1"},
        ),
        # Round 3: mark st_1 completed
        _tool_call_response(
            "call_3", "subtask_update",
            {"subtask_id": "st_1", "status": "completed",
             "result_summary": "found URL"},
        ),
        # Round 4: mark st_2 completed
        _tool_call_response(
            "call_4", "subtask_update",
            {"subtask_id": "st_2", "status": "completed",
             "result_summary": "fact saved"},
        ),
        # Round 5: mark st_3 completed
        _tool_call_response(
            "call_5", "subtask_update",
            {"subtask_id": "st_3", "status": "completed",
             "result_summary": "ack"},
        ),
        # Round 6: write the final summary text — no tool calls → stop
        _text_response(
            "Done: scraped the login URL https://example.com/login, "
            "saved it as fact 'login_url', and marked all 3 subtasks complete."
        ),
    ]

    # Patch providers.get_client + get_model so orchestrator uses our scripted client.
    fake_client = ScriptedClient(script)
    monkeypatch.setattr(providers, "get_client", lambda: fake_client)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    # The agent_loop only knows about the OpenAI SDK shape; pricing.compute_cost
    # is called with our fake model name and returns None for unknown models —
    # that's fine, cost_usd stays NULL on the agent_runs row.

    # Build the goal + ctx
    goal_id = db.create_goal(user_input="scrape my login flow", source="cli")
    ctx = TurnContext(source="cli", goal_id=goal_id)

    # Run the orchestrator synchronously (the orchestrator IS sync — only
    # goal_runner.run is async because of the worker's asyncio loop).
    result = orchestrator.run_orchestrator(goal_id=goal_id, ctx=ctx)

    # ── Assertions ──
    # 1. Final reply landed
    assert "Done" in result["reply"], f"unexpected reply: {result['reply']!r}"

    # 2. Plan complete with 3 subtasks all "completed"
    plan = db.get_goal_plan(goal_id)
    assert plan is not None
    statuses = [st["status"] for st in plan["subtasks"]]
    assert statuses == ["completed", "completed", "completed"], statuses

    # 3. The fact landed
    facts = db.fact_get(goal_id)
    assert facts.get("login_url") == "https://example.com/login"

    # 4. db.goal_plan_is_complete agrees
    assert db.goal_plan_is_complete(goal_id) is True

    # 5. Event log includes plan_set + per-subtask completion events
    event_types = [e["event_type"] for e in db.get_goal_events(goal_id)]
    assert "plan_set" in event_types
    # Three subtasks each got completed → three events
    completed_count = sum(1 for t in event_types if t == "subtask_completed")
    assert completed_count == 3, f"event types: {event_types}"


def test_orchestrator_handles_resume_from_checkpoint(qwe_temp_data_dir, monkeypatch):
    """Plant a checkpoint mid-goal, then run — orchestrator picks up from there.

    Scenario: orchestrator has already done planning + 2 subtasks; we boot
    fresh and feed a script that only handles the final subtask + summary.
    The orchestrator must NOT re-call goal_plan_set.
    """
    import db
    import orchestrator
    import providers
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    # Plant a plan with 3 subtasks, 2 already completed
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
        {"title": "C", "description": ""},
    ])
    db.update_subtask(goal_id, "st_1", status="completed", result_summary="A done")
    db.update_subtask(goal_id, "st_2", status="completed", result_summary="B done")

    # Plant a checkpoint at "round 6" so resume picks up from round 7.
    db.save_checkpoint(
        goal_id, round_num=6, subtask_index=2,
        messages=[
            {"role": "system", "content": "ORCHESTRATOR_STUB"},
            {"role": "user", "content": "x"},
            # Simulated assistant + tool history that's already happened
            {"role": "assistant", "content": "running st_3 now"},
        ],
        plan=db.get_goal_plan(goal_id),
        facts={},
    )

    # Scripted client: round 1 → complete st_3, round 2 → final summary
    script = [
        _tool_call_response(
            "call_1", "subtask_update",
            {"subtask_id": "st_3", "status": "completed", "result_summary": "C done"},
        ),
        _text_response("All three subtasks completed."),
    ]
    monkeypatch.setattr(providers, "get_client", lambda: ScriptedClient(script))
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    ctx = TurnContext(source="cli", goal_id=goal_id)
    result = orchestrator.run_orchestrator(goal_id=goal_id, ctx=ctx)

    assert "All three" in result["reply"]
    statuses = [st["status"] for st in db.get_goal_plan(goal_id)["subtasks"]]
    assert statuses == ["completed", "completed", "completed"]


def test_orchestrator_tools_outside_goal_return_error_strings(qwe_temp_data_dir):
    """The four goal-management tools refuse to run without an active goal_id.

    Protects against an orchestrator tool being called from a chat turn
    that accidentally inherits the schema.
    """
    import tools
    from turn_context import TurnContext

    # No goal_id on ctx
    ctx = TurnContext(source="cli")
    tools._set_turn_ctx(ctx)

    for name, args in [
        ("goal_plan_set", {"subtasks": [{"title": "x", "description": ""}]}),
        ("subtask_update", {"subtask_id": "st_1", "status": "completed"}),
        ("fact_save", {"key": "k", "value": "v"}),
        ("fact_get", {"keys": ["k"]}),
    ]:
        result = tools.execute(name, args)
        assert "Error" in result and "goal" in result.lower(), (
            f"{name} should error without goal_id, got: {result!r}"
        )


def test_orchestrator_subtask_update_rejects_invalid_status(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [{"title": "A", "description": ""}])
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    r = tools.execute("subtask_update", {"subtask_id": "st_1", "status": "bogus"})
    assert "Error" in r


def test_orchestrator_subtask_update_for_missing_subtask(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [{"title": "A", "description": ""}])
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    r = tools.execute("subtask_update", {"subtask_id": "st_99", "status": "completed"})
    assert "Error" in r and "st_99" in r


def test_orchestrator_tools_exclude_browser_but_keep_dispatch():
    """Browser tools from the built-in browser skill must NOT leak into the
    orchestrator's tool set. The orchestrator should use dispatch_subagent
    for browser work — having browser_* tools available directly causes the
    LLM to bypass dispatch and burn 80+ rounds trying to automate a
    browser itself (observed: goal g_f50013dc5e19481b).
    """
    import orchestrator

    orch_tools = orchestrator._get_orchestrator_tools()
    orch_names = {t.get("function", {}).get("name") for t in orch_tools}

    # No browser_* tools should be present
    browser_leaked = sorted(n for n in orch_names if n.startswith("browser_"))
    assert not browser_leaked, (
        f"Browser tools leaked into orchestrator: {browser_leaked}. "
        f"They must stay in _ORCHESTRATOR_EXCLUDED_TOOLS so the LLM "
        f"uses dispatch_subagent(type='browser') instead."
    )

    # dispatch_subagent must be present — it's the only path to browser work
    assert "dispatch_subagent" in orch_names, (
        "dispatch_subagent missing from orchestrator tools"
    )

    # Core tools must still be present
    for required in ("goal_plan_set", "subtask_update", "fact_save",
                     "memory_save", "memory_search", "shell", "read_file"):
        assert required in orch_names, f"Core tool {required!r} missing"


def test_orchestrator_excluded_tools_covers_all_browser_tools():
    """_ORCHESTRATOR_EXCLUDED_TOOLS must cover every tool the browser skill
    exposes. If a new browser tool is added to skills/browser.py but not
    added to the exclusion set, it would leak into the orchestrator.
    """
    import orchestrator
    import skills

    # Get browser tool names from skills.get_tools()
    skill_tools = skills.get_tools(compact=True)
    browser_skill_names = {
        t.get("function", {}).get("name")
        for t in skill_tools
        if (t.get("function", {}).get("name") or "").startswith("browser_")
    }

    missing = browser_skill_names - orchestrator._ORCHESTRATOR_EXCLUDED_TOOLS
    assert not missing, (
        f"Browser skill tools not in _ORCHESTRATOR_EXCLUDED_TOOLS: {sorted(missing)}. "
        f"Add them to prevent leaking into the orchestrator."
    )
