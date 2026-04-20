# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run

```bash
# Install
./setup.sh                    # Linux/Mac — creates .venv, installs deps, pre-loads embeddings
setup.bat                     # Windows

# Run
python cli.py                 # Terminal chat
python cli.py --web --ssl --port 7861  # Web UI (HTTPS, camera/mic require HTTPS)
qwe-qwe --web                # If installed as package
qwe-qwe --doctor             # Diagnostics (20+ checks)

# Tests
pytest tests/ -v              # All tests
pytest tests/test_json_repair.py -v  # Single test file

# Lint
ruff check .
```

Requires Python 3.11+. LM Studio or Ollama must be running with a loaded model.

## Architecture

Single-process Python agent with FastAPI web server. All state in SQLite + Qdrant (local disk mode, no server needed).

### Core Flow

```
server.py (WebSocket) → agent.run() → _build_messages() → agent_loop.run_loop()
    → LLM API (streaming) → tool calls → tools.execute() → repeat until finish_reason=stop
```

### Agent Loop (agent_loop.py — v2)

`run_loop()` is the execution engine. Key mechanisms:

- **Tool result clearing**: before each LLM call, old tool results replaced with one-line summaries (keep last 3 intact). Prevents context overflow during multi-step tasks.
- **Tool result cap**: individual results capped at 4K chars.
- **Text-to-tool extraction**: if model describes a tool call in text but doesn't emit `delta.tool_calls`, regex extracts and executes it (handles Qwen leaked `<tool_call>` syntax, function-call-in-text patterns).
- **Loop detection**: 2 identical tool call signatures (same tool + same args) → `_force_finish=True`. No hard round/call limits — agent works until done.
- **No artificial limits**: `max_turns=0`, `max_tool_calls=0` (unlimited). Only loop detection stops infinite loops.
- **Anti-hedge**: if model produces only thinking tags and empty reply, nudge once with assistant continuation (no `[system]` user messages — those break model flow).
- **tool_search short-circuit**: 2nd+ call returns "tools ALREADY ACTIVE" instead of re-listing.
- **Abort support**: checks `abort_event` at loop start + every streaming chunk.

**Design principle** (learned from OpenCode): don't inject mid-conversation `[system]` messages as user role — they make the model think the user is speaking and break execution flow. Use system prompt rules instead.

### Tool System (tools.py)

**Core tools** (always loaded, ~18): memory_save, memory_search, read_file, write_file, shell, http_request, spawn_task, tool_search, browser_open, browser_snapshot, browser_click, browser_fill, browser_eval, browser_set_visible, send_file, camera_capture, open_url.

**Extended tools** (activated via `tool_search("keyword")`): notes, schedule, secret, mcp, profile, rag, skill, soul, timer + 13 more browser tools. Meta-tool pattern saves ~75% tokens.

**`_get_path_arg(args)`**: extracts path from tool args — models use various field names (`path`, `file_path`, `filepath`, `file`). Used by read_file, write_file, send_file, rag_index.

**send_file(path)**: copies file to uploads/, queues in `_pending_files` list. Server includes files in WS reply payload. Web UI renders as download cards; Telegram sends via `sendDocument`.

**camera_capture(prompt?)**: grabs frame via WebSocket (browser) or OpenCV (direct). Sends to current LLM for vision analysis. Camera stays open (persistent `_camera_cap`) for fast subsequent captures. Auto-detects best camera index by brightness.

**Shell**: runs via Git Bash on Windows (`_detect_shell()`). `_resolve_path()` converts Git Bash paths (`/c/Users/...`) to Windows (`C:/Users/...`).

### Memory (memory.py)

Three-layer knowledge in single Qdrant collection (`qwe_qwe`):
- **Raw** (`tag=knowledge/fact/user/etc`) — immediate saves, auto-chunked if >1000 chars
- **Entity** (`tag=entity`) — graph nodes with typed relations, created by night synthesis
- **Wiki** (`tag=wiki`) — synthesized summaries, best search quality

Hybrid search: FastEmbed dense (384d, multilingual) + SPLADE++ sparse, fused via RRF.

**Session isolation**: thread-scoped raw memory search first, then cross-thread only for synthesized knowledge (wiki, entity, fact, knowledge, user, project, decision, idea). Raw messages from other threads are NOT injected.

**Structured compaction**: when context fills, LLM creates 9-section summary (Current State, Goals, Key Files, Learnings, Next Steps...) injected back as conversation message.

### System Prompt (soul.py)

`to_prompt()` builds system prompt. **Order matters for KV cache** — static rules first, dynamic context last.

Key rules the model follows:
- Rule 3: "NEVER STOP EARLY" — keep calling tools until ALL steps complete
- Rule 6: BROWSER MODES: `browser_open` = read silently (headless); `open_url` = show user a page; `browser_set_visible(true)` + browser tools = interact with visible browser window
- Rule 11: Brave Search for web search (not Google/DuckDuckGo — they block headless)
- Rule 12: After write_file, call send_file to attach file to chat
- Rule 14: "MULTI-STEP: plan mentally then EXECUTE each step"

### Skills (skills/__init__.py)

Pluggable Python modules in `skills/`. Each exports: `DESCRIPTION`, `INSTRUCTION`, `TOOLS` (OpenAI function schema list), `execute(name, args) -> str`.

**Browser skill** (skills/browser.py — 23 tools): open, snapshot, screenshot, click, fill, select, hover, drag, press_key, wait_for, upload, eval, network, console, accessibility (via CDP), tabs (new/switch/close), back/forward/reload. Search engine results auto-extract clickable URLs.

