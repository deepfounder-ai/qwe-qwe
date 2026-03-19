# Changelog

## v0.4.0 (2025-03-19)

### New Features

- **Structured System Prompt** — 8-section architecture: Identity, Runtime, Tools, Memory Protocol, Rules, Examples, Skills, Dynamic Data. 3x more informative for the model, with explicit memory/experience protocol
- **Memento Experience Learning** — Agent saves past task outcomes (success/partial/failed) with composite scoring. Successful approaches are repeated, failed ones avoided. Auto-injected as `[Relevant past experiences:]`
- **LLM Fallback Hybrid Mode** — Auto-escalation to cloud model after 2 tool failures. User-prompted fallback for short/weak responses. Configurable provider + model
- **Ollama Integration** — Full support: `num_ctx` configuration, embedding model setup, inference setup wizard with GPU detection and interactive model selection
- **Inference Setup Wizard** — `qwe-qwe --setup-inference`: detects GPU (NVIDIA/Apple Silicon/CPU), recommends model by VRAM, installs Ollama, pulls model + embedding model, configures provider
- **Web UI: Inference Settings** — Hardware display, model selector with installed/recommended badges, live download progress bar, num_ctx and embedding model configuration
- **Configurable RAG Chunk Size** — Default reduced from 2000 to 800 chars (~200 tokens), optimal for 8-9B models. Configurable via settings

### Security

- **29 vulnerability fixes** across 5 critical, 7 important, 12 moderate, 5 low severity issues
- Shell injection hardening with regex-based command blocking
- Path whitelist (replaced blacklist) for file write operations
- XSS output escaping in web UI
- Vault key relocated to `~/.qwe-qwe/.vault_key`
- Input validation and error path sanitization

### Improvements

- **Data Isolation** — All user data moved to `~/.qwe-qwe/` (XDG-style), agent sandboxed to workspace
- **DB Public API** — 29 internal `_get_conn()` calls replaced with `db.execute()`, `db.fetchall()`, `db.fetchone()`. Atomic `kv_inc()` with SQLite RETURNING
- **Thinking Mode for Ollama** — Handles Ollama's `reasoning` field (not `reasoning_content`)
- **Version Display** — Fixed stale version badge; now reads from pyproject.toml directly
- **Presence Penalty** — Added configurable `presence_penalty` (default 1.5 for Qwen 3.5)
- **KV Cache Optimization** — Dynamic data (time) moved to end of system prompt

### Bug Fixes

- Fixed `[object Object]` display for embed_model in settings
- Fixed updater not reinstalling when new py-modules added
- Fixed model tags for Ollama (correct qwen3.5 sizes)
- Fixed download buttons missing styles in web UI
- Fixed pull progress showing "check terminal" instead of live progress
- Fixed thread ID collision possibility
- Fixed socket leak in provider connections
- Fixed rate limiter off-by-one error
- Fixed N+1 query in message loading

## v0.3.0

Initial public release with CLI, Web UI, Telegram bot, semantic memory, RAG, 32+ tools, skill system, scheduler, and 7 LLM provider support.
