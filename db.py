"""SQLite storage — conversation history, settings, state."""

import gzip
import shutil
import sqlite3
import json
import re
import time
import threading
import uuid
from pathlib import Path
import config
import logger

_log = logger.get("db")

_local = threading.local()
_migrated = False
_migrate_lock = threading.Lock()

# --- DB protection: rolling backups, integrity check, graceful shutdown ------

MAX_BACKUPS = 24            # keep last 24 hourly backups = 1 day of coverage
BACKUP_INTERVAL_SEC = 3600  # how often the background thread fires

_backup_thread_started = False
_backup_thread_lock = threading.Lock()
_integrity_checked = False
_integrity_lock = threading.Lock()


def take_backup(tag: str = "") -> "Path | None":
    """Hot backup using SQLite's online backup API.

    Safe to call while the database is open and being written — SQLite
    serialises the page reads automatically. Creates a file named
    castor_<unix_ts>[_<tag>].db in ~/.castor/db_backups/, then prunes
    old backups so only MAX_BACKUPS files remain.
    """
    try:
        backup_dir = Path(config.DATA_DIR) / "db_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        suffix = f"_{tag}" if tag else ""
        backup_path = backup_dir / f"castor_{ts}{suffix}.db"
        src = sqlite3.connect(str(config.DB_PATH))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst, pages=-1)  # pages=-1 = copy everything atomically
        dst.close()
        src.close()
        _prune_backups(backup_dir)
        _log.info(f"db backup created: {backup_path.name}")
        return backup_path
    except Exception as e:
        _log.warning(f"db backup failed: {e}")
        return None


