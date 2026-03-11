"""Thread/session management — isolated conversations with shared memory.

Each thread has its own message history. Memory (Qdrant) and soul are shared.

Usage:
    import threads
    t = threads.create("research project")
    threads.switch(t["id"])
    current = threads.get_active_id()   # used by db and agent
    threads.list_all()
"""

import time
import json
import db
import logger

_log = logger.get("threads")

# ── Active thread (in-process state) ──
_active_id: str | None = None

DEFAULT_THREAD_ID = "default"
DEFAULT_THREAD_NAME = "Main"


def _ensure_table():
    """Create threads table if not exists + migrate messages."""
    conn = db._get_conn()

    # Create threads table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            archived INTEGER DEFAULT 0,
            meta TEXT DEFAULT '{}'
        );
    """)

    # Add thread_id column to messages if missing
    try:
        conn.execute("SELECT thread_id FROM messages LIMIT 1")
    except Exception:
        conn.execute(f"ALTER TABLE messages ADD COLUMN thread_id TEXT DEFAULT '{DEFAULT_THREAD_ID}'")
        _log.info("migrated messages table: added thread_id column")

    # Ensure default thread exists
    row = conn.execute("SELECT id FROM threads WHERE id=?", (DEFAULT_THREAD_ID,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO threads (id, name, created_at, updated_at) VALUES (?,?,?,?)",
            (DEFAULT_THREAD_ID, DEFAULT_THREAD_NAME, time.time(), time.time())
        )
    conn.commit()


def _gen_id() -> str:
    """Generate a short thread id: t_<timestamp_hex>"""
    return f"t_{int(time.time() * 1000) % 0xFFFFFFFF:08x}"


# ── CRUD ──

def create(name: str, meta: dict | None = None) -> dict:
    """Create a new thread. Returns thread dict."""
    _ensure_table()
    tid = _gen_id()
    now = time.time()
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO threads (id, name, created_at, updated_at, meta) VALUES (?,?,?,?,?)",
        (tid, name, now, now, json.dumps(meta or {}))
    )
    conn.commit()
    _log.info(f"thread created: {tid} '{name}'")
    return {"id": tid, "name": name, "created_at": now, "updated_at": now, "archived": False, "messages": 0}


def get(tid: str) -> dict | None:
    """Get thread by id."""
    _ensure_table()
    conn = db._get_conn()
    row = conn.execute(
        "SELECT id, name, created_at, updated_at, archived, meta FROM threads WHERE id=?",
        (tid,)
    ).fetchone()
    if not row:
        return None
    msg_count = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=?", (tid,)
    ).fetchone()[0]
    return {
        "id": row[0], "name": row[1], "created_at": row[2], "updated_at": row[3],
        "archived": bool(row[4]), "meta": json.loads(row[5] or "{}"), "messages": msg_count,
    }


def list_all(include_archived: bool = False) -> list[dict]:
    """List all threads, sorted by last activity."""
    _ensure_table()
    conn = db._get_conn()
    q = "SELECT id, name, created_at, updated_at, archived FROM threads"
    if not include_archived:
        q += " WHERE archived=0"
    q += " ORDER BY updated_at DESC"
    rows = conn.execute(q).fetchall()

    result = []
    active = get_active_id()
    for tid, name, created, updated, archived in rows:
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", (tid,)
        ).fetchone()[0]

        # Get last message preview
        last_row = conn.execute(
            "SELECT content FROM messages WHERE thread_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (tid,)
        ).fetchone()
        preview = (last_row[0][:60] + "...") if last_row and last_row[0] and len(last_row[0]) > 60 else (last_row[0] if last_row else "")

        result.append({
            "id": tid, "name": name, "created_at": created, "updated_at": updated,
            "archived": bool(archived), "messages": msg_count, "preview": preview or "",
            "active": tid == active,
        })
    return result


def rename(tid: str, name: str) -> str:
    """Rename a thread."""
    _ensure_table()
    conn = db._get_conn()
    conn.execute("UPDATE threads SET name=?, updated_at=? WHERE id=?", (name, time.time(), tid))
    conn.commit()
    _log.info(f"thread renamed: {tid} → '{name}'")
    return f"✓ Renamed to '{name}'"


def archive(tid: str) -> str:
    """Archive a thread (hide from default list)."""
    _ensure_table()
    if tid == DEFAULT_THREAD_ID:
        return "✗ Can't archive the default thread"
    conn = db._get_conn()
    conn.execute("UPDATE threads SET archived=1, updated_at=? WHERE id=?", (time.time(), tid))
    conn.commit()

    # If archiving active thread, switch to default
    if tid == get_active_id():
        switch(DEFAULT_THREAD_ID)

    _log.info(f"thread archived: {tid}")
    return f"✓ Thread archived"


def delete(tid: str) -> str:
    """Delete a thread and all its messages."""
    _ensure_table()
    if tid == DEFAULT_THREAD_ID:
        return "✗ Can't delete the default thread"
    conn = db._get_conn()
    conn.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
    conn.execute("DELETE FROM threads WHERE id=?", (tid,))
    conn.commit()

    if tid == get_active_id():
        switch(DEFAULT_THREAD_ID)

    _log.info(f"thread deleted: {tid}")
    return f"✓ Thread deleted"


# ── Active thread ──

def get_active_id() -> str:
    """Get active thread id."""
    global _active_id
    if _active_id is None:
        _ensure_table()
        _active_id = db.kv_get("active_thread") or DEFAULT_THREAD_ID
    return _active_id


def switch(tid: str) -> str:
    """Switch to a different thread."""
    global _active_id
    _ensure_table()

    # Verify thread exists
    conn = db._get_conn()
    row = conn.execute("SELECT name FROM threads WHERE id=?", (tid,)).fetchone()
    if not row:
        return f"✗ Thread '{tid}' not found"

    old = get_active_id()
    _active_id = tid
    db.kv_set("active_thread", tid)
    _log.info(f"thread switched: {old} → {tid} ('{row[0]}')")
    return f"✓ Switched to '{row[0]}'"


def touch(tid: str | None = None):
    """Update thread's updated_at timestamp."""
    tid = tid or get_active_id()
    _ensure_table()
    conn = db._get_conn()
    conn.execute("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), tid))
    conn.commit()
