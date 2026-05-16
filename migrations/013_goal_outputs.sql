-- v0.22.2: durable per-goal deliverables (files, links, reports).
--
-- A Goal's final reply lives in goals.result as free-form text, but
-- structured deliverables (a CSV the agent wrote, a URL to view, a
-- markdown report the user might want to save into long-term memory)
-- deserve first-class storage so the UI can render Download / Open /
-- Save buttons without parsing prose.
--
-- The orchestrator calls `goal_attach_output(kind, title, value)` during
-- (or at the end of) goal execution. Three kinds are supported today:
--
--   file    — value is an absolute path under ~/.castor/workspace/.
--             Download endpoint streams the bytes; rejects paths outside
--             the workspace as a directory-traversal guard.
--   link    — value is an http(s) URL the user can open in a real browser.
--   report  — value is a markdown body the orchestrator wrote as a
--             standalone artifact. UI renders inline + offers "Save to
--             memory" so the user can persist insights to Qdrant.
--
-- ON DELETE CASCADE keeps outputs in sync when a goal is deleted via
-- DELETE /api/goals/{id} or the bulk cleanup endpoint.
BEGIN;

CREATE TABLE goal_outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,             -- 'file' | 'link' | 'report'
    title       TEXT NOT NULL,             -- human-facing label
    value       TEXT NOT NULL,             -- path / URL / markdown body
    meta        TEXT NOT NULL DEFAULT '{}', -- JSON: byte_size, content_type, etc
    created_at  REAL NOT NULL
);

CREATE INDEX idx_goal_outputs_goal ON goal_outputs (goal_id);

COMMIT;
