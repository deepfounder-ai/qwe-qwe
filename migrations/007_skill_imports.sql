-- v0.18.x: audit trail for skills imported from skills.sh / GitHub.
--
-- qwe-qwe skills live as single `.py` files at ~/.qwe-qwe/skills/.
-- When the user imports a skill from skills.sh (or any compatible
-- Anthropic-style SKILL.md source), we generate a thin adapter `.py`
-- AND stage the original assets (scripts/, references/) under
-- ~/.qwe-qwe/skills_imported/<name>/.
--
-- This table records the provenance: which `.py` came from which URL,
-- what SHA the source had at import time, what license the upstream
-- declared, when we pulled it. Used for:
--   * "imported from skills.sh" badge in the Skills list
--   * future "check for upstream updates" workflow (compare hash)
--   * licensing audit on source-available content (Anthropic docx,
--     pdf, pptx, xlsx are source-available, NOT OSS — record so the
--     user can verify compliance later)
--
-- Schema is intentionally minimal — name (PK) matches the `.py` file
-- stem so a SELECT + filesystem JOIN is trivial.
BEGIN;

CREATE TABLE IF NOT EXISTS skill_imports (
    name        TEXT PRIMARY KEY,           -- matches `~/.qwe-qwe/skills/<name>.py` stem
    source_url  TEXT NOT NULL,              -- canonical user-supplied URL (skills.sh/<…> or github://…)
    source_kind TEXT NOT NULL,              -- "skills_sh" / "github" — for future fetcher routing
    hash        TEXT NOT NULL,              -- SHA-256 of canonical SKILL.md content at import
    license     TEXT,                       -- frontmatter `license` field, free-text
    description TEXT,                       -- frontmatter `description` (≤1024 chars)
    imported_at REAL NOT NULL,              -- UNIX timestamp
    meta        TEXT                        -- JSON sidecar: file list, frontmatter metadata.*, etc.
);

CREATE INDEX IF NOT EXISTS idx_skill_imports_at ON skill_imports(imported_at DESC);

COMMIT;
