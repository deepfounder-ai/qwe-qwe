# Changelog

## v0.12.0 — 2026-04-11

### Business Presets (.qwp)
- **New `presets.py` module** — full install/activate/uninstall lifecycle for domain-specific agent configurations
- **`.qwp` archive format** — zip containing `preset.yaml` manifest, system prompt, Python skills, markdown knowledge
- **Gumroad-ready** — authors package presets once, buyers install with a drag-drop
- **Soul replacement**: activating a preset backs up current soul, applies the preset's traits; deactivate restores the backup
- **System prompt injection**: preset prompt appended as `## Active preset:` section after personality, inside `soul.to_prompt()`
- **Skills discovery extended**: `skills/__init__.py` picks up skills from the active preset's `skills/` dir (no mixing with user skills)
- **Knowledge auto-indexing**: preset knowledge files go through `rag.index_file` with a `preset:<id>` tag, cleaned up on uninstall
- **Single-active constraint**: activating preset B while A is active deactivates A first (keeps soul/prompt semantics clean)
- **Path safety**: sanitized filenames, blocked zip traversal, validated via JSON schema shipped in `schemas/preset.schema.yaml`
- New config path: `PRESETS_DIR = ~/.qwe-qwe/presets/`
- New DB table: `presets` (id, version, name, category, author, license, manifest, installed_at)

### CLI — `qwe-qwe preset`
- Argparse subcommand: `qwe-qwe preset list|install|activate|deactivate|info|rm`
- Slash command `/preset` in interactive mode follows the same layout
- Supports three install sources: `.qwp` archive, unpacked directory, or bare id (via `QWE_MARKET_PATH`)

### Web UI — Market tab
- **New "Market" page** between Knowledge and Cron — storefront icon in the header
- Drop zone for `.qwp` archives with drag-drop and browse button
- Card grid for installed presets with category badge, license badge (free/commercial/trial)
- Active preset banner with "Running as [name]" + one-click deactivate
- Activate / Deactivate / Remove per card
- Hooks into `loadSoulSettings()` so the Soul card reflects preset traits immediately on activation
- CSS: new `.market-*` classes, reuses existing design tokens (`--accent-shadow`, `--inset-hl`, `--radius-pill`)

### Server endpoints
- `GET    /api/presets`                  — list installed
- `GET    /api/presets/{id}`             — full manifest
- `POST   /api/presets/install`          — multipart upload (.qwp / .zip)
- `POST   /api/presets/{id}/activate`
- `POST   /api/presets/deactivate`
- `DELETE /api/presets/{id}`

### Dev-link workflow (`QWE_MARKET_PATH`)
- Set env var to the local market repo root
- `qwe-qwe preset install <bare-id>` resolves `$QWE_MARKET_PATH/presets/*/<id>/` and installs directly from the dir
- No packing needed during development — edit, overwrite, test

### Dependencies
- Added `PyYAML>=6.0`, `jsonschema>=4.0`

### Tests
- `tests/test_presets.py` — 13 tests covering load (dir + zip), validation (ok + bad schema + missing file), install / uninstall / list, activate / deactivate / soul backup-restore, single-active constraint, system prompt suffix hook, skills dir hook
- Isolated via `QWE_DATA_DIR` env + module reload

### Companion release: `qwe-qwe market`
- **`tools/validate.py`** — schema + file-existence + id uniqueness checks for every preset under `presets/`
- **`tools/pack.py`** — validate + zip → `dist/<id>-<version>.qwp`
- **`.github/workflows/validate.yml`** — CI on every PR
- `.gitignore` for `dist/`, `__pycache__/`, local tool output
- `CONTRIBUTING.md` expanded with sections 10–11: validation/packing workflow + dev-link testing
- `README.md` describes the `.qwp` format and `QWE_MARKET_PATH`

---

## v0.11.0 — 2026-04-11

### Web UI Redesign
- **Modern design system**: CSS tokens for colors, radii, shadows, easing curves
- **Smoother animations**: cubic-bezier easing (`ease-out`, `ease-spring`) replacing linear transitions
- **Depth & polish**: gradient surfaces, inset highlights, backdrop blur on header/sidebar/input area
- **Message bubbles**: softer borders, spring animation on mount, hover-reveal action buttons
- **Chat messages**: linear-gradient user bubbles, warm inline code (`#ffd27a`), fade-in tool groups
- **Input area**: accent glow on focus, gradient Send button with press-down, floating empty logo
- **Sidebar**: spring indicator on active thread, translateX hover shift, glass surface
- **Settings**: gradient active nav, hover padding expand, refined trait toggles and skill switches
- **Provider cards**: gradient+glow on active, inset highlights across all surface cards
- **Knowledge Base**: pill tabs, shine animation on progress bar, rounded tables
- **Typography**: unified 10/11/12/13/14.5/17/20/22/26 scale, 0.8px uppercase letter-spacing
- **Accessibility**: `prefers-reduced-motion` support, focus-visible on all interactive elements
- **Mobile**: column layout for KB split, always-visible message actions, responsive header nav

### Chat File Upload — Context Fix
- **Critical fix**: attached files no longer inline into the user message (broke 32k context on long chats)
- Files are saved to `~/.qwe-qwe/uploads/<uuid>_<name>.ext` and referenced by absolute path
- User message contains only `[File attached: name.ext (1.2 KB) — saved at <path>]` + tool hints
- Agent calls `read_file(path)` or `tool_search('rag') → rag_index(path)` on demand
- File body loads into context only when the agent actually needs it
- `user_meta.file` persists name/path/size → UI renders a file chip on history reload
- Telegram bot uses the same path-reference pattern (shared uploads dir)

