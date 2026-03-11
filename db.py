"""SQLite storage — conversation history, settings, state."""

import sqlite3, json, time
from pathlib import Path
import config
import logger

_log = logger.get("db")

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _migrate(_conn)
        _log.info(f"database connected: {config.DB_PATH}")
    return _conn


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
    """)
    conn.commit()


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
                 thread_id: str | None = None):
    conn = _get_conn()
    tid = _tid(thread_id)
    conn.execute(
        "INSERT INTO messages (role, content, tool_calls, tool_call_id, name, ts, thread_id) VALUES (?,?,?,?,?,?,?)",
        (role, content,
         json.dumps(tool_calls) if tool_calls else None,
         tool_call_id, name, time.time(), tid)
    )
    conn.commit()


def get_recent_messages(limit: int = config.MAX_HISTORY_MESSAGES, thread_id: str | None = None) -> list[dict]:
    conn = _get_conn()
    tid = _tid(thread_id)
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_call_id, name FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?",
        (tid, limit)
    ).fetchall()
    messages = []
    for role, content, tc, tc_id, name in reversed(rows):
        msg: dict = {"role": role}
        if content is not None:
            msg["content"] = content
        if tc:
            msg["tool_calls"] = json.loads(tc)
        if tc_id:
            msg["tool_call_id"] = tc_id
        if name:
            msg["name"] = name
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
