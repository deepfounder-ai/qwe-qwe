"""SQLite storage — conversation history, settings, state."""

import shutil
import sqlite3
import json
import re
import time
import threading
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
