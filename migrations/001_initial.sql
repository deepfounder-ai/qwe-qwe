-- 001_initial.sql
-- Baseline schema — snapshot of all tables/indexes that existed before the
-- versioned migration system landed. Everything uses IF NOT EXISTS so that
-- running this against a pre-existing install is a no-op. The runner in db.py
-- also short-circuits this file entirely when it detects a pre-migration DB
-- (see _stamp_existing_db).
--
-- Consolidated from:
--   db.py        : messages, kv, presets, fts_rag, fts_memory, idx_messages_ts, meta col
--   threads.py   : threads table, messages.thread_id column
--   scheduler.py : scheduled_tasks
--   vault.py     : secrets

-- Conversation history ------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,       -- JSON array of tool calls (assistant)
    tool_call_id TEXT,     -- for tool results
    name TEXT,             -- tool name (for tool results)
    ts REAL NOT NULL,
    thread_id TEXT DEFAULT 'default',
    meta TEXT              -- JSON metadata (tools, duration, ...)
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

-- Key/value store -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);

-- Presets (installed manifests) --------------------------------------------
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

-- Threads -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived INTEGER DEFAULT 0,
    meta TEXT DEFAULT '{}'
);

-- Scheduled tasks -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    task TEXT NOT NULL,
    schedule TEXT NOT NULL,
    next_run REAL NOT NULL,
    last_run REAL,
    repeat INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1
);

-- Encrypted secrets ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS secrets (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- FTS5 BM25 full-text search (hybrid with Qdrant vector search) ------------
-- These use the fts5 virtual-table module. The runner wraps each migration
-- in a transaction, but fts5 may not be available on exotic SQLite builds;
-- if creation fails, db.py falls back to logging a warning and continues.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_rag USING fts5(
    chunk_id, file_path, text,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_memory USING fts5(
    point_id, tag, text,
    tokenize='porter unicode61'
);
