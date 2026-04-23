"""Agent-integrity safety rails — irreversible operations are blocked.

Complements ``test_shell_safety.py`` (which covers system-level destruction
like ``rm -rf /``) by asserting that the agent can't wipe out its own
operational state: the SQLite DB, Qdrant memory store, vault secrets,
source tree, or ``.git`` directory.

Design: every block is a hard deny returning a clear reason string. No
confirmation dialog, no ask-permission dance — if the agent tries to do
something irreversible it simply can't.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fresh_tools(qwe_temp_data_dir):
    """Reload tools + config against a clean DB so WRITE_WHITELIST points at the tempdir.

    Important teardown: ``_WRITE_WHITELIST`` is a module-level cache. If
    we leave it populated with paths from the (now-deleted) tempdir, the
    *next* test without this fixture sees a stale whitelist and its
    write_file permission check fails. We clear it at teardown AND
    reload tools so the cache rebuilds against the restored config.
    """
    import importlib
    import sys
    for m in ("config", "tools"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    tools = sys.modules["tools"]
    # Force write whitelist recompute against the freshly-reloaded config
    tools._WRITE_WHITELIST = None
    try:
        yield tools
    finally:
        # qwe_temp_data_dir reloads config on teardown; re-null the cache
        # so the next test computes whitelist against real config, not the
        # tempdir paths we were using.
        tools._WRITE_WHITELIST = None
        # And reload tools on top so downstream tests get a clean module.
        try:
            importlib.reload(sys.modules["tools"])
        except Exception:
            pass


# ── Shell integrity blocks ────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "rm -rf ~/.qwe-qwe",
    "rm -rf $HOME/.qwe-qwe",
    "rm -rf ~/.qwe-qwe/memory",
    "rm -rf ~/.qwe-qwe/vault",
    "rm -rf .git",
    "rm -rf /home/user/project/.git/",
    "rm /home/user/.qwe-qwe/qwe_qwe.db",
    "rm ~/.qwe-qwe/qwe_qwe.db",
    "> ~/.qwe-qwe/qwe_qwe.db",
    "echo x > /home/user/.qwe-qwe/vault.enc",
    "dd if=/dev/zero of=/home/user/.qwe-qwe/qwe_qwe.db",
    "sqlite3 ~/.qwe-qwe/qwe_qwe.db 'DROP TABLE messages'",
    "sqlite3 ~/.qwe-qwe/qwe_qwe.db 'DELETE FROM messages'",
])
def test_shell_blocks_agent_integrity_attacks(fresh_tools, cmd):
    """Every one of these would wipe something irreversible."""
    reason = fresh_tools._check_shell_safety(cmd)
    assert reason is not None, f"cmd should be blocked: {cmd!r}"
    assert "block" in reason.lower()


@pytest.mark.parametrize("cmd", [
    "ls ~/.qwe-qwe",                                 # read-only is fine
    "cat ~/.qwe-qwe/qwe_qwe.db",                     # reading is fine
    "grep foo ~/.qwe-qwe/logs/qwe-qwe.log",          # reading logs
])
def test_shell_allows_read_ops_on_agent_paths(fresh_tools, cmd):
    """Read-only ops against the data dir must still work — we only block writes."""
    assert fresh_tools._check_shell_safety(cmd) is None, (
        f"cmd was blocked but should be allowed: {cmd!r}"
    )


def test_integrity_pattern_distinguishes_qwe_qwe_from_similar_dir_names(fresh_tools):
    """The .qwe-qwe word-boundary must not catch ``~/.qwe-qwe-backup`` etc.

    Uses absolute paths (not ``~/``) to isolate from the legacy broad
    ``rm -rf ~/`` block which catches everything under home.
    """
    # This one SHOULD be caught by our integrity pattern
    assert fresh_tools._AGENT_INTEGRITY_PATTERNS.search("rm -rf /data/.qwe-qwe/memory")
    # This one must NOT — different directory, just prefix-similar
    assert fresh_tools._AGENT_INTEGRITY_PATTERNS.search(
        "rm -rf /data/.qwe-qwe-backup"
    ) is None
    # Same for .qwe-qwe2 (no word-boundary)
    assert fresh_tools._AGENT_INTEGRITY_PATTERNS.search(
        "rm -rf /data/.qwe-qwe2"
    ) is None


# ── write_file integrity blocks via _resolve_path ─────────────────────


def test_write_blocks_sqlite_db(fresh_tools):
    """Direct writes to qwe_qwe.db are blocked even from inside the whitelist."""
    import config
    db_path = config.DATA_DIR / "qwe_qwe.db"
    with pytest.raises(PermissionError, match="qwe_qwe.db"):
        fresh_tools._resolve_path(str(db_path), for_write=True)


def test_write_blocks_sqlite_wal_sidecar(fresh_tools):
    """WAL / SHM sidecars must also be protected (touching them corrupts the DB)."""
    import config
    wal = config.DATA_DIR / "qwe_qwe.db-wal"
    with pytest.raises(PermissionError, match="qwe_qwe.db"):
        fresh_tools._resolve_path(str(wal), for_write=True)


def test_write_blocks_qdrant_memory_store(fresh_tools):
    """Writing into ~/.qwe-qwe/memory/ corrupts every synthesised memory."""
    import config
    target = config.DATA_DIR / "memory" / "collection" / "foo.bin"
    # Make sure the parent exists for the resolve() not to error out before our check
    target.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(PermissionError, match="Qdrant|memory"):
        fresh_tools._resolve_path(str(target), for_write=True)


def test_write_blocks_git_dir(fresh_tools, tmp_path, monkeypatch):
    """Anything under .git/ is off-limits."""
    # Put .git inside the write whitelist so we know the whitelist isn't
    # what's saving us — the block is the integrity reason.
    (tmp_path / ".git").mkdir()
    # Use monkeypatch so _WRITE_WHITELIST reverts when the test finishes —
    # otherwise a stale list leaks into every test after this one.
    monkeypatch.setattr(fresh_tools, "_WRITE_WHITELIST", [str(tmp_path)])
    with pytest.raises(PermissionError, match=".git"):
        fresh_tools._resolve_path(str(tmp_path / ".git" / "HEAD"), for_write=True)


def test_write_blocks_own_source_tree_by_default(fresh_tools, monkeypatch):
    """Agent cannot write to tools.py (or any .py in its own package)."""
    monkeypatch.delenv("QWE_ALLOW_SELF_MODIFY", raising=False)
    tools_py = Path(fresh_tools.__file__).resolve()
    with pytest.raises(PermissionError, match="source tree"):
        fresh_tools._resolve_path(str(tools_py), for_write=True)


def test_write_allows_own_source_tree_when_override_set(fresh_tools, monkeypatch):
    """Users who WANT the agent to refactor qwe-qwe can opt in via env var."""
    monkeypatch.setenv("QWE_ALLOW_SELF_MODIFY", "1")
    # Also add the source tree to the write whitelist — normally it's cwd,
    # but just to be explicit about what we're testing here. monkeypatch
    # auto-reverts so the main suite doesn't get a corrupt whitelist.
    tools_py = Path(fresh_tools.__file__).resolve()
    monkeypatch.setattr(fresh_tools, "_WRITE_WHITELIST", [str(tools_py.parent)])
    # Should not raise
    resolved = fresh_tools._resolve_path(str(tools_py), for_write=True)
    assert resolved == tools_py


def test_write_allows_normal_workspace_file(fresh_tools):
    """Sanity: boring writes still work."""
    import config
    target = config.WORKSPACE_DIR / "note.txt"
    resolved = fresh_tools._resolve_path(str(target), for_write=True)
    assert resolved.name == "note.txt"
