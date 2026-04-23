-- v0.17.29: scheduler analytics.
--
-- The original `scheduled_tasks` table (see 001_initial.sql) tracked only
-- next_run / last_run / repeat / enabled — so the UI's "total runs" stat
-- was always 0 and "last status" had nothing to show. This migration adds
-- execution metrics so the scheduler view actually means something.
--
-- Back-compat note: very old installs that predate the migrations system
-- were stamped at schema_version=1 without the baseline SQL actually
-- running, so they may not even have the `scheduled_tasks` table yet
-- (it's also created lazily by scheduler._ensure_table()). The CREATE
-- TABLE IF NOT EXISTS mirror of the 001 shape below makes this migration
-- safe on both fresh and pre-migrations installs. On a freshly-migrated
-- DB it's a no-op; on a legacy DB it creates the table in its 001 shape
-- before the ALTERs bring it up to date.
--
-- SQLite's ALTER TABLE ADD COLUMN is cheap and doesn't rewrite rows; each
-- column defaults to NULL (or 0 for run_count) on existing rows, so the
-- first post-migration execution of each task populates its own metrics.
BEGIN;

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

ALTER TABLE scheduled_tasks ADD COLUMN run_count INTEGER DEFAULT 0;
ALTER TABLE scheduled_tasks ADD COLUMN last_status TEXT;
ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT;
ALTER TABLE scheduled_tasks ADD COLUMN last_duration_ms INTEGER;
ALTER TABLE scheduled_tasks ADD COLUMN last_result TEXT;

COMMIT;
