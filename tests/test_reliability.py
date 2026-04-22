"""Tests for reliability features: retry loop and self-check.

Uses the real agent module. No module-level sys.modules mutation — the
per-test fixtures below supply what's needed through monkeypatch.
"""

import pytest


@pytest.fixture
def agent_mod():
    """Import the real agent module (no mocks needed for these assertions)."""
    import agent
    return agent


# ── Tests for _get_tool_schema ──

def test_get_tool_schema_known(agent_mod):
    schema = agent_mod._get_tool_schema("shell")
    assert schema is not None
    assert "command" in schema.get("properties", {})


def test_get_tool_schema_unknown(agent_mod):
    schema = agent_mod._get_tool_schema("nonexistent_tool")
    assert schema is None


# ── Tests for _repair_json (already tested, but verify integration) ──

def test_repair_json_trailing_comma(agent_mod):
    result = agent_mod._repair_json('{"command": "ls",}')
    assert result == {"command": "ls"}


def test_repair_json_empty(agent_mod):
    result = agent_mod._repair_json("")
    assert result == {}


# ── Tests for _SELF_CHECK_TOOLS ──

def test_self_check_tools_list(agent_mod):
    assert "shell" in agent_mod._SELF_CHECK_TOOLS
    assert "write_file" in agent_mod._SELF_CHECK_TOOLS
    assert "memory_search" not in agent_mod._SELF_CHECK_TOOLS
    assert "read_file" not in agent_mod._SELF_CHECK_TOOLS


# ── Tests for TurnResult new fields ──

def test_turn_result_has_reliability_fields(agent_mod):
    r = agent_mod.TurnResult()
    assert r.json_repairs == 0
    assert r.retry_successes == 0
    assert r.self_check_fixes == 0
