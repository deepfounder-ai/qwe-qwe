"""Tests for reliability features: retry loop and self-check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock modules before importing agent
import types

mock_db = types.ModuleType("db")
mock_db.kv_get = lambda *a, **kw: None
mock_db.kv_set = lambda *a, **kw: None
mock_db._get_conn = lambda: None
mock_db.get_recent_messages = lambda *a, **kw: []
mock_db.save_message = lambda *a, **kw: None
mock_db.count_messages = lambda *a, **kw: 0
sys.modules["db"] = mock_db

mock_memory = types.ModuleType("memory")
mock_memory.search = lambda *a, **kw: []
mock_memory.save = lambda *a, **kw: "ok"
mock_memory.delete = lambda *a, **kw: True
sys.modules["memory"] = mock_memory

mock_logger = types.ModuleType("logger")
mock_logger.get = lambda name: types.SimpleNamespace(
    info=lambda *a, **kw: None,
    warning=lambda *a, **kw: None,
    error=lambda *a, **kw: None,
)
mock_logger.event = lambda *a, **kw: None
sys.modules["logger"] = mock_logger

mock_config = types.ModuleType("config")
mock_config.LLM_BASE_URL = "http://localhost:1234/v1"
mock_config.LLM_MODEL = "test"
mock_config.LLM_API_KEY = "test"
mock_config.EMBED_BASE_URL = "http://localhost:1234/v1"
mock_config.EMBED_MODEL = "test"
mock_config.EMBED_API_KEY = "test"
mock_config.EMBED_DIM = 768
mock_config.QDRANT_MODE = "memory"
mock_config.QDRANT_PATH = "./memory"
mock_config.QDRANT_URL = "http://localhost:6333"
mock_config.QDRANT_COLLECTION = "test"
mock_config.DB_PATH = ":memory:"
mock_config.TZ_OFFSET = 0
mock_config.MAX_HISTORY_MESSAGES = 4
mock_config.MAX_MEMORY_RESULTS = 3
mock_config.MAX_TOOL_ROUNDS = 10
mock_config.COMPACTION_THRESHOLD = 20
mock_config.THINKING_ENABLED = False
mock_config.EDITABLE_SETTINGS = {
    "tool_retry_max": ("setting:tool_retry_max", int, 3, "Max retries", 0, 5),
    "self_check_enabled": ("setting:self_check_enabled", int, 1, "Self-check", 0, 1),
}
mock_config.get = lambda key: mock_config.EDITABLE_SETTINGS[key][2]
sys.modules["config"] = mock_config

mock_providers = types.ModuleType("providers")
mock_providers.get_model = lambda: "test-model"
mock_providers.get_client = lambda: None
sys.modules["providers"] = mock_providers

mock_soul = types.ModuleType("soul")
mock_soul.load = lambda: {}
mock_soul.to_prompt = lambda s: "test"
sys.modules["soul"] = mock_soul

mock_threads = types.ModuleType("threads")
mock_threads.get_active_id = lambda: "test"
mock_threads.get = lambda tid: None
mock_threads.touch = lambda tid: None
sys.modules["threads"] = mock_threads

import agent


# ── Tests for _get_tool_schema ──

def test_get_tool_schema_known():
    schema = agent._get_tool_schema("shell")
    assert schema is not None
    assert "command" in schema.get("properties", {})


def test_get_tool_schema_unknown():
    schema = agent._get_tool_schema("nonexistent_tool")
    assert schema is None


# ── Tests for _repair_json (already tested, but verify integration) ──

def test_repair_json_trailing_comma():
    result = agent._repair_json('{"command": "ls",}')
    assert result == {"command": "ls"}


def test_repair_json_empty():
    result = agent._repair_json("")
    assert result == {}


# ── Tests for _SELF_CHECK_TOOLS ──

def test_self_check_tools_list():
    assert "shell" in agent._SELF_CHECK_TOOLS
    assert "write_file" in agent._SELF_CHECK_TOOLS
    assert "memory_search" not in agent._SELF_CHECK_TOOLS
    assert "read_file" not in agent._SELF_CHECK_TOOLS


# ── Tests for TurnResult new fields ──

def test_turn_result_has_reliability_fields():
    r = agent.TurnResult()
    assert r.json_repairs == 0
    assert r.retry_successes == 0
    assert r.self_check_fixes == 0
