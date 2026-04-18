# Agent Instructions: qwe-qwe

## Quick Commands

```bash
./setup.sh         # Linux/Mac: create venv, install deps, preload embeddings
setup.bat          # Windows: same
qwe-qwe            # Terminal chat
qwe-qwe --web      # Web UI at http://localhost:7860
qwe-qwe --doctor   # Diagnostics (20+ checks)
pytest tests/ -v   # Run all tests
ruff check .       # Lint
```

## Architecture

Single-process Python agent with FastAPI web server. All state in SQLite + Qdrant (disk mode).

**Entry points:**
- `cli.py` — terminal chat (rich console)
- `server.py` — FastAPI web server with WebSocket
- `agent.py:_run_inner()` — core loop: build messages → LLM API → tool calls → repeat

**Core components:**
- `tools.py` — 8 core tools loaded by default (~750 tokens). Extended tools activated via `tool_search("keyword")` meta-tool, saving ~3000 tokens vs loading all 46 tools
- `memory.py` — 3-layer Qdrant memory (RAW → ENTITY → WIKI) with hybrid search (FastEmbed + SPLADE++ + SQLite FTS5)
- `soul.py:to_prompt()` — personality + system prompt. Order matters: rules → identity → self-knowledge → caveman/spicy mode injections
- `scheduler.py` — SQLite-backed cron with daemon thread
- `providers.py` — 7 LLM providers (LM Studio, Ollama, OpenAI, OpenRouter, Groq, Together, DeepSeek)

**Data layout** (`~/.qwe-qwe/`):
- `qwe_qwe.db` — SQLite (messages, threads, settings, tasks)
- `memory/` — Qdrant vectors (disk mode)
- `wiki/` — synthesized markdown pages
- `logs/` — qwe-qwe.log, errors.log

## Critical Patterns

### Shell Execution (Windows)
- Uses **Git Bash** (`/c/Users/...`) when available. Path conversion via `_resolve_path()` in `tools.py`
- Never uses WSL (causes stack overflow)
- Shell detection at import time via `_detect_shell()`

### Tool System (Token Optimization)
- Only 8 core tools loaded by default. Extended tools activated via `tool_search()`:
  - Keywords: `browser`, `notes`, `schedule`, `secret`, `mcp`, `profile`, `rag`, `skill`, `soul`, `timer`, `model`, `cron`
- Per-turn tracking via `_active_extra_tools` set (reset each turn in `_reset_active_tools()`)

### JSON Repair
- Small models (Qwen, Gemma) often produce malformed JSON
- Automatic repair: trailing commas, single quotes, unclosed brackets, JS comments, markdown fences
- 3-level retry: repair → schema hint → minimal prompt
- 400 errors from provider disable structured output for session

### Anti-Hedge Mechanism
- On round 0, if model talks without tool calls and input >40 chars, inject nudge message then remove it (no history pollution)

### Gemma Support
- Strips `<|channel>thought` tags from streaming responses and model outputs

### SafeConsole
- Wraps Rich console to catch cp1251 encoding errors on Windows. Never crashes, falls back to ASCII stripping

### Hybrid Search
- FastEmbed dense (384d) + SPLADE++ sparse + SQLite FTS5 BM25 fused via RRF
- Night synthesis at 03:00 (configurable) processes `synthesis_status=pending` chunks

### Write Safety
- `write_file` only allows paths in whitelist: workspace, `~/.qwe-qwe/`, project cwd
- Path validation via `_resolve_path()` with `for_write=True` check

### Signed Presets
- ed25519 signature verification via `presets.py`
- Policy modes: `off`, `warn` (default), `require`
- Controlled by `QWE_PRESET_SIGNATURE_POLICY` env var

### MCP Integration
- MCP servers launched via `mcp_manager.py`
- Path conversion via `mcp_client._fix_paths_in_args()` for Windows paths

## Environment Variables

```bash
QWE_LLM_URL=http://localhost:1234/v1    # LLM server URL (default)
QWE_LLM_MODEL=qwen/qwen3.5-9b          # Model name
QWE_LLM_KEY=lm-studio                   # API key
QWE_DB_PATH=~/.qwe-qwe/qwe_qwe.db      # Database path
QWE_DATA_DIR=~/.qwe-qwe                # All user data dir
QWE_QDRANT_MODE=disk                   # memory | disk | server
QWE_QDRANT_PATH=~/.qwe-qwe/memory      # For disk mode
QWE_PASSWORD=                          # Web UI auth (optional)
QWE_PRESET_SIGNATURE_POLICY=warn       # off | warn | require
QWE_MARKET_PATH=/path/to/market        # Optional preset marketplace
```

## Setup Requirements

**Prerequisites:**
- Python 3.11+
- Git Bash (Windows only, for UNIX commands)
- LM Studio or Ollama running with model loaded before starting qwe-qwe

**Critical dependencies:**
- `cryptography` — for signed presets
- `qdrant-client>=1.11.0` — semantic memory
- `fastembed>=0.4.0` — local embeddings (ONNX)
- `openai>=1.0.0` — OpenAI-compatible client

**Recommended models:**
- Qwen 3.5 9B Q4_K_M (~5.5GB) — best for tool calling
- Gemma 4 E4B-IT (~4GB) — fast, simple tasks

## Testing

```bash
pytest tests/test_json_repair.py -v    # Single test file
pytest tests/ -v                       # All tests
```

**Test coverage:** JSON repair, signed presets, tools, reliability, server endpoints

## Known Gotchas

1. **LM Studio/Ollama must be running** before starting qwe-qwe
2. **Session tokens reset** after structured output 400 errors
3. **Shell safety patterns** block dangerous commands (rm -rf /, fork bombs, etc.)
4. **First-run setup** is interactive (asks for name, timezone, personality)
5. **Docker users:** LLM server runs on host, access via `host.docker.internal:1234`
6. **LAN access toggleable** via `/api/network` endpoint (default: on)
