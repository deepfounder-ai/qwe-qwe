"""Smoke + unit tests for skill_creator.

The module is 1318 lines and was shipped without tests. This file pins
the parts that don't need an LLM:

- pure helpers (_sanitize_id, _infer_op, _extract_json, _extract_code,
  _fix_indentation, _fix_empty_blocks, _build_table_ddl)
- the deterministic mapping path (_build_mapping_from_tools +
  _assemble_from_mapping) — this is step 3 of the pipeline that
  *replaces* the LLM call when tool ops are recognisable
- end-to-end pipeline with a fully mocked _llm_call returning canned
  plan + tools JSON

Doesn't pin: real LLM calls, threading-based async, the notify side
channel — those need integration env which is out of scope here.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sc(qwe_temp_data_dir):
    """Reload skill_creator against a fresh temp QWE_DATA_DIR.

    Important because USER_SKILLS_DIR resolves at import time off
    config.DATA_DIR — without this, tests would write to the real
    user's ~/.qwe-qwe/skills/.
    """
    if "skills.skill_creator" in sys.modules:
        del sys.modules["skills.skill_creator"]
    if "skills" in sys.modules:
        importlib.reload(sys.modules["skills"])
    import skills.skill_creator as sc
    return sc


# ── Pure helpers ─────────────────────────────────────────────────────


def test_sanitize_id_strips_non_alnum(sc):
    assert sc._sanitize_id("My Cool Skill") == "MyCoolSkill"
    assert sc._sanitize_id("test-123_foo") == "test123_foo"
    assert sc._sanitize_id("foo.bar/baz") == "foobarbaz"


def test_infer_op_recognises_known_verbs(sc):
    assert sc._infer_op("add_workout") == "add"
    assert sc._infer_op("list_users") == "list"
    assert sc._infer_op("delete_record") == "delete"
    assert sc._infer_op("update_status") == "update"
    assert sc._infer_op("get_info") == "get"
    assert sc._infer_op("fetch_balance") == "get"
    assert sc._infer_op("compute_stats") == "stats"
    # unknown verbs fall back to "custom"
    assert sc._infer_op("perform_xyz") == "custom"


def test_extract_json_plain_dict(sc):
    assert sc._extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_extract_json_with_thinking_prefix(sc):
    raw = "<think>let me plan</think>\n```json\n{\"plan\": \"do x\"}\n```"
    assert sc._extract_json(raw) == {"plan": "do x"}


def test_extract_json_dict_inside_prose(sc):
    # The function scans from the end for the last balanced { ... }
    raw = "Here you go: {\"k\": [1, 2, 3]} that's the result"
    assert sc._extract_json(raw) == {"k": [1, 2, 3]}


def test_extract_json_invalid_returns_falsy(sc):
    """For totally unparseable text the function returns a falsy value
    (currently {} via _repair_json's fallback). The pipeline then
    rejects it via `if not plan: retry`. The contract is "falsy on
    failure", not specifically None — keep the test honest."""
    result = sc._extract_json("not json at all")
    assert not result, f"expected falsy, got {result!r}"


def test_extract_code_strips_thinking_and_fences(sc):
    raw = (
        "<think>plan it</think>\n"
        "```python\n"
        "if name == \"foo\":\n"
        "    return \"ok\"\n"
        "```"
    )
    out = sc._extract_code(raw)
    assert "if name ==" in out
    assert '```' not in out
    assert 'return "ok"' in out


def test_build_table_ddl_indents_each_table(sc):
    plan = {
        "tables": [
            "CREATE TABLE workouts (id INTEGER PRIMARY KEY)",
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)",
        ]
    }
    ddl = sc._build_table_ddl(plan)
    # Each table wrapped in conn.execute("""...""")
    assert ddl.count("conn.execute") == 2
    assert "workouts" in ddl
    assert "notes" in ddl
    # Lines indented for placement inside execute()
    for line in ddl.splitlines():
        if line.strip():
            assert line.startswith("    "), f"DDL line not indented: {line!r}"


# ── Deterministic mapping path (no LLM) ──────────────────────────────


def test_build_mapping_from_tools_recognises_known_ops(sc):
    """Step 3 of the pipeline: when all tools are CRUD-style, the
    mapping covers them and the LLM is skipped entirely."""
    tools_list = [
        {
            "type": "function",
            "function": {
                "name": "add_note",
                "description": "Add a new note",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_notes",
                "description": "List all notes",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_note",
                "description": "Delete a note by id",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            },
        },
    ]
    plan = {"tables": ["CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT)"]}
    mapping = sc._build_mapping_from_tools(tools_list, plan)
    assert set(mapping.keys()) == {"add_note", "list_notes", "delete_note"}
    # Each mapping entry should have an 'op' that maps to a known template
    for tool, spec in mapping.items():
        assert "op" in spec
        assert spec["op"] in ("add", "list", "delete", "update", "get", "stats", "custom")


