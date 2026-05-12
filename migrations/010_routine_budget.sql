-- v0.21.0: per-routine USD budget caps.
--
-- budget_usd_cap   — null = no cap (default); positive number = cap in dollars
-- budget_period_sec — rolling window length, default 86400 (24h)
--
-- When set: scheduler sums agent_runs.cost_usd for this cron_id over the
-- last budget_period_sec; if sum >= cap, the next fire is skipped with
-- status='skipped', reason='budget_exceeded' in agent_runs (so history
-- honestly reflects what happened).
BEGIN;

ALTER TABLE scheduled_tasks ADD COLUMN budget_usd_cap REAL;
ALTER TABLE scheduled_tasks ADD COLUMN budget_period_sec INTEGER DEFAULT 86400;

COMMIT;
