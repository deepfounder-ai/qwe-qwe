"""SQLite storage — conversation history, settings, state."""

import sqlite3, json, time, threading
from pathlib import Path
import config
import logger

_log = logger.get("db")

_local = threading.local()
_migrated = False
_migrate_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _migrated
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
        # Migrate once across all threads
        with _migrate_lock:
            if not _migrated:
                _migrate(conn)
                _migrated = True
                _log.info(f"database connected: {config.DB_PATH}")
    return conn


def _migrate(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,       -- JSON array of tool calls (assistant)
            tool_call_id TEXT,     -- for tool results
            name TEXT,             -- tool name (for tool results)
            ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
        CREATE TABLE IF NOT EXISTS presets (
            id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            author_name TEXT,
            license_type TEXT,
            manifest_json TEXT NOT NULL,
            installed_at REAL NOT NULL,
            source_path TEXT
        );
    """)
    conn.commit()

    # FTS5 tables for BM25 keyword search (hybrid with Qdrant vector search)
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_rag USING fts5(
                chunk_id, file_path, text,
                tokenize='porter unicode61'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_memory USING fts5(
                point_id, tag, text,
                tokenize='porter unicode61'
            );
        """)
        conn.commit()
    except Exception as e:
        _log.warning(f"FTS5 tables not created (may not be supported): {e}")

    # Add meta column (JSON) for assistant message metadata: tools, duration, etc.
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


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


def fts_upsert(table: str, id_col: str, id_val: str, fields: dict):
    """Insert or replace a row in an FTS5 table.

    FTS5 has no UPSERT — delete old row first, then insert.
    ``fields`` maps column names to values (excluding id_col).
    """
    if table not in ("fts_rag", "fts_memory"):
        return
    try:
        conn = _get_conn()
        # Delete existing row with this id
        conn.execute(
            f"DELETE FROM {table} WHERE {id_col} = ?", (id_val,)
        )
        # Build INSERT
        cols = [id_col] + list(fields.keys())
        vals = [id_val] + list(fields.values())
        placeholders = ",".join("?" for _ in cols)
        col_names = ",".join(cols)
        conn.execute(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", vals)
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