def _prune_backups(backup_dir: Path) -> None:
    """Keep only the MAX_BACKUPS most recent backup files."""
    try:
        backups = sorted(
            backup_dir.glob("castor_*.db"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in backups[:-MAX_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def latest_backup() -> "Path | None":
    """Return the most recent backup Path, or None if no backups exist."""
    try:
        backup_dir = Path(config.DATA_DIR) / "db_backups"
        backups = sorted(
            backup_dir.glob("castor_*.db"),
            key=lambda p: p.stat().st_mtime,
        )
        return backups[-1] if backups else None
    except Exception:
        return None


def check_and_restore() -> bool:
    """Integrity check on startup. Auto-restores from backup if malformed.

    Returns True if the DB is healthy (original or restored from backup).
    Returns False if malformed AND no backup was available — caller lets
    sqlite3.connect() create a fresh database.

    Safe to call from both server.py lifespan and _get_conn(). Sets
    _integrity_checked=True so that a direct call from lifespan prevents
    _get_conn() from running the check a second time.
    """
    global _integrity_checked
    db_path = Path(config.DB_PATH)
    if not db_path.exists():
        _integrity_checked = True
        return True  # fresh install — nothing to check

    try:
        probe = sqlite3.connect(str(db_path), check_same_thread=False, timeout=3)
        result = probe.execute("PRAGMA integrity_check(1)").fetchone()
        probe.close()
        if result and result[0] == "ok":
            _integrity_checked = True
            return True
    except sqlite3.DatabaseError as e:
        _log.error(f"database integrity check failed: {e}")
    except Exception as e:
        _log.warning(f"database probe error: {e}")
        _integrity_checked = True
        return True  # non-corruption error (permissions etc.) — don't wipe DB

    # Database is malformed — attempt restore from latest backup
    backup = latest_backup()
    if backup:
        _log.warning(f"corrupt database — restoring from backup: {backup.name}")
        try:
            ts = int(time.time())
            corrupted = db_path.with_name(f"castor.db.corrupted.{ts}")
            shutil.copy2(str(db_path), str(corrupted))
            shutil.copy2(str(backup), str(db_path))
            # Note: any existing castor.db-wal is intentionally left in place.
            # SQLite's WAL page checksums and salt validation mean stale/corrupt
            # frames are safely skipped on recovery. Valid frames after the
            # backup point will be replayed, giving MORE data than the backup alone.
            _log.info(
                f"database restored from {backup.name} "
                f"(corrupted copy saved as {corrupted.name})"
            )
            _integrity_checked = True
            return True
        except Exception as e:
            _log.error(f"restore from backup failed: {e}")

    # No backup — rename corrupted DB and let startup create a fresh one
    _log.error("database corrupted and no backup available — starting fresh")
    try:
        corrupted = db_path.with_name(f"castor.db.corrupted.{int(time.time())}")
        db_path.rename(corrupted)
        _log.warning(f"corrupted file saved as {corrupted.name}")
    except OSError:
        pass
    _integrity_checked = True
    return False


def graceful_shutdown() -> None:
    """Flush WAL and close the connection for this thread cleanly.

    Call on SIGTERM / SIGINT before the process exits. Without this,
    killing the process mid-write leaves WAL pages unflushed; the next
    startup has to recover them — and if the WAL is also partially
    written, recovery can fail and corrupt the database.
    """
    try:
        conn = getattr(_local, "conn", None)
        if conn:
            row = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            # row = (busy, log_frames, checkpointed_frames)
            if row and row[0]:  # busy > 0 means some frames couldn't be checkpointed
                _log.warning(
                    f"wal_checkpoint(FULL): busy={row[0]} log={row[1]} "
                    f"checkpointed={row[2]} — other connections may hold WAL open"
                )
            conn.close()
            _local.conn = None
            _log.info("database flushed and closed (graceful shutdown)")
    except Exception as e:
        _log.warning(f"graceful db shutdown error (non-fatal): {e}")


def start_backup_scheduler() -> None:
    """Start a daemon thread that takes a hot backup every BACKUP_INTERVAL_SEC.

    Also takes an immediate 'startup' backup so there's always at least one
    backup from the last clean start. Idempotent — safe to call multiple times.
    """
    global _backup_thread_started
    if _backup_thread_started:
        return
    with _backup_thread_lock:
        if _backup_thread_started:
            return
        _backup_thread_started = True

    take_backup("startup")

    def _loop() -> None:
        while True:
            time.sleep(BACKUP_INTERVAL_SEC)
            try:
                take_backup()
            except Exception as e:
                _log.warning(f"scheduled backup error: {e}")

    t = threading.Thread(target=_loop, name="db-backup", daemon=True)
    t.start()
    _log.info(
        f"db backup scheduler started "
        f"(interval={BACKUP_INTERVAL_SEC}s, max={MAX_BACKUPS} backups)"
    )


# --- Migration runner -------------------------------------------------------
# Schema changes live in ``migrations/NNN_name.sql``. See migrations/README.md
# for the full convention. A single kv key, ``schema_version``, tracks the
# highest migration number that has been applied.

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


def _get_conn() -> sqlite3.Connection:
    global _migrated, _integrity_checked
    # Run integrity check + auto-restore exactly once per process, before the
    # first sqlite3.connect() call. If the file is corrupted, check_and_restore()
    # renames it aside; sqlite3.connect() then creates a fresh DB.
    if not _integrity_checked:
        with _integrity_lock:
            if not _integrity_checked:
                check_and_restore()
                _integrity_checked = True
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, faster than FULL
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")  # clean up any leftover WAL on startup
        _local.conn = conn
        # Migrate once across all threads
        with _migrate_lock:
            if not _migrated:
                _apply_migrations(conn)
                _migrated = True
                _log.info(f"database connected: {config.DB_PATH}")
    return conn


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the currently-applied schema version (0 if never applied)."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        # kv table doesn't exist yet — brand new DB.
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _write_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, value, ts) VALUES ('schema_version', ?, ?)",
        (str(version), time.time()),
    )


def _has_baseline_tables(conn: sqlite3.Connection) -> bool:
    """Heuristic: does this DB already have the pre-migration baseline?

    If ``messages`` exists, assume 001_initial.sql is already effectively
    applied (the ad-hoc CREATE TABLE IF NOT EXISTS code that used to live
    in ``_migrate`` has run at some point in this DB's history).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    return row is not None


def _list_migrations() -> list[tuple[int, Path]]:
    """Return [(version, path)] sorted ascending by version."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    for p in MIGRATIONS_DIR.iterdir():
        if not p.is_file():
            continue
        m = _MIGRATION_RE.match(p.name)
        if not m:
            continue
        out.append((int(m.group(1)), p))
    out.sort(key=lambda x: x[0])
    return out


def _iter_sql_statements(sql: str):
    """Yield individual SQL statements from a migration script.

    Strips ``--`` line comments *before* splitting on ``;`` — comments that
    contain semicolons (e.g. ``-- doesn't rewrite rows; each …``) would
    otherwise create spurious statement fragments.  Bare ``BEGIN`` / ``COMMIT``
    / ``ROLLBACK`` tokens are skipped so callers can manage their own
    transaction.  Splitting on ``;`` is safe for our migration files — none of
    them embed semicolons inside string literals.
    """
    # Strip line comments first so in-comment semicolons don't mislead the splitter.
    clean_lines = [ln.split("--")[0] for ln in sql.splitlines()]
    clean_sql = "\n".join(clean_lines)
    for raw in clean_sql.split(";"):
        stmt = raw.strip()
        if not stmt:
            continue
        if stmt.upper() in ("BEGIN", "COMMIT", "ROLLBACK"):
            continue
        yield stmt


def _apply_one(conn: sqlite3.Connection, path: Path) -> None:
    """Run one migration file inside a single transaction.

    Executes statements one-by-one (instead of ``executescript``) so that
    ``ALTER TABLE ADD COLUMN`` statements that raise ``duplicate column name``
    can be silently skipped.  This makes migrations idempotent when
    ``scheduler._ensure_table()`` (or similar helpers) have already added a
    column before the migration ran — a common cause of test flakiness when
    the scheduler module is imported before the DB is fully migrated.
    """
    sql = path.read_text(encoding="utf-8")
    conn.execute("BEGIN")
    try:
        for stmt in _iter_sql_statements(sql):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    # Column already exists — migration is effectively already
                    # applied for this column; skip and continue.
                    _log.debug(
                        f"{path.name}: skipping already-present column ({exc})"
                    )
                    continue
                raise
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring the database up to the latest migration version.

    Behaviour:
    * Fresh DB → applies every migration in order, bumping schema_version.
    * Existing install with tables but no schema_version → stamp to 1
      without re-running 001_initial.sql, then apply 002+ normally.
    * Already up-to-date → no-op.
    """
    migs = _list_migrations()
    if not migs:
        return
    latest = migs[-1][0]

    current = _read_schema_version(conn)

    # Back-compat stamp: a pre-migration DB that already has the baseline
    # schema is treated as already at version 1.
    if current == 0 and _has_baseline_tables(conn):
        # Ensure kv exists before writing (baseline creates it, but be safe).
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS kv ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL);"
        )
        _write_schema_version(conn, 1)
        conn.commit()
        current = 1
        _log.info("stamped existing DB as schema_version=1 (backward-compat)")

    for version, path in migs:
        if version <= current:
            continue
        try:
            _apply_one(conn, path)
        except Exception as e:
            _log.error(f"migration {path.name} failed: {e}")
            raise
        _write_schema_version(conn, version)
        conn.commit()
        current = version
        _log.info(f"applied migration {path.name}")

    if current != latest:  # pragma: no cover — defensive
        _log.warning(f"schema_version={current} but latest migration is {latest}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Legacy entry point kept for backward compatibility with callers/tests
    that imported the old helper. Delegates to the migration runner."""
    _apply_migrations(conn)


# --- Public query helpers (use these instead of _get_conn() directly) ---

def execute(sql: str, params: tuple = ()) -> int:
    """Execute a write query (INSERT/UPDATE/DELETE) and commit. Returns rowcount."""
    conn = _get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def fetchall(sql: str, params: tuple = ()) -> list:
    """Execute a read query and return all rows."""
    return _get_conn().execute(sql, params).fetchall()


def fetchone(sql: str, params: tuple = ()):
    """Execute a read query and return one row (or None)."""
    return _get_conn().execute(sql, params).fetchone()


# --- Thread-aware helpers ---

def _tid(thread_id: str | None = None) -> str:
    """Resolve thread_id: explicit > active > default."""
    if thread_id:
        return thread_id
    # Lazy import to avoid circular dep at module load
    import threads
    return threads.get_active_id()


# --- Messages (all thread-scoped) ---

def save_message(role: str, content: str | None = None,
                 tool_calls: list | None = None,
                 tool_call_id: str | None = None,
                 name: str | None = None,
                 thread_id: str | None = None,
                 meta: dict | None = None):
    conn = _get_conn()
    tid = _tid(thread_id)
    conn.execute(
        "INSERT INTO messages (role, content, tool_calls, tool_call_id, name, ts, thread_id, meta) VALUES (?,?,?,?,?,?,?,?)",
        (role, content,
         json.dumps(tool_calls) if tool_calls else None,
         tool_call_id, name, time.time(), tid,
         json.dumps(meta) if meta else None)
    )
    conn.commit()


def get_recent_messages(limit: int = None, thread_id: str | None = None) -> list[dict]:
    if limit is None:
        limit = config.get("max_history_messages")
    conn = _get_conn()
    tid = _tid(thread_id)
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_call_id, name, meta FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?",
        (tid, limit)
    ).fetchall()
    messages = []
    for role, content, tc, tc_id, name, meta_json in reversed(rows):
        msg: dict = {"role": role}
        if content is not None:
            msg["content"] = content
        if tc:
            msg["tool_calls"] = json.loads(tc)
        if tc_id:
            msg["tool_call_id"] = tc_id
        if name:
            msg["name"] = name
        if meta_json:
            msg["meta"] = json.loads(meta_json)
        messages.append(msg)
    return messages