### Providers (providers.py)

OpenAI-compatible client for 7 providers. `list_all()` pings local providers in parallel with 30s cache. `ping()` timeout is 1s.

### Voice (stt.py, tts.py)

**STT**: configurable backend (auto/local/api). Local = faster-whisper on CPU. API = OpenAI-compatible (Groq free tier works). PyAV fallback when ffmpeg not in PATH.

**TTS**: auto-detects API style from URL. Supports OpenAI `/v1/audio/speech`, custom `/tts` with `prompt_audio` (voice cloning), Fish Speech `/v1/tts`, s2.cpp `/generate`.

### Web UI (static/index.html)

Single-file SPA. Key features:
- Telegram-style input bar (pill input, borderless icons)
- Mobile: three-dot menu for all actions
- Live Voice Mode: VAD → STT → LLM → TTS → auto-listen loop
- Camera PiP overlay with auto-capture on send
- File attachments rendered as download cards; camera captures as inline images
- Settings: Soul, Model, Skills, Threads, Profile, Heartbeat, Voice, Vision, MCP, Stats, System

## Key Patterns

- **Shell via Git Bash on Windows** — always use UNIX commands.
- **Path conversion**: `_resolve_path()` handles Git Bash → Windows paths.
- **Gemma support**: strips `<|channel>thought` tags from streaming and responses.
- **Self-check**: validates tool args before shell/write_file.
- **SafeConsole**: wraps Rich console to catch cp1251 encoding errors on Windows.
- **File uploads**: Web UI and Telegram support drag/drop files. Server saves to uploads/, injects path reference (not content) into user message.
- **Windows asyncio**: custom exception handler silences `ConnectionResetError` from MCP subprocess cleanup.
- **Warning suppression**: FastEmbed pooling warning + Qdrant local index warnings suppressed via `warnings.catch_warnings()`.
- **Shared utilities**: `utils.py` contains `strip_thinking()` and `extract_thinking()` — single canonical implementation imported by agent.py, agent_loop.py, tasks.py.
- **Preset isolation**: activating a preset switches thread + workspace. Deactivating restores originals.
- **Visible browser**: `browser_set_visible(true)` launches Playwright with `headless=False`. All 23 browser tools work on the visible window.

## Data Layout

All user data in `~/.qwe-qwe/` (configurable via `QWE_DATA_DIR`):
- `qwe_qwe.db` — SQLite (messages, threads, settings, scheduled tasks)
- `memory/` — Qdrant vectors (disk mode)
- `wiki/` — synthesized markdown pages
- `skills/` — user-created skills
- `uploads/` — images, documents, camera captures
- `workspace/` — default CWD for relative paths (switches to preset workspace when preset is active)
- `presets/<id>/` — installed presets (each with own workspace/, knowledge/, skills/)
- `logs/` — qwe-qwe.log (INFO+), errors.log (WARNING+)

## Environment Variables

`QWE_LLM_URL` (default localhost:1234/v1), `QWE_LLM_MODEL`, `QWE_LLM_KEY`, `QWE_DB_PATH`, `QWE_DATA_DIR`, `QWE_QDRANT_MODE` (memory/disk/server), `QWE_PASSWORD` (web auth), `QWE_STT_DEVICE` (cpu/cuda).

## Release Checklist

Before every release, verify these steps:

### 1. pyproject.toml `py-modules` is complete

Every `.py` module that `import X` needs at runtime MUST be listed in `[tool.setuptools] py-modules`. If you add a new module (e.g. `my_feature.py`), add it to this list. Missing modules cause `ModuleNotFoundError` when installed via `pip install -e .` or the curl installer.

```bash
# Quick check: find all imported local modules not in py-modules
python -c "
import ast, pathlib
toml = pathlib.Path('pyproject.toml').read_text()
modules_line = [l for l in toml.split('\n') if 'py-modules' in l][0]
registered = set(m.strip().strip('\"') for m in modules_line.split('[')[1].split(']')[0].split(','))
py_files = {p.stem for p in pathlib.Path('.').glob('*.py') if p.stem != '__init__' and not p.stem.startswith('test')}
missing = py_files - registered - {'setup'}
if missing: print(f'MISSING from py-modules: {missing}')
else: print('All modules registered')
"
```

### 2. Version bumped in BOTH files

Update version in **both** `config.py` (`VERSION = "X.Y.Z"`) and `pyproject.toml` (`version = "X.Y.Z"`). The `/api/version` endpoint reads from `pyproject.toml` via `updater._current_version()`.

### 3. `--doctor` covers new features

Run `python cli.py --doctor` and verify new features have checks. When adding a new subsystem (voice, camera, browser, etc.), add a corresponding check in `cli.py:doctor()`. Doctor must not crash on any platform — use try/except and avoid unicode emoji (cp1251 terminals crash on ⚡✓⚠).

### 4. Compile check all modified files

```bash
python -m py_compile agent.py agent_loop.py tools.py server.py soul.py config.py memory.py
```

### 5. Test the install path

The curl installer (`install.sh`) does `git clone → pip install -e .`. If `py-modules` is wrong, the install succeeds but the app crashes on import. After pushing, test:
```bash
# On a clean machine or venv:
pip install -e . && python -c "import agent, agent_loop, tools, server"
```

### 6. Tag and release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main --tags
gh release create vX.Y.Z --title "vX.Y.Z — ..." --notes "..."
```
