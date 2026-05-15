"""Goals can use user-installed skill tools.

Before this commit, both orchestrator and subagents had strict whitelists
that filtered OUT skill tools (canvas, weather, user-created skills like
linkedin_lead_gen, etc) and MCP tools. The user rightly asked: "can Goals
use skill tools?" — and the answer was "no, that's a gap".

Fix:
  1. Orchestrator: includes all skill + MCP tools by default
     (user-installed = trusted).
  2. dispatch_subagent: new ``extra_tools`` param widens the subagent's
     whitelist per dispatch — orchestrator can expose a domain-specific
     skill tool to a generic subagent type.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
#  Orchestrator: skill tools auto-included
# ─────────────────────────────────────────────────────────────────────────────


def test_orchestrator_includes_skill_tools(qwe_temp_data_dir, monkeypatch):
    """Orchestrator's tool list contains skill tools beyond the core whitelist."""
    import orchestrator
    import tools

    # Inject a fake "skill tool" alongside the real ones.
    real_all_tools = tools._get_all_tools_full

    fake_skill_tool = {
        "type": "function",
        "function": {
            "name": "linkedin_lead_gen_search",
            "description": "Search LinkedIn for leads via a user-installed skill.",
            "parameters": {"type": "object", "properties": {
                "keywords": {"type": "string"}
            }},
        },
    }

    def _fake_all():
        return list(real_all_tools()) + [fake_skill_tool]

    monkeypatch.setattr(tools, "_get_all_tools_full", _fake_all)

    orch_tools = orchestrator._get_orchestrator_tools()
    names = {t["function"]["name"] for t in orch_tools}

    # Core whitelist still present
    assert "goal_plan_set" in names
    assert "dispatch_subagent" in names
    # The new skill tool got picked up automatically
    assert "linkedin_lead_gen_search" in names


def test_orchestrator_excludes_non_whitelisted_core_tools(qwe_temp_data_dir):
    """Core tools (in tools.TOOLS) that aren't in the orchestrator whitelist
    must NOT be exposed. Browser tools ARE skill tools (from skills/browser.py)
    so they're auto-included via the skill route — that's intentional.

    What we DO want gated: core tools like camera_capture, tool_search,
    self_config — they're either subagent-only or chat-only."""
    import orchestrator
    orch_tools = orchestrator._get_orchestrator_tools()
    names = {t["function"]["name"] for t in orch_tools}
    # These core tools are deliberately off-limits to the orchestrator.
    # tool_search expands the chat agent's visible tool set — orchestrator
    # already gets full skill access, so this would just confuse it.
    assert "tool_search" not in names
    # camera_capture is a chat-mode tool (live WS connection required).
    assert "camera_capture" not in names
    # self_config mutates global agent settings — too dangerous for goals.
    assert "self_config" not in names


def test_orchestrator_dedupes_skill_tools(qwe_temp_data_dir, monkeypatch):
    """If the same tool name appears twice (e.g. a skill shadowing a core
    tool), keep only the first occurrence — no duplicate entries in the
    OpenAI request body."""
    import orchestrator
    import tools

    duplicate = {
        "type": "function",
        "function": {
            "name": "memory_save",  # name collision with the core tool
            "description": "Override of memory_save",
            "parameters": {"type": "object"},
        },
    }
    real = tools._get_all_tools_full
    monkeypatch.setattr(tools, "_get_all_tools_full",
                        lambda: list(real()) + [duplicate])
    result = orchestrator._get_orchestrator_tools()
    names = [t["function"]["name"] for t in result]
    assert names.count("memory_save") == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Subagent: extra_tools widens the whitelist per dispatch
# ─────────────────────────────────────────────────────────────────────────────


def test_subagent_extra_tools_expand_whitelist(qwe_temp_data_dir, monkeypatch):
    """Passing extra_tools=['linkedin_lead_gen_search'] to a browser subagent
    must make that tool available alongside the base browser_* set."""
    import subagent
    import tools

    fake_skill = {
        "type": "function",
        "function": {
            "name": "linkedin_lead_gen_search",
            "description": "X",
            "parameters": {"type": "object"},
        },
    }
    real = tools._get_all_tools_full
    monkeypatch.setattr(tools, "_get_all_tools_full",
                        lambda: list(real()) + [fake_skill])

    schemas = subagent._get_subagent_tools(
        "browser", extra_tools=["linkedin_lead_gen_search"]
    )
    names = {t["function"]["name"] for t in schemas}
    # Base browser tools still there
    assert "browser_open" in names
    assert "browser_click" in names
    # Plus the extra skill we asked for
    assert "linkedin_lead_gen_search" in names


def test_subagent_without_extra_tools_unchanged(qwe_temp_data_dir):
    """Default subagent tool sets must be unchanged when extra_tools is empty."""
    import subagent
    base = subagent._get_subagent_tools("browser")
    base_names = {t["function"]["name"] for t in base}
    # No leakage of arbitrary tool names from the broader registry
    assert "linkedin_lead_gen_search" not in base_names
    assert "weather_get" not in base_names
    # But the type's own tools must be present
    assert "browser_open" in base_names


def test_subagent_extra_tools_invalid_input_ignored(qwe_temp_data_dir):
    """Non-string entries in extra_tools must be silently dropped, not crash."""
    import subagent
    # extra_tools with various junk — should still produce a valid schema list
    result = subagent._get_subagent_tools(
        "browser", extra_tools=["browser_open", None, 42, "", "   "]
    )
    names = {t["function"]["name"] for t in result}
    # The valid entry is honoured; junk is dropped without raising.
    assert "browser_open" in names


def test_dispatch_subagent_tool_passes_extra_tools(qwe_temp_data_dir, monkeypatch):
    """End-to-end: dispatch_subagent({extra_tools: [...]}) reaches subagent.run_subagent."""
    import db
    import subagent
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    captured: dict = {}

    def _fake_run(**kw):
        captured.update(kw)
        return "done"

    monkeypatch.setattr(subagent, "run_subagent", _fake_run)

    result = tools.execute("dispatch_subagent", {
        "type": "browser",
        "prompt": "go",
        "subtask_id": "st_1",
        "extra_tools": ["linkedin_lead_gen_search", "linkedin_lead_gen_save"],
    })
    assert result == "done"
    assert captured["extra_tools"] == [
        "linkedin_lead_gen_search", "linkedin_lead_gen_save"
    ]