def clear_history(thread_id: str | None = None):
    conn = _get_conn()
    tid = _tid(thread_id)
    conn.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
    conn.commit()


def count_messages(thread_id: str | None = None) -> int:
    conn = _get_conn()
    tid = _tid(thread_id)
    row = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (tid,)).fetchone()
    return row[0]


def get_oldest_messages(limit: int, thread_id: str | None = None) -> list[dict]:
    """Get oldest messages for compaction."""
    conn = _get_conn()
    tid = _tid(thread_id)
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE thread_id=? ORDER BY id ASC LIMIT ?",
        (tid, limit)
    ).fetchall()
    return [{"id": r[0], "role": r[1], "content": r[2] or ""} for r in rows]


def delete_messages_by_ids(ids: list[int]):
    conn = _get_conn()
    conn.executemany("DELETE FROM messages WHERE id=?", [(i,) for i in ids])
    conn.commit()


# --- Key-Value ---

def kv_set(key: str, value: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
        (key, value, time.time())
    )
    conn.commit()


def kv_get(key: str) -> str | None:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def kv_delete(key: str) -> bool:
    """Delete a KV entry. Returns True if a row was actually removed."""
    return execute("DELETE FROM kv WHERE key = ?", (key,)) > 0


def kv_inc(key: str, delta: int = 1) -> int:
    """Atomically increment a counter. Creates if not exists.
    Uses RETURNING clause (SQLite 3.35+) for single-statement atomicity.
    """
    conn = _get_conn()
    now = time.time()
    try:
        row = conn.execute(
            "INSERT INTO kv (key, value, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT), ts = ? "
            "RETURNING CAST(value AS INTEGER)",
            (key, str(delta), now, delta, now)
        ).fetchone()
        conn.commit()
        return row[0] if row else delta
    except Exception:
        # Fallback for older SQLite without RETURNING
        conn.execute(
            "INSERT INTO kv (key, value, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT), ts = ?",
            (key, str(delta), now, delta, now)
        )
        conn.commit()
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row else delta


def kv_get_prefix(prefix: str) -> dict[str, str]:
    """Get all kv pairs where key starts with prefix."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT key, value FROM kv WHERE key LIKE ? || '%'", (prefix,)
    ).fetchall()
    return {k: v for k, v in rows}


# ---------------------------------------------------------------------------
# FTS5 BM25 helpers
# ---------------------------------------------------------------------------

def _fts_escape(query: str) -> str:
    """Escape FTS5 special characters by quoting each word."""
    words = query.split()
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words if w.strip())


_SAFE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_ident(name: str) -> bool:
    """Return True iff *name* is a safe SQL identifier (alpha/underscore start, alphanumeric/underscore body)."""
    return bool(_SAFE_IDENT_RE.match(name))


def fts_upsert(table: str, id_col: str, id_val: str, fields: dict):
    """Insert or replace a row in an FTS5 table.

    FTS5 has no UPSERT — delete old row first, then insert.
    ``fields`` maps column names to values (excluding id_col).
    """
    if table not in ("fts_rag", "fts_memory"):
        return
    # Validate identifier names to prevent injection via column names
    if not _safe_ident(id_col):
        _log.warning("fts_upsert: unsafe id_col rejected: %r", id_col)
        return
    for col in fields:
        if not _safe_ident(col):
            _log.warning("fts_upsert: unsafe column name rejected: %r", col)
            return
    try:
        conn = _get_conn()
        # Delete existing row with this id
        conn.execute(
            f"DELETE FROM {table} WHERE {id_col} = ?", (id_val,)  # noqa: S608 — table/id_col validated above
        )
        # Build INSERT
        cols = [id_col] + list(fields.keys())
        vals = [id_val] + list(fields.values())
        placeholders = ",".join("?" for _ in cols)
        col_names = ",".join(cols)
        conn.execute(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", vals)  # noqa: S608
        conn.commit()
    except Exception as e:
        _log.debug(f"fts_upsert({table}) failed: {e}")


def fts_search(table: str, query: str, limit: int = 20) -> list[dict]:
    """BM25 keyword search over an FTS5 table.

    Returns list of dicts with all columns + ``rank`` (BM25 score, negative = better).
    """
    if table not in ("fts_rag", "fts_memory"):
        return []
    escaped = _fts_escape(query)
    if not escaped or escaped == '""':
        return []
    try:
        conn = _get_conn()
        # Get column names (excluding internal rowid)
        # FTS5 tables: first call pragma to get columns
        cols_row = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = [r[1] for r in cols_row]

        rows = conn.execute(
            f"SELECT *, rank FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
            (escaped, limit)
        ).fetchall()

        results = []
        for row in rows:
            d = {}
            for i, col in enumerate(col_names):
                d[col] = row[i]
            d["rank"] = row[-1]  # BM25 rank (negative, lower = better match)
            results.append(d)
        return results
    except Exception as e:
        _log.debug(f"fts_search({table}) failed: {e}")
        return []


def fts_delete(table: str, id_col: str, id_val: str):
    """Delete rows from an FTS5 table by id column match."""
    if table not in ("fts_rag", "fts_memory"):
        return
    try:
        conn = _get_conn()
        conn.execute(f"DELETE FROM {table} WHERE {id_col} = ?", (id_val,))
        conn.commit()
    except Exception as e:
        _log.debug(f"fts_delete({table}) failed: {e}")


def fts_delete_match(table: str, col: str, value: str):
    """Delete rows from FTS5 where a column contains a value (exact match via =)."""
    if table not in ("fts_rag", "fts_memory"):
        return
    try:
        conn = _get_conn()
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (value,))
        conn.commit()
    except Exception as e:
        _log.debug(f"fts_delete_match({table}) failed: {e}")


def rrf_merge(ranked_lists: list[list[tuple[str, float]]],
              k: int = 60, limit: int = 10) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion across multiple ranked result lists.

    Each list: [(id, score), ...] sorted by relevance (best first).
    Returns merged [(id, combined_rrf_score)] sorted by combined score desc.

    RRF formula: score(d) = sum(1 / (k + rank_i(d))) for each list i.
    """
    scores: dict[str, float] = {}
    for rlist in ranked_lists:
        for rank, (item_id, _original_score) in enumerate(rlist):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return merged[:limit]


