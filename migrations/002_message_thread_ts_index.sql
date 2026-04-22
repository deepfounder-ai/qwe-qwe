-- 002_message_thread_ts_index.sql
-- Example migration demonstrating the convention. Also a real (cheap)
-- optimisation: the common history query filters by thread_id and orders
-- by id/ts, so a composite index helps larger conversations.

CREATE INDEX IF NOT EXISTS idx_messages_thread_ts ON messages(thread_id, ts);
