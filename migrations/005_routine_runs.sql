-- v0.17.32: per-fire run history for routines.
--
-- Before this migration, each routine kept only the LAST run's metrics
-- (last_run, last_status, last_duration_ms, last_error, last_result).
-- Users couldn't see:
--   - how many times a routine has actually fired vs been scheduled
--   - which runs failed and which succeeded over time
--   - runs that were MISSED because the server was offline at the
--     scheduled time — a normal occurrence for a local-first agent that
--     only runs while the computer is awake
--
-- routine_runs stores one row per fire attempt (actual or missed). The
-- scheduler detects "server was offline across scheduled slots" on
-- startup and inserts status=missed rows so users can see the history
-- honestly.
--
-- status values:
--   ok      — agent.run finished, no error marker in the reply
--   err     — agent.run raised, or the reply matched _DRY_RUN_ERROR_MARKERS
--   missed  — server was offline at the scheduled time; never executed
--   skipped — per-thread fire lock was held (concurrent fire in progress)
BEGIN;

CREATE TABLE IF NOT EXISTS routine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cron_id INTEGER NOT NULL,
    scheduled_at REAL NOT NULL,   -- when the run was supposed to start
    started_at REAL,              -- when agent.run actually began (NULL for missed)
    finished_at REAL,             -- when agent.run returned (NULL for missed/running)
    duration_ms INTEGER,
    status TEXT NOT NULL,         -- ok / err / missed / skipped
    error TEXT,
    result_preview TEXT,
    thread_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_routine_runs_cron_id ON routine_runs(cron_id);
CREATE INDEX IF NOT EXISTS idx_routine_runs_scheduled_at ON routine_runs(scheduled_at);

COMMIT;
