-- v0.20.0: support auto-resume for interrupted turns.
--
-- Two narrow additions on agent_runs (created by migration 008):
--   1. resumed_from_run_id — chains resume runs back to their original
--      aborted run, so analytics can show "run #142 (resumed #138)".
--   2. dismissed_at — user clicked Dismiss on the banner, or TTL
--      expired. Filtered out of "resume?" prompts.
--
-- The 'interrupted' marker on individual partial messages lives in
-- messages.meta (existing JSON column) — no schema change for that.
BEGIN;

ALTER TABLE agent_runs ADD COLUMN resumed_from_run_id INTEGER;
ALTER TABLE agent_runs ADD COLUMN dismissed_at REAL;

CREATE INDEX IF NOT EXISTS idx_agent_runs_dismissed_at
    ON agent_runs(dismissed_at);

COMMIT;
