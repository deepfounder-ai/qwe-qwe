"""Tests for reliability features: retry loop and self-check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock modules before importing agent
import types

mock_db = types.ModuleType("db")
mock_db.kv_get = lambda *a, **kw: None
mock_db.kv_set = lambda *a, **kw: None
mock_db.kv_inc = lambda *a, **kw: 0
mock_db.kv_get_prefix = lambda *a, **kw: {}
mock_db._get_conn = lambda: None
mock_db.get_recent_messages = lambda *a, **kw: []
mock_db.save_message = lambda *a, **kw: None
mock_db.count_messages = lambda *a, **kw: 0
mock_db.execute = lambda *a, **kw: 0
mock_db.fetchall = lambda *a, **kw: []
mock_db.fetchone = lambda *a, **kw: None
sys.modules["db"] = mock_db

mock_memory = types.ModuleType("memory")
mock_memory.search = lambda *a, **kw: []
mock_memory.search_by_vector = lambda *a, **kw: []
mock_memory.search_grouped = lambda *a, **kw: []
mock_memory.recommend = lambda *a, **kw: []
mock_memory.embed = lambda text: [0.0] * 768
mock_memory._sparse_embed = lambda text: types.SimpleNamespace(indices=[0], values=[1.0])
mock_memory.save = lambda *a, **kw: "ok"
mock_memory.delete = lambda *a, **kw: True
mock_memory.cleanup = lambda *a, **kw: 0
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
mock_config.MAX_EXPERIENCE_RESULTS = 2
mock_config.MAX_TOOL_ROUNDS = 10
mock_config.COMPACTION_THRESHOLD = 20
mock_config.THINKING_ENABLED = False
mock_config.WORKSPACE_DIR = __import__("pathlib").Path("/tmp/qwe-test-workspace")
mock_config.DATA_DIR = __import__("pathlib").Path("/tmp/qwe-test-data")
mock_config.EDITABLE_SETTINGS = {
    "tool_retry_max": ("setting:tool_retry_max", int, 3, "Max retries", 0, 5),
    "self_check_enabled": ("setting:self_check_enabled", int, 1, "Self-check", 0, 1),
    "experience_learning": ("setting:experience_learning", int, 1, "Experience", 0, 1),
    "max_memory_results": ("setting:max_memory_results", int, 3, "Memory results", 0, 10),
    "presence_penalty": ("setting:presence_penalty", float, 1.5, "Presence penalty", 0.0, 2.0),
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

mock_tools = types.ModuleType("tools")
mock_tools.TOOLS = [
    {"type": "function", "function": {"name": "shell", "description": "Run shell command",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write a file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
]
mock_tools.get_all_tools = lambda compact=False: mock_tools.TOOLS
mock_tools.execute = lambda *a, **kw: "ok"
sys.modules["tools"] = mock_tools

mock_skills = types.ModuleType("skills")
mock_skills.get_instruction = lambda name: None
sys.modules["skills"] = mock_skills

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