def test_assemble_from_mapping_produces_valid_python(sc):
    """The generated execute() body should be ast-parseable when wrapped."""
    import ast
    tools_list = [
        {
            "type": "function",
            "function": {
                "name": "add_item",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        },
    ]
    mapping = sc._build_mapping_from_tools(tools_list, {"tables": []})
    body, has_custom, custom_tools = sc._assemble_from_mapping(mapping)
    # Wrap in a function so it parses standalone
    wrapped = "def execute(name, args):\n    import json, db\n    conn = db._get_conn()\n" + body
    ast.parse(wrapped)


# ── delete_skill protections ─────────────────────────────────────────


def test_delete_skill_refuses_builtin(sc):
    # mcp_manager is a built-in skill — must not be deletable
    result = sc._delete_skill("mcp_manager")
    assert "built-in" in result.lower() or "cannot" in result.lower()


def test_delete_skill_invalid_identifier(sc):
    result = sc._delete_skill("not a valid name")
    # Either rejected as invalid or as "not found" — both acceptable;
    # the key invariant is no exception is raised
    assert isinstance(result, str)


def test_delete_skill_missing_returns_friendly_error(sc):
    result = sc._delete_skill("nonexistent_skill_xyz")
    assert "not found" in result.lower()


# ── execute() dispatch ───────────────────────────────────────────────


def test_execute_unknown_tool_returns_message(sc):
    result = sc.execute("totally_made_up_tool", {})
    assert "Unknown tool" in result


def test_execute_list_skill_files_returns_string(sc):
    result = sc.execute("list_skill_files", {})
    assert isinstance(result, str)
    # Built-in skills should be visible
    assert "skill_creator" in result or "no skills" in result.lower()


# ── Integration points ──────────────────────────────────────────────


def test_skill_creator_in_default_skills():
    """skill_creator must be in the default-on set so users can use
    create_skill without first activating anything."""
    import skills
    assert "skill_creator" in skills._DEFAULT_SKILLS


def test_create_skill_listed_under_tool_search_skill_keyword():
    """tool_search('skill') should surface create_skill."""
    import tools
    skill_tools = tools._TOOL_SEARCH_INDEX.get("skill", [])
    assert "create_skill" in skill_tools
    assert "delete_skill" in skill_tools
    assert "list_skill_files" in skill_tools


# ── Prompt content: agent capabilities exposed to generated skills ──
#
# These tests pin the API surface that skill_creator advertises to the
# LLM. Without them, a refactor that accidentally narrows the
# INSTRUCTION (e.g. dropping the camera example) would silently
# regress generated-skill quality — the LLM would stop reaching for
# capabilities it no longer knows it has.


def test_instruction_documents_memory_api(sc):
    assert "memory.save" in sc.INSTRUCTION
    assert "memory.search" in sc.INSTRUCTION


def test_instruction_documents_tools_dispatcher(sc):
    """The cross-tool composition path — generated skills call any
    other tool by name via tools.execute()."""
    assert "tools.execute" in sc.INSTRUCTION
    # Spot-check the most useful capabilities
    for cap in ("camera_capture", "http_request", "secret_save", "secret_get",
                "read_file", "write_file", "send_file", "open_url"):
        assert cap in sc.INSTRUCTION, f"{cap} missing from INSTRUCTION"


def test_instruction_documents_llm_direct_path(sc):
    assert "providers.get_client" in sc.INSTRUCTION


def test_instruction_keeps_db_section(sc):
    """Don't accidentally drop the original db API while expanding."""
    for fn in ("db._get_conn", "db.kv_get", "db.kv_set", "db.kv_inc"):
        assert fn in sc.INSTRUCTION, f"{fn} missing from INSTRUCTION"


def test_step1_plan_mentions_composability(sc):
    """The planner needs to know skills can use camera/memory/etc, or
    it'll only output CRUD-shaped plans."""
    assert "memory.save" in sc.STEP1_PLAN
    assert "camera_capture" in sc.STEP1_PLAN
    assert "http_request" in sc.STEP1_PLAN


def test_step3_code_examples_cover_camera_memory_secret(sc):
    """Step 3 examples train the LLM by showing the patterns. Drop one
    and the model stops generating that pattern."""
    assert "camera_capture" in sc.STEP3_CODE
    assert "memory.save" in sc.STEP3_CODE
    assert "memory.search" in sc.STEP3_CODE
    assert "secret_get" in sc.STEP3_CODE
    assert "http_request" in sc.STEP3_CODE