### Knowledge Base: Upload from Computer
- **New endpoint** `POST /api/knowledge/upload` — accepts multipart file, saves to `uploads/kb/`
- **Drop zone** above the file browser: drag-drop or click "browse computer"
- **Auto-select**: uploaded files appear immediately in the selection panel, ready to index
- Status feedback: green "✓ N files uploaded — ready to index" / red error state
- **Global drop guard**: `window.drop → preventDefault` so files never open in a new browser tab
- `knowledge_index` validator now accepts paths under `UPLOADS_DIR` (not just `$HOME`)
- Indexing stays fully local — no LLM round-trip, embeddings done in-process

### Design System Cleanup
- New tokens: `--accent-shadow`/`-lg`, `--error-shadow`/`-lg`, `--inset-hl`/`-strong`, `--radius-pill`, `--warn`, `--on-accent`, `--code-bg`, `--code-inline`
- Replaced hardcoded hex colors with semantic variables across 20+ declarations
- Replaced duplicated `0 4px 16px` shadows with tokens
- Dead code: removed unused `--shadow-glow`, `--purple`, `--radius-xl`, duplicate `.msg { flex-shrink }`
- Hover-lift added to `.sidebar-thread`, `.settings-nav-item`, `.nav-btn`
- Active press-down added to `.model-chip`, `.nav-btn`
- Flat surfaces (`.provider-card`, `.thread-row`, `.cron-table`, `.kn-browser`, `.log-viewer`) now have inset highlights

---

## v0.10.0 — 2026-04-10

### Agent Loop v2
- **Clean execution loop** inspired by claw-code-agent architecture
- **Continuation handling**: automatic re-prompt when model truncates on max_tokens
- **Budget system**: multi-dimensional limits (turns, tool calls, tokens)
- **Event emitter**: typed events replace 5 global callback variables
- **Feature flag**: `agent_loop_v2` setting (default: on, legacy loop as fallback)
- **Vision fallback**: strips images when model doesn't support them
- Files: agent_loop.py, agent_events.py, agent_budget.py

### Unified Memory + RAG
- **Single Qdrant collection** for agent memory AND indexed files
- Files from Upload UI now go through memory.save() with `tag=knowledge`
- File content auto-chunks and queues for night synthesis
- Knowledge graph (entities, wiki) can reference uploaded files
- GPU branch (images, scanned PDFs) unified with text path
- Added payload indexes: file_path, source_type, document_tags

### Spicy Duck
- Secret hidden skill: Lovense smart device integration (6 tools)
- LAN API control via Lovense Remote app
- Heart button UI: tap 10x to activate/deactivate
- Auto-logo switch when mode active
- Personality override in system prompt

### Caveman Mode
- Token compression when `brevity=high`
- Inspired by github.com/JuliusBrussee/caveman
- Rules: no filler, no articles, no hedging, fragments OK
- Code and tool calls stay intact

### File Upload in Chat
- Drag & drop .txt, .py, .md, .pdf, .json and 15+ file types
- Document preview chip in Web UI
- Telegram documents: downloaded, text extracted, prepended to message
- Shared `_extract_file_text()` helper (dedup)

### Infrastructure Fixes
- Shell: prefer Git Bash, skip WSL (stack overflow)
- Removed SSRF block for localhost (agent needs local HTTP access)
- Removed blanket `$()` and backtick ban (legitimate bash patterns)
- Fixed http_request for localhost access
- Telegram auto-start in uvicorn lifespan
- `self_config` tool: agent reads/sets own settings
- tok/s display in message footer
- Code review fixes: VERSION constant, timezone-aware cron, entity dedup

---

## v0.9.0 — 2026-04-05

### Knowledge Graph
- **Three-layer memory**: raw chunks + entity nodes + wiki summaries in single Qdrant collection
- **Auto-chunking**: texts >1000 chars split into ~800 char pieces on sentence boundaries
- **Synthesis queue**: chunks tagged `synthesis_status=pending` for batch processing
- **Night synthesis worker** (`synthesis.py`): LLM extracts entities + relations, builds wiki
- **Entity nodes**: typed entities (technology, person, project, concept) with weighted relations
- **Wiki chunks**: synthesized knowledge stored as searchable vectors + markdown on disk
- **Enriched search**: auto-context prioritizes wiki -> entities -> thread -> global -> experience
- **Graph visualization**: interactive force-directed SVG graph in Knowledge > Graph tab
- **Configurable**: synthesis_enabled, synthesis_time (default 03:00), synthesis_max_per_run

### Agent Improvements
- **`self_config` tool**: agent can read/set any setting or system key (telegram token, etc.)
- **Anti-hedge fix**: only nudge for long inputs (>40 chars), nudge cleaned from history
- **Round limit warning**: model gets "LAST round" before exhaustion
- **tok/s display**: real token count and speed from LLM stream usage

### Infrastructure
- **Telegram auto-start** in uvicorn lifespan (not just run_server)
- **Thinking stripped** from Telegram responses
- **Code review fixes**: VERSION constant, timezone-aware synthesis cron, entity dedup threshold

---

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
