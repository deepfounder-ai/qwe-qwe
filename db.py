"""SQLite storage — conversation history, settings, state."""

import sqlite3, json, time
from pathlib import Path
import config

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _migrate(_conn)
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


# --- Messages ---

def save_message(role: str, content: str | None = None,
                 tool_calls: list | None = None,
                 tool_call_id: str | None = None,
                 name: str | None = None):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO messages (role, content, tool_calls, tool_call_id, name, ts) VALUES (?,?,?,?,?,?)",
        (role, content,
         json.dumps(tool_calls) if tool_calls else None,
         tool_call_id, name, time.time())
    )
    conn.commit()


def get_recent_messages(limit: int = config.MAX_HISTORY_MESSAGES) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_call_id, name FROM messages ORDER BY id DESC LIMIT ?",
        (limit,)
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


def clear_history():
    conn = _get_conn()
    conn.execute("DELETE FROM messages")
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
