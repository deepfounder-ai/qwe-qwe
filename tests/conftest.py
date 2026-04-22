"""Shared pytest fixtures for the qwe-qwe test suite.

Historical note: several legacy test files used to inject mock modules into
``sys.modules`` at import time (``sys.modules["memory"] = FakeModule()``).
pytest collects every test file before running, so those mocks leaked to
every sibling test. The fix was to replace module-level mutation with
``monkeypatch`` fixtures that auto-revert after each test. This conftest
provides the common fixtures used across the suite.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

# Make repo root importable for every test file (single source of truth —
# legacy files used to each do this themselves).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def qwe_temp_data_dir(monkeypatch):
    """Point QWE_DATA_DIR at a fresh tempdir and reload config/db.

    Yields the Path of the tempdir. Original env + module state are restored
    automatically by ``monkeypatch``; the tempdir itself is removed on exit.
    """
    import importlib

    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_pytest_"))
    monkeypatch.setenv("QWE_DATA_DIR", str(tmp_root))

    # Close any stale DB connection before reload
    if "db" in sys.modules:
        try:
            _local = getattr(sys.modules["db"], "_local", None)
            conn = getattr(_local, "conn", None) if _local else None
            if conn is not None:
                conn.close()
            if _local is not None:
                _local.conn = None
            sys.modules["db"]._migrated = False
        except Exception:
            pass

    # Reload in dependency order
    for mod_name in ("config", "db", "soul", "presets"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)

    try:
        yield tmp_root
    finally:
        # Close the test's DB connection before nuking the dir
        try:
            db_mod = sys.modules.get("db")
            if db_mod is not None:
                _local = getattr(db_mod, "_local", None)
                conn = getattr(_local, "conn", None) if _local else None
                if conn is not None:
                    conn.close()
                if _local is not None:
                    _local.conn = None
                db_mod._migrated = False
        except Exception:
            pass
        shutil.rmtree(tmp_root, ignore_errors=True)
        # Reload core modules against whatever QWE_DATA_DIR is now in effect
        # (monkeypatch will have restored the original value before this runs
        #  in the normal finalizer order — but we also reload here so later
        #  tests don't see state tied to the now-removed tempdir).
        for mod_name in ("config", "db", "soul", "presets"):
            if mod_name in sys.modules:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass


@pytest.fixture
def mock_llm(monkeypatch):
    """Patch providers.get_client() to return a deterministic fake client.

    The fake client exposes ``chat.completions.create(**kw)`` which returns a
    single non-streaming response with text ``"ok"`` and no tool calls. Tests
    that need a specific reply can override via ``mock_llm.reply = "..."``.
    """

    class _FakeMessage:
        def __init__(self, content: str):
            self.content = content
            self.tool_calls = None
            self.role = "assistant"

    class _FakeChoice:
        def __init__(self, content: str):
            self.message = _FakeMessage(content)
            self.finish_reason = "stop"
            self.delta = _FakeMessage(content)

    class _FakeResp:
        def __init__(self, content: str):
            self.choices = [_FakeChoice(content)]
            self.id = "fake"
            self.model = "fake"

    class _FakeCompletions:
        def __init__(self, holder):
            self._holder = holder

        def create(self, **_):
            return _FakeResp(self._holder.reply)

    class _FakeChat:
        def __init__(self, holder):
            self.completions = _FakeCompletions(holder)

    class _FakeClient:
        def __init__(self, holder):
            self.chat = _FakeChat(holder)

    class _Holder:
        reply = "ok"

    holder = _Holder()
    client = _FakeClient(holder)

    import providers
    monkeypatch.setattr(providers, "get_client", lambda: client, raising=False)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model", raising=False)
    return holder
