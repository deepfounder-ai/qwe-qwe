# Changelog

## v0.8.0 — 2026-04-04

### Browser Skill
- Native browser control via **Playwright + headless Chromium**
- 7 tools: `browser_open`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_fill`, `browser_eval`, `browser_close`
- Tool alias system — hallucinated names like `google_search` redirect to real browser tools

### Tool Search (Meta-Tool)
- **`tool_search`** — discover and activate tools by keyword instead of loading all 46 at once
- Only 8 core tools loaded by default (~750 tokens vs ~3500 previously)
- **75% token savings** on every API call
- Keywords: browser, notes, schedule, secret, mcp, profile, rag, skill, soul, timer, model

### System Prompt Optimization
- Removed redundant tool descriptions from system prompt (tools= API param handles it)
- Consolidated rules from 13 to 10 compact lines
- System prompt: ~2600 tokens -> ~1200 tokens (**-55%**)
- Total (prompt + tools): ~8000 -> ~2000 tokens (**-75%**)

### Gemma 4 Support
- Strip `<|channel>thought` thinking tags from Gemma responses
- Detect and redirect Gemma thinking in streaming (not leaked as content)
- Strip generic `<|...|>` special tokens from output
- Anti-hedge nudge: if model talks instead of acting (any language), retry with "use tools NOW"

### Tool Call UI (Claude Code Style)
- Collapsible grouped tool calls in chat messages (e.g. "2 commands", "3 file reads")
- Each tool shows: name, human-readable description, result preview
- Live activity log during streaming, persists in final message
- Tool calls shown on history reload from meta.tools

### Shell & Path Fixes (Windows)
- Shell executes via **Git Bash** on Windows (UNIX commands work everywhere)
- Auto-convert Git Bash paths (`/c/Users/...`) in `read_file`, `write_file`, and MCP tools
- System prompt provides shell-compatible paths per OS
- `PYTHONIOENCODING=utf-8` + `encoding="utf-8"` for subprocess — no more `(no output)` from encoding errors
- `SafeConsole` wrapper prevents cp1251 emoji crashes

### Agent Improvements
- Self-check validates required fields before applying corrections (no more `KeyError: 'command'`)
- Shell timeout hints: "Use spawn_task for servers" when uvicorn/flask detected
- Stuck detection: warn model after 5+ tool errors per turn
- `write_file` self-verify only for sensitive paths (not workspace)
- Default skills always included even after DB save
- Removed config upper limits — all settings uncapped

### UI Cleanup
- Removed thread stats footer
- Aligned Send/Stop buttons with input field
- Duration shown in seconds (not ms) in message meta
- Removed old tool-tag badges from message footer

---

## v0.7.0 — 2026-04-03

### MCP Client Support
- **Model Context Protocol** client with stdio and HTTP transports
- JSON-RPC 2.0 over subprocess stdin/stdout (stdio) and HTTP POST
- Auto-discovery of tools via `tools/list`, execution via `tools/call`
- Tool naming: `mcp__servername__toolname`
- Config stored in SQLite KV
- MCP REST API endpoints in server.py
- `mcp_manager` skill: add, remove, restart, toggle MCP servers from chat

### Left Sidebar Layout
- ChatGPT-style left sidebar for thread navigation
- Desktop: collapsible sidebar (260px)
- Mobile: slide-in overlay with hamburger menu
- Vertical thread list with active highlight

### Mobile Responsive
- Touch-friendly: `touch-action: manipulation`, zoom prevention
- Horizontal scrollable settings nav
- Overflow fixes for long text in message boxes
- Proper input width on mobile

### Other
- Code block copy button in chat messages
- Agent self-awareness in system prompt (knows its own systems, file paths)
- MCP settings section in Web UI
- Breathing animation on all borders for live content

---

## v0.6.0 — 2026-04-02

### Real-Time Streaming
- **asyncio.Queue pattern** for WebSocket streaming (fixes run_coroutine_threadsafe deadlock)
- Content, thinking, and status callbacks from agent thread to async event loop
- Rich Live progressive Markdown rendering in CLI

### Telegram Upgrade
- Streaming via `editMessageText` fallback
- Inline keyboard (Retry button)
- `drop_pending_updates=True` to fix polling conflicts
- Topic-to-thread mapping for supergroups

### Windows Support
- `setup.bat` installer with Python detection, venv, dependency verification
- Fixed `result.stdout or ""` NoneType bug in shell tool
- Firewall rule documentation for LAN access

### Other
- Streaming toggle for Telegram in system settings
- Abort on WebSocket disconnect
- Various encoding fixes (cp1251 on Windows)

---

## v0.5.0 — 2026-03-20

### Hybrid Search & Embeddings
- **FastEmbed** replaces OpenAI/LM Studio for embeddings — fully local ONNX inference
- **Multilingual embeddings** — paraphrase-multilingual-MiniLM-L12-v2 (50+ languages)
- **Hybrid search** — dense + sparse (SPLADE++) fused via Reciprocal Rank Fusion
- IDF modifier on sparse index
- Qdrant-side score filtering (0.45 memory, 0.5 experience)
- Float16 vectors (2x less storage)
- Auto-migration v1->v2 with crash recovery

### Small Model Optimizations
- Smart tool output summarization
- Progressive context injection
- Chain-of-workers (max depth 3, total 45 rounds)
- Self-knowledge in system prompt
- spawn_task delegation rule

### Skill Creator
- Telegram template for Bot API integration
- Auto-detection of operation types
- Param validation in smoke tests
- delete_skill tool
- Template-based generation

---

## v0.4.0

- Setup-inference wizard with Ollama auto-install
- LLM fallback hybrid mode
- Configurable RAG chunk size
- Interactive model selection