# --- Agent runs (cost tracking) -------------------------------------------
# See migrations/008_agent_runs.sql for the table shape.

def insert_agent_run(
    thread_id: str,
    source: str,
    started_at: float,
    status: str = "running",
    cron_id: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    scheduled_at: float | None = None,
    resumed_from_run_id: int | None = None,
) -> int:
    """Insert a new run row. Returns the new id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO agent_runs (thread_id, cron_id, source, scheduled_at, "
        " started_at, status, model, provider, resumed_from_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (thread_id, cron_id, source, scheduled_at, started_at, status, model, provider, resumed_from_run_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def finalize_agent_run(
    run_id: int,
    finished_at: float | None,
    duration_ms: int | None,
    status: str,
    error: str | None = None,
    result_preview: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = None,
) -> None:
    """Update a previously-inserted run with final metrics."""
    conn = _get_conn()
    conn.execute(
        "UPDATE agent_runs SET finished_at=?, duration_ms=?, status=?, "
        " error=?, result_preview=?, input_tokens=?, output_tokens=?, cost_usd=? "
        "WHERE id=?",
        (finished_at, duration_ms, status, error,
         (result_preview or "")[:200] or None,
         int(input_tokens or 0), int(output_tokens or 0), cost_usd, run_id),
    )
    conn.commit()


def insert_skipped_run(
    cron_id: int, thread_id: str, scheduled_at: float, reason: str = "missed"
) -> int:
    """For routine fires that never executed (missed / skipped / budget_exceeded).

    ``reason`` is stored in the ``error`` column, not ``status``.  Valid status
    values are the enum defined in migration 008: running / ok / err / aborted /
    missed / skipped.  ``reason`` maps to the closest status:
    - "missed"            → status="missed"
    - "skipped"           → status="skipped"
    - "budget_exceeded"   → status="skipped", error="budget_exceeded"
    Anything unrecognised defaults to status="skipped".
    """
    _STATUS_MAP = {
        "missed": "missed",
        "skipped": "skipped",
        "budget_exceeded": "skipped",
    }
    status = _STATUS_MAP.get(reason, "skipped")
    error = reason if status != reason else None  # store in error only when it differs
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO agent_runs (thread_id, cron_id, source, scheduled_at, "
        " started_at, status, error, input_tokens, output_tokens, cost_usd) "
        "VALUES (?,?,?,?,?,?,?,0,0,0.0)",
        (thread_id or "", cron_id, "routine", scheduled_at, scheduled_at,
         status, error),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_runs_for_thread(thread_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Per-thread run history, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, thread_id, cron_id, source, scheduled_at, started_at, "
        "       finished_at, duration_ms, status, error, result_preview, "
        "       model, provider, input_tokens, output_tokens, cost_usd "
        "FROM agent_runs WHERE thread_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (thread_id, int(limit), int(offset)),
    ).fetchall()
    cols = ("id", "thread_id", "cron_id", "source", "scheduled_at",
            "started_at", "finished_at", "duration_ms", "status", "error",
            "result_preview", "model", "provider",
            "input_tokens", "output_tokens", "cost_usd")
    return [dict(zip(cols, r, strict=True)) for r in rows]


def get_thread_totals(thread_id: str) -> dict:
    """Returns {input_tokens, output_tokens, cost_usd, run_count}.

    cost_usd is None (not 0.0) when all runs have NULL cost — callers can
    distinguish "genuinely zero cost" from "pricing data unavailable".
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        "       SUM(cost_usd), COUNT(*) "
        "FROM agent_runs WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    run_count = int(row[3])
    # cost_usd=None means "runs exist but we couldn't price them" (NULL from
    # unknown-model pricing).  cost_usd=0.0 means "no runs" (definitively zero).
    cost_usd = float(row[2]) if row[2] is not None else (0.0 if run_count == 0 else None)
    return {
        "input_tokens": int(row[0]),
        "output_tokens": int(row[1]),
        "cost_usd": cost_usd,
        "run_count": run_count,
    }


def get_period_totals(
    start_ts: float, end_ts: float, source: str | None = None
) -> dict:
    """Aggregated metrics for a time window. by_source breakdown included."""
    conn = _get_conn()
    params: list = [start_ts, end_ts]
    src_clause = ""
    if source:
        src_clause = " AND source=?"
        params.append(source)
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        "       SUM(cost_usd), COUNT(*) "
        f"FROM agent_runs WHERE started_at>=? AND started_at<?{src_clause}",
        params,
    ).fetchone()
    by_src_rows = conn.execute(
        "SELECT source, COALESCE(SUM(input_tokens),0), "
        "       COALESCE(SUM(output_tokens),0), SUM(cost_usd), "
        f"      COUNT(*) FROM agent_runs WHERE started_at>=? AND started_at<?{src_clause} "
        "GROUP BY source",
        params,  # reuse same params (already includes source filter if set)
    ).fetchall()
    by_source = {
        r[0]: {
            "input_tokens": int(r[1]),
            "output_tokens": int(r[2]),
            "cost_usd": float(r[3]) if r[3] is not None else None,
            "run_count": int(r[4]),
        }
        for r in by_src_rows
    }
    return {
        "start_ts": float(start_ts),
        "end_ts": float(end_ts),
        "total_input_tokens": int(row[0]),
        "total_output_tokens": int(row[1]),
        "total_cost_usd": float(row[2]) if row[2] is not None else None,
        "run_count": int(row[3]),
        "by_source": by_source,
    }


