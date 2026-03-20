"""Tests for shell safety blockers in tools.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# We need to mock db and memory modules since tools.py imports them
import types

# Create mock modules to avoid SQLite/Qdrant initialization
mock_db = types.ModuleType("db")
mock_db.kv_get = lambda *a, **kw: None
mock_db.kv_set = lambda *a, **kw: None
mock_db._get_conn = lambda: None
sys.modules["db"] = mock_db

mock_memory = types.ModuleType("memory")
mock_memory.search = lambda *a, **kw: []
mock_memory.search_by_vector = lambda *a, **kw: []
mock_memory.search_grouped = lambda *a, **kw: []
mock_memory.recommend = lambda *a, **kw: []
mock_memory.embed = lambda text: [0.0] * 768
mock_memory.sparse_embed = lambda text: types.SimpleNamespace(indices=[0], values=[1.0])
mock_memory._sparse_embed = mock_memory.sparse_embed
mock_memory.save = lambda *a, **kw: "ok"
mock_memory.delete = lambda *a, **kw: True
mock_memory.cleanup = lambda *a, **kw: 0
sys.modules["memory"] = mock_memory

mock_logger = types.ModuleType("logger")
mock_logger.get = lambda name: types.SimpleNamespace(
    info=lambda *a, **kw: None,
    debug=lambda *a, **kw: None,
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
sys.modules["config"] = mock_config

import tools


def test_blocks_sudo():
    result = tools.execute("shell", {"command": "sudo apt install something"})
    assert "Blocked" in result


def test_blocks_rm_rf_root():
    result = tools.execute("shell", {"command": "rm -rf /"})
    assert "Blocked" in result


def test_blocks_mkfs():
    result = tools.execute("shell", {"command": "mkfs.ext4 /dev/sda1"})
    assert "Blocked" in result


def test_blocks_dev_redirect():
    result = tools.execute("shell", {"command": "echo x > /dev/sda"})
    assert "Blocked" in result


def test_allows_safe_commands():
    result = tools.execute("shell", {"command": "echo hello"})
    assert "hello" in result
    assert "Blocked" not in result


def test_allows_ls():
    result = tools.execute("shell", {"command": "ls"})
    assert "Blocked" not in result
