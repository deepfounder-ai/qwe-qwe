"""Tests for db.py backup/restore/graceful-shutdown functions."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


# ── take_backup ──────────────────────────────────────────────────────────────

def test_take_backup_creates_file(qwe_temp_data_dir):
    import db
    db._get_conn()
    result = db.take_backup("test")
    assert result is not None
    assert result.exists()
    assert result.name.endswith("_test.db")


def test_take_backup_is_readable(qwe_temp_data_dir):
    import db
    db._get_conn()
    path = db.take_backup("readcheck")
    assert path is not None
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    assert any(r[0] == "kv" for r in rows)


def test_take_backup_prunes_old(qwe_temp_data_dir, monkeypatch):
    import db
    db._get_conn()
    monkeypatch.setattr(db, "MAX_BACKUPS", 3)
    for i in range(5):
        db.take_backup(f"b{i}")
    backup_dir = Path(db.config.DATA_DIR) / "db_backups"
    remaining = list(backup_dir.glob("castor_*.db"))
    assert len(remaining) <= 3


# ── latest_backup ─────────────────────────────────────────────────────────────

def test_latest_backup_none_when_no_backups(qwe_temp_data_dir):
    import db
    assert db.latest_backup() is None


def test_latest_backup_returns_most_recent(qwe_temp_data_dir):
    import db
    db._get_conn()
    db.take_backup("first")
    time.sleep(0.02)
    second = db.take_backup("second")
    assert db.latest_backup() == second


# ── check_and_restore ─────────────────────────────────────────────────────────

def test_check_and_restore_ok_on_fresh_db(qwe_temp_data_dir):
    import db
    db._get_conn()
    assert db.check_and_restore() is True


def test_check_and_restore_ok_when_no_db_file(qwe_temp_data_dir):
    import db
    import config
    p = Path(config.DB_PATH)
    if p.exists():
        p.unlink()
    assert db.check_and_restore() is True


def test_check_and_restore_detects_corruption(qwe_temp_data_dir):
    import db
    import config
    db._get_conn()
    # Close the connection properly before corrupting so the WAL is flushed/closed
    conn = db._local.conn
    if conn is not None:
        conn.close()
    db._local.conn = None
    db._migrated = False
    db._integrity_checked = False
    db_path = Path(config.DB_PATH)
    db_path.write_bytes(b"THIS IS NOT A SQLITE DATABASE" * 100)
    # Remove WAL/SHM sidecars so SQLite cannot recover from them and mask corruption
    for ext in ("-wal", "-shm"):
        sidecar = db_path.parent / (db_path.name + ext)
        if sidecar.exists():
            sidecar.unlink()
    assert db.check_and_restore() is False  # no backup → returns False


def test_check_and_restore_restores_from_backup(qwe_temp_data_dir):
    import db
    import config
    db._get_conn()
    db.kv_set("canary", "alive")
    db.take_backup("before_corrupt")

    # Close the connection properly before corrupting so the WAL is flushed/closed
    conn = db._local.conn
    if conn is not None:
        conn.close()
    # Reset ALL module-level state so check_and_restore() fires again
    db._local.conn = None
    db._migrated = False
    db._integrity_checked = False  # critical: without this the guard skips the check

    # Corrupt the database and remove WAL/SHM sidecars so SQLite cannot recover
    # from them and mask the corruption before check_and_restore() probes
    db_path = Path(config.DB_PATH)
    db_path.write_bytes(b"CORRUPTED" * 200)
    for ext in ("-wal", "-shm"):
        sidecar = db_path.parent / (db_path.name + ext)
        if sidecar.exists():
            sidecar.unlink()

    result = db.check_and_restore()
    assert result is True
    # After restore, DB should be readable and canary value present
    db._local.conn = None
    db._migrated = False
    assert db.kv_get("canary") == "alive"


# ── graceful_shutdown ─────────────────────────────────────────────────────────

def test_graceful_shutdown_does_not_raise(qwe_temp_data_dir):
    import db
    db._get_conn()
    db.graceful_shutdown()  # must not raise


def test_graceful_shutdown_closes_connection(qwe_temp_data_dir):
    import db
    db._get_conn()
    db.graceful_shutdown()
    assert getattr(db._local, "conn", None) is None


# ── start_backup_scheduler ────────────────────────────────────────────────────

def test_start_backup_scheduler_is_idempotent(qwe_temp_data_dir, monkeypatch):
    import db
    import threading
    monkeypatch.setattr(db, "_backup_thread_started", False)
    db._get_conn()
    before = [t for t in threading.enumerate() if t.name == "db-backup"]
    db.start_backup_scheduler()
    after_first = [t for t in threading.enumerate() if t.name == "db-backup"]
    db.start_backup_scheduler()  # second call must not raise or spawn extra threads
    after_second = [t for t in threading.enumerate() if t.name == "db-backup"]
    # exactly one new thread was spawned by the first call
    assert len(after_first) == len(before) + 1
    # second call is a no-op — no additional thread
    assert len(after_second) == len(after_first)


# ── server.py wiring ──────────────────────────────────────────────────────────

def test_lifespan_starts_backup_scheduler(qwe_temp_data_dir, monkeypatch):
    """server lifespan startup must call db.start_backup_scheduler()."""
    import db
    calls = []
    monkeypatch.setattr(db, "start_backup_scheduler", lambda: calls.append(1))
    import server
    import asyncio
    async def _run():
        async with server.lifespan(server.app):
            pass
    asyncio.run(_run())
    assert len(calls) >= 1, "start_backup_scheduler was not called from lifespan"


def test_lifespan_calls_graceful_shutdown(qwe_temp_data_dir, monkeypatch):
    """server lifespan shutdown block must call db.graceful_shutdown()."""
    import db
    calls = []
    monkeypatch.setattr(db, "graceful_shutdown", lambda: calls.append(1))
    import server
    import asyncio
    async def _run():
        async with server.lifespan(server.app):
            pass
    asyncio.run(_run())
    assert len(calls) >= 1, "graceful_shutdown was not called from lifespan shutdown"


# ── FTS5 query escaping ─────────────────────────────────────────────────────

class TestFtsEscape:
    """Tests for db._fts_escape() — FTS5 special-char stripping."""

    def test_normal_words(self):
        from db import _fts_escape
        assert _fts_escape("hello world") == '"hello" "world"'

    def test_embedded_double_quotes(self):
        from db import _fts_escape
        result = _fts_escape('foo"bar')
        assert '"' not in result.replace('"foobar"', "")  # quotes only as delimiters
        assert result == '"foobar"'

    def test_fts5_operators_stripped(self):
        from db import _fts_escape
        # NEAR, *, : are FTS5 operators — must be stripped from words
        result = _fts_escape("col:value test*")
        assert "*" not in result
        assert ":" not in result
        assert '"colvalue"' in result
        assert '"test"' in result

    def test_parentheses_stripped(self):
        from db import _fts_escape
        result = _fts_escape("(hello) {world}")
        assert "(" not in result
        assert ")" not in result
        assert "{" not in result
        assert "}" not in result
        assert '"hello"' in result
        assert '"world"' in result

    def test_caret_stripped(self):
        from db import _fts_escape
        result = _fts_escape("^boost term")
        assert "^" not in result
        assert '"boost"' in result

    def test_all_special_returns_empty_quoted(self):
        from db import _fts_escape
        # Input that becomes empty after stripping
        result = _fts_escape('"*:^(){}')
        assert result == '""'

    def test_empty_input(self):
        from db import _fts_escape
        assert _fts_escape("") == '""'

    def test_mixed_clean_and_dirty(self):
        from db import _fts_escape
        result = _fts_escape('normal "quoted" special*')
        assert '"normal"' in result
        assert '"quoted"' in result
        assert '"special"' in result
        assert "*" not in result