def get_runs_for_routine(cron_id: int, limit: int = 50) -> list[dict]:
    """Per-routine run history (replaces routine_runs query)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, thread_id, scheduled_at, started_at, finished_at, "
        "       duration_ms, status, error, result_preview, "
        "       input_tokens, output_tokens, cost_usd, model, provider "
        "FROM agent_runs WHERE cron_id=? ORDER BY id DESC LIMIT ?",
        (int(cron_id), int(limit)),
    ).fetchall()
    cols = ("id", "thread_id", "scheduled_at", "started_at", "finished_at",
            "duration_ms", "status", "error", "result_preview",
            "input_tokens", "output_tokens", "cost_usd", "model", "provider")
    return [dict(zip(cols, r, strict=True)) for r in rows]


def dismiss_run(run_id: int) -> None:
    """Mark a run dismissed (idempotent — does not overwrite existing dismissed_at)."""
    conn = _get_conn()
    conn.execute(
        "UPDATE agent_runs SET dismissed_at = ? "
        "WHERE id = ? AND dismissed_at IS NULL",
        (time.time(), int(run_id)),
    )
    conn.commit()


def get_resumable_run_for_thread(
    thread_id: str,
    source_filter: str | None = None,
    ttl_sec: float = 604800,
) -> dict | None:
    """Return the most recent aborted run on this thread that's still
    eligible for resume, or None.

    Filters out:
      - non-aborted statuses
      - dismissed runs
      - runs older than ttl_sec
      - runs that are themselves resume runs (resumed_from_run_id NOT NULL)
      - runs that have already been resumed-from (referenced by some later row)
      - source='cli' (Ctrl+C is intentional stop)
      - optionally: anything not matching source_filter
    """
    cutoff = time.time() - float(ttl_sec)
    params: list = [thread_id, cutoff]
    src_clause = ""
    if source_filter is not None:
        src_clause = " AND source = ?"
        params.append(source_filter)

    # Two related-but-different guards (do not collapse):
    #   resumed_from_run_id IS NULL  → row is not itself a resume run
    #   id NOT IN (...)              → no later row already resumed from it
    sql = (
        "SELECT id, started_at, result_preview, model, source, cron_id "
        "FROM agent_runs "
        "WHERE thread_id = ? AND status = 'aborted' "
        "  AND dismissed_at IS NULL "
        "  AND started_at >= ? "
        "  AND resumed_from_run_id IS NULL "
        "  AND source != 'cli' "
        "  AND id NOT IN (SELECT resumed_from_run_id FROM agent_runs "
        "                 WHERE resumed_from_run_id IS NOT NULL) "
        f"{src_clause} "
        "ORDER BY id DESC LIMIT 1"
    )
    conn = _get_conn()
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "started_at": row[1],
        "result_preview": row[2],
        "model": row[3],
        "source": row[4],
        "cron_id": row[5],
    }


def get_routine_period_spend(cron_id: int, period_sec: float) -> float:
    """Sum agent_runs.cost_usd for this cron_id over the last period_sec.

    NULL cost_usd values (pricing unknown) are treated as 0 — they don't
    contribute to budget burn. This is intentional: a user running a local
    LLM (cost=0) should never hit a cap; an unknown-price model should not
    block fires either, but should be obvious in analytics.
    """
    import time as _t
    cutoff = _t.time() - float(period_sec)
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_runs "
        "WHERE cron_id = ? AND started_at >= ?",
        (int(cron_id), cutoff),
    ).fetchone()
    return float(row[0] or 0.0)


def get_routine_budget(cron_id: int) -> dict | None:
    """Return {"cap": float, "period_sec": int} or None if no cap set."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT budget_usd_cap, budget_period_sec FROM scheduled_tasks WHERE id=?",
        (int(cron_id),),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return {"cap": float(row[0]), "period_sec": int(row[1] or 86400)}


# ─────────────────────────────────────────────────────────────────────────────
#  Goal runtime (Phase 1 of long-running agent architecture)
#
#  Three tables:
#    goals             — durable queue + status machine
#    goal_checkpoints  — orchestrator state snapshots (resume after crash)
#    goal_events       — append-only event log for observability
#
#  Lease protocol: when a worker claims a goal it sets worker_id +
#  lease_expires_at. The worker MUST heartbeat (refresh lease_expires_at)
#  more often than LEASE_DURATION_SEC, or another worker is allowed to
#  take over. claim_next_goal does this in a single atomic SQL update so
#  two workers never grab the same goal.
# ─────────────────────────────────────────────────────────────────────────────


# Terminal statuses — no further work happens after the goal reaches one.
GOAL_TERMINAL_STATUSES = ("done", "failed", "aborted")
# Statuses that can be resumed by a worker.
GOAL_RESUMABLE_STATUSES = ("pending", "running", "paused")


