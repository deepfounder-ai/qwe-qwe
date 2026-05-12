"""Tests for the SQLite migration runner in db.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_db(qwe_temp_data_dir):  # noqa: ARG001
    """qwe_temp_data_dir already reloads db.py against a fresh DATA_DIR;
    this helper just returns the module."""
    import db
    return db


def _latest_migration_number(db_mod) -> int:
    migs = db_mod._list_migrations()
    assert migs, "expected at least one migration file on disk"
    return migs[-1][0]


# ---------------------------------------------------------------------------
# Fresh-DB path
# ---------------------------------------------------------------------------

def test_fresh_db_applies_all_migrations(qwe_temp_data_dir):
    db = _reload_db(qwe_temp_data_dir)
    conn = db._get_conn()
    expected = _latest_migration_number(db)

    current = db._read_schema_version(conn)
    assert current == expected, (
        f"fresh DB should be stamped at the latest version ({expected}), got {current}"
    )

    # All baseline tables exist.
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for required in ("messages", "kv", "presets", "threads",
                     "scheduled_tasks", "secrets"):
        assert required in tables, f"baseline table {required!r} missing"

    # The composite index from migration 002 must be present.
    idx = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_messages_thread_ts'"
    ).fetchone()
    assert idx is not None, "expected idx_messages_thread_ts to exist after 002"


# ---------------------------------------------------------------------------
# Backward-compat stamping
# ---------------------------------------------------------------------------

def test_existing_install_gets_stamped_without_rerunning_baseline(qwe_temp_data_dir):
    """A DB that has tables but no schema_version must be marked as v1
    automatically, and further migrations applied on top."""
    import config
    import db

    # Simulate a legacy install: create the messages table directly, bypassing
    # the migration runner, before the runner ever sees this DB.
    raw = sqlite3.connect(config.DB_PATH)
    # Shape matches what threads.py's legacy ALTER left on existing installs:
    # a messages table with thread_id already present.
    raw.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT,
            ts REAL NOT NULL,
            thread_id TEXT DEFAULT 'default'
        );
        CREATE TABLE kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            ts REAL NOT NULL
        );
        INSERT INTO messages (role, content, ts) VALUES ('user', 'legacy data', 1.0);
        """
    )
    raw.commit()
    raw.close()

    # Force the in-memory flag to re-run migrations on the next _get_conn call.
    db._migrated = False
    db._local.conn = None

    conn = db._get_conn()

    # Version should be bumped to latest, and the legacy row must still be there.
    expected = _latest_migration_number(db)
    assert db._read_schema_version(conn) == expected

    row = conn.execute("SELECT content FROM messages WHERE role='user'").fetchone()
    assert row is not None and row[0] == "legacy data", \
        "legacy data must survive the migration stamp"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_reapply_is_noop(qwe_temp_data_dir):
    db = _reload_db(qwe_temp_data_dir)
    conn = db._get_conn()
    before = db._read_schema_version(conn)

    # Run the runner again directly. schema_version must not change and no
    # exception must be raised (every migration is already applied).
    db._apply_migrations(conn)
    after = db._read_schema_version(conn)
    assert before == after, "re-running migrations must not change schema_version"


# ---------------------------------------------------------------------------
# Failure atomicity
# ---------------------------------------------------------------------------

def test_invalid_migration_raises_and_preserves_version(qwe_temp_data_dir, tmp_path,
                                                        monkeypatch):
    db = _reload_db(qwe_temp_data_dir)
    conn = db._get_conn()
    stable_version = db._read_schema_version(conn)

    # Point the runner at a fake migrations dir that contains ONE file with
    # garbage SQL. Version number is higher than anything real, so the runner
    # will try to apply it.
    fake_dir = tmp_path / "bogus_migrations"
    fake_dir.mkdir()
    (fake_dir / "999_boom.sql").write_text("THIS IS NOT VALID SQL;")

    monkeypatch.setattr(db, "MIGRATIONS_DIR", fake_dir)

    with pytest.raises(sqlite3.Error):
        db._apply_migrations(conn)

    assert db._read_schema_version(conn) == stable_version, (
        "a failing migration must leave schema_version unchanged"
    )


# ---------------------------------------------------------------------------
# Migration discovery
# ---------------------------------------------------------------------------

def test_migrations_dir_present_and_parseable():
    """Smoke test: on-disk layout matches the NNN_name.sql convention."""
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    assert migrations_dir.is_dir(), "migrations/ directory must exist at repo root"

    import db
    migs = db._list_migrations()
    assert len(migs) >= 1
    # Numbers must be strictly monotonically increasing and start from 1.
    nums = [v for v, _ in migs]
    assert nums == sorted(nums)
    assert len(set(nums)) == len(nums), "duplicate migration numbers"
    assert nums[0] == 1, "first migration should be 001"


# ---------------------------------------------------------------------------
# Migration 008 — agent_runs table
# ---------------------------------------------------------------------------

def test_migration_008_creates_agent_runs(qwe_temp_data_dir):
    import db
    db._migrated = False  # force re-run
    conn = db._get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_runs'"
    ).fetchone()
    assert row is not None, "agent_runs table not created"
    cols = {c[1] for c in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    expected = {"id", "thread_id", "cron_id", "source", "scheduled_at",
                "started_at", "finished_at", "duration_ms", "status",
                "error", "result_preview", "model", "provider",
                "input_tokens", "output_tokens", "cost_usd"}
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_migration_008_drops_routine_runs(qwe_temp_data_dir):
    import db
    db._migrated = False
    conn = db._get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routine_runs'"
    ).fetchone()
    assert row is None, "routine_runs should be gone"


def test_migration_008_copies_legacy_routine_runs(qwe_temp_data_dir):
    import db
    import sqlite3
    import time
    import glob
    # Build a DB at schema_version=7 with routine_runs populated, then
    # let the migration runner fast-forward to 8.
    db_path = qwe_temp_data_dir / "qwe_qwe.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(open("migrations/001_initial.sql").read())
    for n in range(2, 8):
        path = f"migrations/00{n}_"
        f = glob.glob(path + "*.sql")[0]
        conn.executescript(open(f).read())
    conn.execute("INSERT OR REPLACE INTO kv (key,value,ts) VALUES ('schema_version','7',?)", (time.time(),))
    conn.execute("INSERT INTO routine_runs (cron_id, scheduled_at, started_at, status, thread_id) "
                 "VALUES (1, 1000.0, 1001.0, 'ok', 't1')")
    conn.commit()
    conn.close()
    db._migrated = False
    conn = db._get_conn()
    rows = conn.execute("SELECT cron_id, thread_id, status, source FROM agent_runs").fetchall()
    assert (1, 't1', 'ok', 'routine') in rows
