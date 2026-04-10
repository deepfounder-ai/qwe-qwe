# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run

```bash
# Install
./setup.sh                    # Linux/Mac — creates .venv, installs deps, pre-loads embeddings
setup.bat                     # Windows

# Run
python cli.py                 # Terminal chat
python -m uvicorn server:app --host 0.0.0.0 --port 7860  # Web UI
qwe-qwe --web                # If installed as package
qwe-qwe --doctor             # Diagnostics (14 checks)

# Tests
pytest tests/ -v              # All tests
pytest tests/test_json_repair.py -v  # Single test file

# Lint
ruff check .
```

Requires Python 3.11+. LM Studio or Ollama must be running with a loaded model.

## Architecture

Single-process Python agent with FastAPI web server. All state in SQLite + Qdrant (local disk mode, no server needed).

### Core Loop

`agent.py:_run_inner()` — the brain. Receives user input, builds messages, calls LLM via OpenAI-compatible API, processes tool calls in a loop until `finish_reason=stop` or max rounds.

Key flow: `server.py` (WebSocket) → `agent.run()` → `_build_messages()` → LLM API → tool calls → `tools.execute()` → repeat.

### Tool System (tools.py)

**Core tools** (always loaded, ~8): memory_save, memory_search, read_file, write_file, shell, http_request, spawn_task, tool_search, self_config.

**Extended tools** (activated via `tool_search("keyword")`): browser, notes, schedule, secret, mcp, profile, rag, skill, soul, timer. This meta-tool pattern saves ~75% tokens vs loading all 46 tools.

`_active_extra_tools` set tracks what's been activated per turn. `_reset_active_tools()` clears between turns.

### Memory (memory.py)

Three-layer knowledge in single Qdrant collection (`qwe_qwe`):
- **Raw** (`tag=knowledge/fact/user/etc`) — immediate saves, auto-chunked if >1000 chars
- **Entity** (`tag=entity`) — graph nodes with typed relations, created by night synthesis
- **Wiki** (`tag=wiki`) — synthesized summaries, best search quality

Hybrid search: FastEmbed dense (384d) + SPLADE++ sparse + SQLite FTS5 BM25, fused via RRF.

`synthesis.py` runs at 03:00 (configurable) — processes pending chunks, extracts entities/relations via LLM, builds wiki pages.

### System Prompt (soul.py)

`to_prompt()` builds the system prompt. Order matters for KV cache:
1. Rules (static, ~10 lines)
2. Identity + personality traits (dynamic per soul config)
3. Self-knowledge: file paths, shell type, active skills
4. Caveman mode injection when `brevity=high`
5. Spicy Duck mode injection when that hidden skill is active
6. Time, model info

### Skills (skills/__init__.py)

Pluggable Python modules in `skills/`. Each skill exports: `DESCRIPTION`, `INSTRUCTION`, `TOOLS` (OpenAI function schema list), `execute(name, args) -> str`.

`_DEFAULT_SKILLS` always loaded. `_HIDDEN_SKILLS` require activation key in DB (e.g., `spicy_duck` needs `db.kv_get("spicy_duck") == "quack"`).

Skill tools dispatched in `tools.execute()` after built-in tools, before MCP.

### Providers (providers.py)

OpenAI-compatible client for 7 providers: LM Studio, Ollama, OpenAI, OpenRouter, Groq, Together, DeepSeek. Runtime switching via `switch_model`.

### Scheduler (scheduler.py)

SQLite-backed cron with daemon thread. Special tasks: `__heartbeat__` (periodic checks), `__synthesis__` (knowledge graph). Natural schedule syntax: "in 5m", "every 2h", "daily 09:00".

## Key Patterns

- **Shell runs via Git Bash on Windows** (`tools.py:_SHELL_EXE`). Always use UNIX commands in shell descriptions.
- **Path conversion**: `_resolve_path()` converts Git Bash paths (`/c/Users/...`) to Windows (`C:/Users/...`) for read_file/write_file. `mcp_client._fix_paths_in_args()` does the same for MCP tools.
- **Anti-hedge**: if model responds without tool calls on round 0 and input >40 chars, injects nudge message then removes it (no history pollution).
- **Gemma support**: strips `<|channel>thought` tags from streaming and responses.
- **Self-check**: validates tool args before dangerous operations (shell, write_file). Corrections must contain required fields or are rejected.
- **SafeConsole**: wraps Rich console to catch cp1251 encoding errors on Windows.
- **File uploads**: Web UI and Telegram support drag/drop of .txt/.py/.md/.pdf files. Server extracts text via `_extract_file_text()`, injected into user message.

## Data Layout

All user data in `~/.qwe-qwe/` (configurable via `QWE_DATA_DIR`):
- `qwe_qwe.db` — SQLite (messages, threads, settings, scheduled tasks)
- `memory/` — Qdrant vectors (disk mode)
- `wiki/` — synthesized markdown pages
- `skills/` — user-created skills
- `uploads/` — images and documents from chat
- `logs/` — qwe-qwe.log (INFO+), errors.log (WARNING+)

## Environment Variables

`QWE_LLM_URL` (default localhost:1234/v1), `QWE_LLM_MODEL`, `QWE_LLM_KEY`, `QWE_DB_PATH`, `QWE_DATA_DIR`, `QWE_QDRANT_MODE` (memory/disk/server), `QWE_PASSWORD` (web auth).
