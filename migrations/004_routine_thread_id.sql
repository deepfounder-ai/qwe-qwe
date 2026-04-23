-- v0.17.30: routine reworks the scheduler to bind each routine to a
-- permanent thread. ONE routine → ONE thread that persists for the
-- lifetime of the routine — every firing appends a new turn there, so
-- users see the routine as an evolving chat log with full tool-call
-- history, not a stateless cron entry.
--
-- The thread is created at routine-save time (scheduler.add) and reused
-- on every _check_and_run firing. For legacy rows (added before this
-- migration) thread_id starts NULL and is lazy-filled on next firing.
BEGIN;

ALTER TABLE scheduled_tasks ADD COLUMN thread_id TEXT;

COMMIT;