def create_goal(
    *,
    user_input: str,
    source: str,
    thread_id: str | None = None,
    budget_usd: float | None = None,
    budget_seconds: int | None = None,
    meta: dict | None = None,
) -> str:
    """Enqueue a new goal. Returns the new goal_id."""
    goal_id = "g_" + uuid.uuid4().hex[:16]
    conn = _get_conn()
    conn.execute(
        """INSERT INTO goals (id, thread_id, source, user_input, status,
                              budget_usd, budget_seconds, created_at, meta)
           VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (
            goal_id,
            thread_id,
            source,
            user_input,
            budget_usd,
            budget_seconds,
            time.time(),
            json.dumps(meta or {}),
        ),
    )
    conn.commit()
    log_goal_event(goal_id, "goal_created",
                   {"source": source, "thread_id": thread_id})
    return goal_id


def get_goal(goal_id: str) -> dict | None:
    """Fetch a goal row by id. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT id, thread_id, source, user_input, status, plan, result, error,
                  budget_usd, budget_seconds, cost_usd, started_at, finished_at,
                  created_at, worker_id, lease_expires_at, meta
           FROM goals WHERE id=?""",
        (goal_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "thread_id": row[1],
        "source": row[2],
        "user_input": row[3],
        "status": row[4],
        "plan": json.loads(row[5]) if row[5] else None,
        "result": row[6],
        "error": row[7],
        "budget_usd": row[8],
        "budget_seconds": row[9],
        "cost_usd": float(row[10] or 0.0),
        "started_at": row[11],
        "finished_at": row[12],
        "created_at": row[13],
        "worker_id": row[14],
        "lease_expires_at": row[15],
        "meta": json.loads(row[16]) if row[16] else {},
    }


def list_goals(
    *,
    status: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List goals filtered by optional status / thread_id, newest first."""
    conn = _get_conn()
    where = []
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    if thread_id:
        where.append("thread_id=?")
        params.append(thread_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    rows = conn.execute(
        f"""SELECT id, thread_id, source, user_input, status, started_at,
                   finished_at, created_at, cost_usd
            FROM goals {where_sql} ORDER BY created_at DESC LIMIT ?""",
        params,
    ).fetchall()
    return [
        {
            "id": r[0],
            "thread_id": r[1],
            "source": r[2],
            "user_input": r[3],
            "status": r[4],
            "started_at": r[5],
            "finished_at": r[6],
            "created_at": r[7],
            "cost_usd": float(r[8] or 0.0),
        }
        for r in rows
    ]


def claim_next_goal(worker_id: str, lease_sec: int) -> str | None:
    """Atomically claim the next runnable goal. Returns goal_id or None.

    A goal is claimable if it's:
      - status='pending', OR
      - status='running' or 'paused' with expired/missing lease (worker died)

    Uses ``UPDATE ... WHERE id IN (SELECT ... LIMIT 1)`` to keep the read and
    the claim atomic — SQLite serialises the write, so two workers calling
    this concurrently can never grab the same row.
    """
    conn = _get_conn()
    now = time.time()
    new_lease = now + lease_sec
    # Two-step but inside a single immediate transaction so concurrent claims
    # serialise: first SELECT the candidate, then UPDATE it by id checking
    # status hasn't changed. The "BEGIN IMMEDIATE" upgrade is implicit on
    # the first write under WAL mode but we force it via savepoint to make
    # the intent obvious.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        # Another writer holds the lock; treat as "no goal this poll".
        return None
    try:
        row = conn.execute(
            """SELECT id, status, started_at FROM goals
               WHERE status='pending'
                  OR (status IN ('running','paused')
                      AND (lease_expires_at IS NULL OR lease_expires_at < ?))
               ORDER BY created_at LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        goal_id, prev_status, prev_started = row
        # Take the lease.
        conn.execute(
            """UPDATE goals
               SET status='running',
                   worker_id=?,
                   lease_expires_at=?,
                   started_at=COALESCE(started_at, ?)
               WHERE id=?""",
            (worker_id, new_lease, now, goal_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Log takeover separately (best-effort, doesn't roll back the claim).
    if prev_status in ("running", "paused") and prev_started is not None:
        log_goal_event(goal_id, "worker_lost",
                       {"new_worker_id": worker_id, "prev_status": prev_status})
        log_goal_event(goal_id, "resumed",
                       {"reason": "previous_worker_lease_expired"})
    return goal_id


def heartbeat_goal(goal_id: str, worker_id: str, lease_sec: int) -> bool:
    """Refresh the lease. Returns True if the lease is still ours.

    If a different worker has taken over (because we hung past the lease),
    returns False — caller should stop work and abandon the goal.
    """
    conn = _get_conn()
    cur = conn.execute(
        """UPDATE goals SET lease_expires_at=?
           WHERE id=? AND worker_id=?""",
        (time.time() + lease_sec, goal_id, worker_id),
    )
    conn.commit()
    return cur.rowcount > 0


def release_worker_leases(worker_id: str) -> int:
    """On worker startup, release any leases this worker_id held in a previous
    life. Returns the number of goals released.

    A "released" goal goes back to status='paused' so the worker picks it up
    again via the normal claim path (and the takeover events get logged).
    Without this, a worker that crashed without cleanup would block its own
    goals from being re-claimed until lease expiration.
    """
    conn = _get_conn()
    cur = conn.execute(
        """UPDATE goals SET status='paused', worker_id=NULL, lease_expires_at=NULL
           WHERE worker_id=? AND status='running'""",
        (worker_id,),
    )
    conn.commit()
    return cur.rowcount


def mark_goal_done(goal_id: str, *, result: str) -> None:
    conn = _get_conn()
    conn.execute(
        """UPDATE goals SET status='done', result=?, finished_at=?,
                            worker_id=NULL, lease_expires_at=NULL
           WHERE id=?""",
        (result, time.time(), goal_id),
    )
    conn.commit()
    log_goal_event(goal_id, "goal_completed", {"reply_len": len(result or "")})


def mark_goal_failed(goal_id: str, *, error: str) -> None:
    conn = _get_conn()
    conn.execute(
        """UPDATE goals SET status='failed', error=?, finished_at=?,
                            worker_id=NULL, lease_expires_at=NULL
           WHERE id=?""",
        (error[:2000], time.time(), goal_id),
    )
    conn.commit()
    log_goal_event(goal_id, "error", {"error": error[:500]})


def mark_goal_paused(goal_id: str, *, reason: str) -> None:
    """Pause a goal. Worker releases the lease so any worker can resume."""
    conn = _get_conn()
    conn.execute(
        """UPDATE goals SET status='paused',
                            worker_id=NULL, lease_expires_at=NULL
           WHERE id=?""",
        (goal_id,),
    )
    conn.commit()
    log_goal_event(goal_id, "paused", {"reason": reason})


def mark_goal_aborted(goal_id: str, *, reason: str = "user_aborted") -> None:
    """Terminal abort — different from paused: won't auto-resume."""
    conn = _get_conn()
    conn.execute(
        """UPDATE goals SET status='aborted', finished_at=?,
                            worker_id=NULL, lease_expires_at=NULL
           WHERE id=?""",
        (time.time(), goal_id),
    )
    conn.commit()
    log_goal_event(goal_id, "aborted", {"reason": reason})


# ── Checkpoints ──────────────────────────────────────────────────────────────

# Keep this many checkpoints per goal; older ones are pruned on each save.
CHECKPOINT_RETENTION = 5

# Hard cap on serialised messages — protects against runaway compaction failures.
MAX_CHECKPOINT_BLOB_BYTES = 4 * 1024 * 1024  # 4 MB compressed


def save_checkpoint(
    goal_id: str,
    round_num: int,
    *,
    subtask_index: int = -1,
    messages: list[dict],
    plan: dict | None = None,
    facts: dict | None = None,
) -> None:
    """Persist orchestrator state. Keeps only the latest CHECKPOINT_RETENTION."""
    blob = gzip.compress(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    if len(blob) > MAX_CHECKPOINT_BLOB_BYTES:
        # Truncate oldest non-system messages to fit; never drop the system prompt.
        _log.warning(
            f"checkpoint blob > {MAX_CHECKPOINT_BLOB_BYTES} bytes for goal {goal_id} "
            f"round {round_num}; truncating to fit"
        )
        messages = _truncate_messages_to_fit(messages, MAX_CHECKPOINT_BLOB_BYTES)
        blob = gzip.compress(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO goal_checkpoints
           (goal_id, round_num, subtask_index, messages_blob, plan_snapshot,
            facts_snapshot, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            goal_id,
            int(round_num),
            int(subtask_index),
            blob,
            json.dumps(plan or {}),
            json.dumps(facts or {}),
            time.time(),
        ),
    )
    # Prune: keep only the latest N checkpoints per goal.
    conn.execute(
        """DELETE FROM goal_checkpoints
           WHERE goal_id=? AND id NOT IN (
               SELECT id FROM goal_checkpoints
               WHERE goal_id=? ORDER BY round_num DESC LIMIT ?
           )""",
        (goal_id, goal_id, CHECKPOINT_RETENTION),
    )
    conn.commit()


def load_latest_checkpoint(goal_id: str) -> dict | None:
    """Return latest snapshot or None if there are no checkpoints for this goal."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT round_num, subtask_index, messages_blob, plan_snapshot,
                  facts_snapshot, timestamp
           FROM goal_checkpoints WHERE goal_id=?
           ORDER BY round_num DESC LIMIT 1""",
        (goal_id,),
    ).fetchone()
    if not row:
        return None
    try:
        messages = json.loads(gzip.decompress(row[2]).decode("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.error(f"checkpoint blob corrupt for goal {goal_id}: {e}")
        return None
    return {
        "round_num": int(row[0]),
        "subtask_index": int(row[1]),
        "messages": messages,
        "plan": json.loads(row[3]) if row[3] else {},
        "facts": json.loads(row[4]) if row[4] else {},
        "timestamp": float(row[5]),
    }


def _truncate_messages_to_fit(messages: list[dict], byte_limit: int) -> list[dict]:
    """Drop the oldest non-system messages until the gzipped JSON fits."""
    # Always keep system prompt (index 0 if present).
    head = []
    tail = list(messages)
    if tail and tail[0].get("role") == "system":
        head = [tail.pop(0)]
    while tail:
        candidate = head + tail
        size = len(gzip.compress(json.dumps(candidate, ensure_ascii=False).encode("utf-8")))
        if size <= byte_limit:
            return candidate
        tail.pop(0)
    return head or messages[:1]


# ── Event log ────────────────────────────────────────────────────────────────


def log_goal_event(goal_id: str, event_type: str, payload: dict | None = None) -> None:
    """Append a row to goal_events. Never raises — telemetry shouldn't break work."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO goal_events (goal_id, timestamp, event_type, payload) "
            "VALUES (?, ?, ?, ?)",
            (goal_id, time.time(), event_type, json.dumps(payload or {})),
        )
        conn.commit()
    except Exception:
        _log.exception(f"failed to log goal event {event_type} for {goal_id}")


def get_goal_events(goal_id: str, limit: int = 200) -> list[dict]:
    """Return events for a goal, oldest first (for live tailing in UI)."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT timestamp, event_type, payload FROM goal_events
           WHERE goal_id=? ORDER BY id LIMIT ?""",
        (goal_id, int(limit)),
    ).fetchall()
    return [
        {
            "timestamp": float(r[0]),
            "event_type": r[1],
            "payload": json.loads(r[2]) if r[2] else {},
        }
        for r in rows
    ]


# ── Plan helpers (Phase 2) ───────────────────────────────────────────────────
#
# A goal's plan is JSON in goals.plan with shape:
#   {
#     "version": 1,
#     "subtasks": [{"id": "st_1", "title": "...", "description": "...",
#                   "status": "pending"|"in_progress"|"completed"|"skipped"|"failed",
#                   "started_at": float|null, "finished_at": float|null,
#                   "result_summary": str|null, "dispatched_subagent": str|null,
#                   "attempts": int}],
#     "current_index": int,
#     "created_at": float, "updated_at": float
#   }


_VALID_SUBTASK_STATUSES = {"pending", "in_progress", "completed", "skipped", "failed"}


def set_goal_plan(goal_id: str, subtasks: list[dict]) -> dict:
    """Set or replace a goal's plan. Each input subtask is {title, description}.

    Generates stable IDs `st_1`..`st_N` so the orchestrator can refer to
    them by id across rounds. Replaces any existing plan (the orchestrator
    is responsible for not losing track of in-flight work — typically
    only called once at the start).

    Returns the saved plan dict.
    """
    now = time.time()
    plan = {
        "version": 1,
        "subtasks": [
            {
                "id": f"st_{i+1}",
                "title": (st.get("title") or "").strip(),
                "description": (st.get("description") or "").strip(),
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "result_summary": None,
                "dispatched_subagent": None,
                "attempts": 0,
            }
            for i, st in enumerate(subtasks)
        ],
        "current_index": 0,
        "created_at": now,
        "updated_at": now,
    }
    conn = _get_conn()
    conn.execute(
        "UPDATE goals SET plan=? WHERE id=?",
        (json.dumps(plan), goal_id),
    )
    conn.commit()
    log_goal_event(goal_id, "plan_set", {"subtasks": len(plan["subtasks"])})
    return plan


def update_subtask(
    goal_id: str,
    subtask_id: str,
    *,
    status: str | None = None,
    result_summary: str | None = None,
    dispatched_subagent: str | None = None,
    bump_attempts: bool = False,
) -> dict | None:
    """Patch one subtask in the goal's plan. Returns the new plan or None.

    Only non-None fields are written. ``status`` must be one of
    _VALID_SUBTASK_STATUSES. ``bump_attempts`` increments the attempts
    counter (useful when an LLM retries a failed subagent).
    """
    if status and status not in _VALID_SUBTASK_STATUSES:
        raise ValueError(f"invalid status {status!r}; want one of {_VALID_SUBTASK_STATUSES}")
    conn = _get_conn()
    row = conn.execute("SELECT plan FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row or not row[0]:
        return None
    plan = json.loads(row[0])
    now = time.time()
    found = False
    for st in plan.get("subtasks", []):
        if st["id"] != subtask_id:
            continue
        found = True
        if status:
            prev_status = st.get("status")
            st["status"] = status
            if status == "in_progress" and not st.get("started_at"):
                st["started_at"] = now
            if status in ("completed", "failed", "skipped") and not st.get("finished_at"):
                st["finished_at"] = now
            if status == "in_progress" and prev_status != "in_progress":
                # Roll current_index forward to this subtask so the
                # orchestrator UI/observability tracks the "active" subtask.
                plan["current_index"] = plan["subtasks"].index(st)
        if result_summary is not None:
            st["result_summary"] = result_summary[:4000]
        if dispatched_subagent is not None:
            st["dispatched_subagent"] = dispatched_subagent
        if bump_attempts:
            st["attempts"] = int(st.get("attempts") or 0) + 1
        break
    if not found:
        return None
    plan["updated_at"] = now
    conn.execute("UPDATE goals SET plan=? WHERE id=?", (json.dumps(plan), goal_id))
    conn.commit()
    if status:
        log_goal_event(goal_id, f"subtask_{status}",
                       {"subtask_id": subtask_id,
                        "result_preview": (result_summary or "")[:200]})
    return plan


def get_goal_plan(goal_id: str) -> dict | None:
    """Fetch only the plan column (cheaper than get_goal when that's all we need)."""
    conn = _get_conn()
    row = conn.execute("SELECT plan FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def goal_plan_is_complete(goal_id: str) -> bool:
    """True if every subtask reached a terminal status (completed/failed/skipped)."""
    plan = get_goal_plan(goal_id)
    if not plan or not plan.get("subtasks"):
        return False
    return all(
        st.get("status") in ("completed", "failed", "skipped")
        for st in plan["subtasks"]
    )


# ── Goal facts (Phase 2) ─────────────────────────────────────────────────────


def fact_save(goal_id: str, key: str, value: str,
              source_subtask_id: str | None = None) -> None:
    """Upsert a structured fact for this goal. Keys are unique per goal."""
    now = time.time()
    conn = _get_conn()
    # snake_case-ish keys to keep the orchestrator's prompt tidy. Reject
    # whitespace + newlines defensively; if the LLM passes garbage we'd
    # rather error than store unreadable rows.
    if not key or not key.strip() or "\n" in key:
        raise ValueError(f"invalid fact key: {key!r}")
    conn.execute(
        """INSERT INTO goal_facts (goal_id, key, value, source_subtask_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(goal_id, key) DO UPDATE SET
               value=excluded.value,
               source_subtask_id=excluded.source_subtask_id,
               updated_at=excluded.updated_at""",
        (goal_id, key.strip(), value, source_subtask_id, now, now),
    )
    conn.commit()


def fact_get(goal_id: str, keys: list[str] | None = None) -> dict[str, str]:
    """Return {key: value} for the requested keys, or all keys if None."""
    conn = _get_conn()
    if keys:
        # Build a parameterised IN list — avoid string concat for SQL safety.
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT key, value FROM goal_facts WHERE goal_id=? AND key IN ({placeholders})",
            (goal_id, *keys),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT key, value FROM goal_facts WHERE goal_id=? ORDER BY key",
            (goal_id,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def fact_list_keys(goal_id: str) -> list[str]:
    """Just the keys, sorted. Cheap pre-flight before fact_get."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT key FROM goal_facts WHERE goal_id=? ORDER BY key",
        (goal_id,),
    ).fetchall()
    return [r[0] for r in rows]


def fact_delete(goal_id: str, key: str) -> bool:
    """Delete one fact. Returns True if a row was removed."""
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM goal_facts WHERE goal_id=? AND key=?",
        (goal_id, key),
    )
    conn.commit()
    return cur.rowcount > 0


# ── Goal outputs (Phase 2 follow-up) ─────────────────────────────────────────
#
# Structured deliverables the orchestrator produces during a goal: files
# written to the workspace, URLs to open, standalone markdown reports.
# Stored separately from goals.result (free-form prose) so the UI can
# render Download / Open / Save-to-memory buttons without parsing.


GOAL_OUTPUT_KINDS = ("file", "link", "report")


def attach_goal_output(
    goal_id: str,
    *,
    kind: str,
    title: str,
    value: str,
    meta: dict | None = None,
) -> int:
    """Register a deliverable on the goal. Returns the new output's row id.

    Validation is light here (kind whitelist, non-empty title/value); the
    orchestrator-side tool wrapper does the heavier per-kind checks
    (path-traversal guard for files, scheme check for links).
    """
    if kind not in GOAL_OUTPUT_KINDS:
        raise ValueError(f"invalid output kind {kind!r}; want one of {GOAL_OUTPUT_KINDS}")
    if not title or not title.strip():
        raise ValueError("output title cannot be empty")
    if not value or not value.strip():
        raise ValueError("output value cannot be empty")
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO goal_outputs (goal_id, kind, title, value, meta, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (goal_id, kind, title.strip(), value, json.dumps(meta or {}), time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_goal_outputs(goal_id: str) -> list[dict]:
    """List outputs for a goal, oldest first."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, kind, title, value, meta, created_at
           FROM goal_outputs WHERE goal_id=? ORDER BY id""",
        (goal_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "kind": r[1],
            "title": r[2],
            "value": r[3],
            "meta": json.loads(r[4]) if r[4] else {},
            "created_at": float(r[5]),
        }
        for r in rows
    ]


def get_goal_output(goal_id: str, output_id: int) -> dict | None:
    """Fetch one output. Returns dict or None if not found."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT id, kind, title, value, meta, created_at
           FROM goal_outputs WHERE goal_id=? AND id=?""",
        (goal_id, int(output_id)),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "kind": row[1],
        "title": row[2],
        "value": row[3],
        "meta": json.loads(row[4]) if row[4] else {},
        "created_at": float(row[5]),
    }


def delete_goal_output(goal_id: str, output_id: int) -> bool:
    """Delete one output. Returns True if a row was removed."""
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM goal_outputs WHERE goal_id=? AND id=?",
        (goal_id, int(output_id)),
    )
    conn.commit()
    return cur.rowcount > 0
