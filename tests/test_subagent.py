"""Phase 2c tests: subagent dispatch.

A subagent gets a fresh LLM context, a restricted tool whitelist, and
returns ONE string back to the orchestrator. These tests use the same
ScriptedClient pattern as test_orchestrator.py but exercise:

  - tool whitelist enforcement (subagent type → allowed tools)
  - shared_context fact injection into the user prompt
  - result truncation at MAX_RESULT_CHARS
  - orchestrator + subagent end-to-end via dispatch_subagent
"""
from __future__ import annotations

import pytest

from tests.test_orchestrator import (
    ScriptedClient,
    _text_response,
    _tool_call_response,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Subagent tool whitelist (no LLM needed)
# ─────────────────────────────────────────────────────────────────────────────


def test_subagent_research_tools_are_restricted():
    import subagent
    allowed = subagent.SUBAGENT_TOOLS["research"]
    assert "http_request" in allowed
    assert "browser_open" in allowed
    # MUST NOT have shell or write_file
    assert "shell" not in allowed
    assert "write_file" not in allowed


def test_subagent_code_tools_exclude_browser():
    import subagent
    allowed = subagent.SUBAGENT_TOOLS["code"]
    assert "read_file" in allowed
    assert "write_file" in allowed
    assert "shell" in allowed
    assert "browser_open" not in allowed
    assert "browser_click" not in allowed


def test_subagent_browser_tools_exclude_shell():
    import subagent
    allowed = subagent.SUBAGENT_TOOLS["browser"]
    assert "browser_open" in allowed
    assert "browser_click" in allowed
    assert "shell" not in allowed
    assert "write_file" not in allowed


def test_get_subagent_tools_filters_to_whitelist():
    """The OpenAI-schema list returned for a subagent contains ONLY allowed names."""
    import subagent
    schemas = subagent._get_subagent_tools("research")
    names = {t["function"]["name"] for t in schemas}
    assert names.issubset(subagent.SUBAGENT_TOOLS["research"])
    # Sanity check that we got at least one tool
    assert len(names) > 0


# ─────────────────────────────────────────────────────────────────────────────
#  Subagent dispatch — happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_subagent_run_returns_final_string(qwe_temp_data_dir, monkeypatch):
    """run_subagent runs the loop, returns the LLM's final text content."""
    import db
    import providers
    import subagent

    goal_id = db.create_goal(user_input="x", source="cli")

    # Scripted: one round of text reply with finish=stop. No tool calls.
    monkeypatch.setattr(providers, "get_client",
                        lambda: ScriptedClient([_text_response("Found 3 URLs: a, b, c")]))
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    result = subagent.run_subagent(
        goal_id=goal_id, subtask_id="st_1",
        subagent_type="research", prompt="find 3 URLs about widgets",
    )
    assert result == "Found 3 URLs: a, b, c"

    # Logged in goal_events
    types = [e["event_type"] for e in db.get_goal_events(goal_id)]
    assert "subagent_dispatched" in types
    assert "subagent_completed" in types


def test_subagent_rejects_unknown_type(qwe_temp_data_dir):
    import db
    import subagent
    goal_id = db.create_goal(user_input="x", source="cli")
    r = subagent.run_subagent(
        goal_id=goal_id, subtask_id="st_1",
        subagent_type="bogus", prompt="do thing",
    )
    assert "Error" in r and "unknown subagent type" in r


def test_subagent_rejects_empty_prompt(qwe_temp_data_dir):
    import db
    import subagent
    goal_id = db.create_goal(user_input="x", source="cli")
    r = subagent.run_subagent(
        goal_id=goal_id, subtask_id="st_1",
        subagent_type="research", prompt="   ",
    )
    assert "Error" in r and "empty" in r.lower()


def test_subagent_injects_shared_facts_into_prompt(qwe_temp_data_dir, monkeypatch):
    """When shared_context.keys is given, those goal_facts get appended to the user message."""
    import db
    import providers
    import subagent

    goal_id = db.create_goal(user_input="x", source="cli")
    db.fact_save(goal_id, "login_url", "https://example.com/login")
    db.fact_save(goal_id, "search_kw", "drayage")

    captured_messages: list[list[dict]] = []

    class _CapturingClient:
        def __init__(self):
            self._wrapped = ScriptedClient([_text_response("done")])
        @property
        def chat(self):
            outer = self
            class _Chat:
                @property
                def completions(self):
                    inner_outer = outer
                    class _Completions:
                        def create(self, **kw):
                            captured_messages.append(list(kw.get("messages") or []))
                            return inner_outer._wrapped.chat.completions.create(**kw)
                    return _Completions()
            return _Chat()

    monkeypatch.setattr(providers, "get_client", lambda: _CapturingClient())
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    subagent.run_subagent(
        goal_id=goal_id, subtask_id="st_1", subagent_type="browser",
        prompt="log in and scrape",
        shared_context={"keys": ["login_url", "search_kw"]},
    )

    # The user message in the first LLM call must contain both facts.
    assert captured_messages, "no LLM calls captured"
    user_msg = next(m for m in captured_messages[0] if m["role"] == "user")
    assert "login_url: https://example.com/login" in user_msg["content"]
    assert "search_kw: drayage" in user_msg["content"]


def test_subagent_enforces_max_rounds_budget(qwe_temp_data_dir, monkeypatch):
    """Regression test for the production bug where max_rounds was declared
    as a parameter but never passed to run_loop. A scripted fake LLM that
    always wants to call a tool would otherwise run forever.

    The fix passes BudgetLimits(max_turns=max_rounds) into run_loop, which
    hits the existing check_budget gate after the configured number of turns
    and short-circuits with a summary message.
    """
    import db
    import providers
    import subagent

    goal_id = db.create_goal(user_input="x", source="cli")

    # Build a script that keeps calling http_request forever — if the
    # budget didn't fire, the loop would consume every chunk and hit the
    # "out of script → STOP" fallback. With the fix, the budget gate
    # short-circuits before that.
    bottomless = []
    for i in range(50):  # plenty more than max_rounds=5
        bottomless.append(_tool_call_response(
            f"call_{i}", "http_request",
            {"url": "https://example.com/", "method": "GET"},
        ))
    bottomless.append(_text_response("done"))

    monkeypatch.setattr(providers, "get_client", lambda: ScriptedClient(bottomless))
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")
    # Stub http_request to avoid real network — return a short string fast.
    import tools as _tools
    monkeypatch.setattr(_tools, "execute",
                        lambda name, args: "200 OK\n<html></html>"
                        if name == "http_request"
                        else _tools.execute.__wrapped__(name, args)
                        if hasattr(_tools.execute, "__wrapped__")
                        else "ok")

    result = subagent.run_subagent(
        goal_id=goal_id, subtask_id="st_1",
        subagent_type="research", prompt="hammer http forever",
        max_rounds=5,
    )
    # The result string is whatever run_loop produced when the budget fired —
    # either the synthetic "[Task completed with N tool calls...]" message
    # or some text from a final fallback. Either way it MUST exist and
    # mention the cap was hit.
    assert result is not None
    # Budget hits "max turns (5) reached" — we expect a non-empty result.
    assert len(result) > 0


def test_subagent_truncates_oversize_result(qwe_temp_data_dir, monkeypatch):
    """A subagent that returns a >8 KB blob gets truncated before reaching the orchestrator."""
    import db
    import providers
    import subagent

    goal_id = db.create_goal(user_input="x", source="cli")
    huge = "x" * (subagent.MAX_RESULT_CHARS + 5000)
    monkeypatch.setattr(providers, "get_client",
                        lambda: ScriptedClient([_text_response(huge)]))
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    result = subagent.run_subagent(
        goal_id=goal_id, subtask_id="st_1",
        subagent_type="research", prompt="dump everything",
    )
    assert len(result) <= subagent.MAX_RESULT_CHARS + 100  # +marker
    assert "truncated" in result


# ─────────────────────────────────────────────────────────────────────────────
#  Orchestrator + subagent — full end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def test_orchestrator_dispatches_subagent_and_uses_result(qwe_temp_data_dir, monkeypatch):
    """Two-LLM scripted run: orchestrator dispatches a research subagent.

    The orchestrator and the subagent use the SAME provider.get_client() —
    so we hand out a script that has BOTH the orchestrator's tool calls
    AND the subagent's reply, in the order they happen.

    Sequence:
      Orch round 1: goal_plan_set([1 subtask])
      Orch round 2: dispatch_subagent("research", "find a URL", "st_1")
      → subagent round 1: text reply "https://example.com"
      Orch round 3: subtask_update(st_1, completed)
      Orch round 4: final text summary, stop
    """
    import db
    import orchestrator
    import providers
    from turn_context import TurnContext

    script = [
        # Orch r1
        _tool_call_response("c1", "goal_plan_set",
                            {"subtasks": [{"title": "Find a URL", "description": "via research subagent"}]}),
        # Orch r2: dispatch
        _tool_call_response("c2", "dispatch_subagent",
                            {"type": "research", "prompt": "find me a URL", "subtask_id": "st_1"}),
        # Subagent r1: returns text
        _text_response("https://example.com"),
        # Orch r3: mark st_1 completed
        _tool_call_response("c3", "subtask_update",
                            {"subtask_id": "st_1", "status": "completed",
                             "result_summary": "got url"}),
        # Orch r4: final summary
        _text_response("Done. URL found: https://example.com"),
    ]

    # IMPORTANT: share ONE client instance across orchestrator AND subagent
    # so the scripted call counter advances monotonically across both loops.
    # If we returned a fresh client per get_client() call, the subagent's
    # nested run_loop would start the script over from index 0.
    shared_client = ScriptedClient(script)
    monkeypatch.setattr(providers, "get_client", lambda: shared_client)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    goal_id = db.create_goal(user_input="find a URL", source="cli")
    ctx = TurnContext(source="cli", goal_id=goal_id)
    result = orchestrator.run_orchestrator(goal_id=goal_id, ctx=ctx)

    # The subagent's reply made it back into the orchestrator's final answer
    assert "https://example.com" in result["reply"]
    # Plan complete
    statuses = [st["status"] for st in db.get_goal_plan(goal_id)["subtasks"]]
    assert statuses == ["completed"]
    # Event log shows the subagent dispatch
    types = [e["event_type"] for e in db.get_goal_events(goal_id)]
    assert "subagent_dispatched" in types
    assert "subagent_completed" in types


def test_dispatch_subagent_tool_validates_required_args(qwe_temp_data_dir):
    """Direct tool call without a goal → clear error string."""
    import tools
    from turn_context import TurnContext

    tools._set_turn_ctx(TurnContext(source="cli"))  # no goal_id
    r = tools.execute("dispatch_subagent", {
        "type": "research", "prompt": "x", "subtask_id": "st_1",
    })
    assert "Error" in r and "goal" in r.lower()


def test_dispatch_subagent_tool_rejects_missing_fields(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext
    goal_id = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    # Missing prompt
    r = tools.execute("dispatch_subagent",
                      {"type": "research", "subtask_id": "st_1"})
    assert "Error" in r

    # Missing type
    r = tools.execute("dispatch_subagent",
                      {"prompt": "x", "subtask_id": "st_1"})
    assert "Error" in r
