# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For a high-level system map see `ARCHITECTURE.md`. For contributor setup + release flow see `CONTRIBUTING.md`. This file is tuned for what an LLM agent actually needs to know to get work done.

## Build & Run

```bash
./setup.sh            # Linux/Mac — creates .venv, installs deps, pre-loads embeddings
setup.bat             # Windows

python cli.py                                # Terminal chat
python cli.py --web --ssl --port 7861        # Web UI (HTTPS required for mic/camera)
qwe-qwe --web --doctor                       # If installed as package; doctor runs 30+ checks

# Tests
pytest tests/                                # All tests (~495 currently)
pytest tests/test_integration.py -v          # Integration tests (TestClient + mocked LLM)
pytest tests/test_turn_context.py -v         # Per-request state isolation
pytest tests/test_telemetry.py -v            # Privacy contract + consent gates
pytest tests/test_tools.py::test_blocks_sudo # Single test by nodeid
pytest --cov --cov-report=term               # With coverage (floor: 24% — do not regress)

# Lint (all must pass pre-commit & CI)
ruff check .
python scripts/check_js.py                   # node --check on static/index.html inline <script>
python -c "import ast, pathlib
for p in pathlib.Path('.').glob('*.py'):
    ast.parse(p.read_text(encoding='utf-8'), filename=str(p), feature_version=(3,11))"
```

Requires Python 3.11+. LM Studio or Ollama must be running with a loaded model for real LLM turns; integration tests mock the provider.

## Architecture

Single-process Python agent. FastAPI + WebSocket server, SQLite for metadata, Qdrant (local disk) for vectors. No external services required.

### Request lifecycle

```
client (Web WS / Telegram / CLI)
    → server.py builds TurnContext with per-request callbacks + abort_event
    → agent.run(user_input, ctx=...)
        → _build_messages() — soul + auto_context (recall) + history + user turn
        → agent_loop.run_loop() — streaming + tool dispatch + abort check per chunk
            → tools.execute(name, args) — dispatched via TurnContext thread-local
        → emit content/thinking/tool_call to ctx.on_* callbacks
    → server streams WS events back to client
```

### TurnContext (`turn_context.py`) — per-request state isolation

**Added v0.17.25. Critical to know.** Module-level globals (`_content_callback`, `_pending_image_path`, etc.) used to let concurrent Web + Telegram turns stomp each other's state. Now bundled in `TurnContext` dataclass, propagated via `contextvars.ContextVar` (`_current_turn_ctx`):

- `agent.run(..., ctx=...)` — optional; CLI gets a default ctx.
- `_run_inner` sets the ContextVar at the top; `_emit_content` / `_emit_thinking` / etc. read it.
- `agent_loop.run_loop(ctx=...)` extended to take ctx + threads it into `tools._set_turn_ctx(ctx)` (thread-local).
- Blocking tools (`shell`, `http_request`) read `tools._get_abort_event()` from the thread-local ctx and exit early on abort (v0.17.19).
- Back-compat shim: `agent._content_callback = fn` still works but emits a one-shot DeprecationWarning. `_harvest_legacy_slots(ctx)` copies legacy attributes onto the freshly built ctx at each `agent.run()` top.

When adding new per-turn state: put it on TurnContext, not as a module global.

### Agent Loop v2 (`agent_loop.py`)

- **No artificial limits**: `max_turns=0`, `max_tool_calls=0`. Only loop detection (2 identical tool+args signatures → `_force_finish`) stops infinite loops.
- **Tool result clearing**: before each LLM call, old tool results (keeping last 3 intact) become `[cleared — N chars of <tool_name> output]` stubs. **No bytes of original content preserved** (v0.17.18) — a tool that printed a secret can't leak it back via the cleared stub.
- **Tool result cap**: individual results capped at 4000 chars.
- **Text-to-tool extraction**: if model writes `<tool_call>{...}` in prose instead of emitting `delta.tool_calls`, regex extracts and executes. Every extracted call goes through `_pre_dispatch_safety_check` (same gate as native tool calls — shell safety, write_file whitelist).
- **Anti-hedge**: empty reply with only thinking → one nudge as assistant continuation. Never inject `[system]` messages as user role — breaks model flow (lesson from OpenCode).
- **Abort**: checked per streaming chunk + propagated into `shell` / `http_request` via `threading.local`.

### Tool System (`tools.py`)

**Core tools** (29 always-loaded — check with `grep -c '"name":' tools.py`): memory_save, memory_search, memory_delete, read_file, write_file, shell, http_request, spawn_task, tool_search, send_file, camera_capture, open_url, self_config + 6 browser quickstart tools + 10 meta-tools. `tool_search("keyword")` unlocks extended tools (notes, schedule, secret, mcp, profile, rag, skill, soul, timer + 17 more browser tools).

**Shell safety** (`_check_shell_safety`): speed-bump against obvious bypasses (sudo, rm -rf /, eval $(...), $(curl ...) | sh, Cyrillic lookalikes, hex-encoded rm). **NOT a trust boundary** — agent runs with full user privileges. For real isolation, run in a container. Tests live in `tests/test_shell_safety.py`.

**Path resolution** (`_resolve_path`): Git Bash `/c/Users/...` → Windows `C:/Users/...`. Write whitelist: `~/.qwe-qwe/workspace/`, `~/.qwe-qwe/`, cwd.

**send_file**: copies to `uploads/`, queues in `_pending_files`. Server includes in WS reply. Rule 12 in soul.py says "after write_file call send_file".

**Imports**: local `import X` inside function branches ships time bombs (v0.17.7 `subprocess` UnboundLocalError, v0.17.23 rag.py f-string SyntaxError). Hoist to module top; use `importlib.import_module` only for circular-import dodges. Never use `import X as _X` alias just to re-bind a module-level name inside a function.

### Memory (`memory.py`)

**3-way hybrid search** (dense + sparse + BM25 FTS5, fused via RRF) in a single Qdrant collection (`qwe_qwe`):
- **Raw** (`tag=knowledge/fact/user/...`) — immediate saves, auto-chunked >1000 chars.
- **Entity** (`tag=entity`) — graph nodes with typed relations, created by night synthesis.
- **Wiki** (`tag=wiki`) — synthesized summaries, highest-quality recall.

**Session isolation**: thread-scoped raw first, then cross-thread only for synthesized tags. Raw messages from OTHER threads never injected.

**Secret scrubbing** (`_scrub_secrets`, v0.17.18): `memory.save()` redacts OpenAI/Anthropic/Groq/GitHub/AWS/Slack/JWT key shapes + dotenv lines before persistence. If you add a new key format, extend `_SECRET_PATTERNS` + `tests/test_secret_scrub.py`.

**Lazy-init lock**: `_get_qdrant()` + FastEmbed dense/sparse models use double-checked locking (disk-mode Qdrant rejects concurrent folder open). Same pattern in `rag.py::_get_markitdown`.

**Structured compaction**: when context fills (`context_budget`, default 24000), LLM creates 9-section summary (Current State / Goals / Key Files / Learnings / Next Steps / ...) injected back as conversation message.

### SQLite migrations (`migrations/`)

**Added v0.17.26.** Versioned SQL files applied in order. `schema_version` in kv tracks latest applied.

- `001_initial.sql` — baseline (messages, kv, presets, threads, scheduled_tasks, secrets, FTS5 virtual tables).
- `002_message_thread_ts_index.sql` — composite index example.
- `migrations/README.md` — convention: `NNN_snake_case.sql`, transaction-per-file.

`db._apply_migrations()` runs on first connection. Back-compat: if `schema_version` missing AND `messages` table exists, stamp at 1 without re-running baseline. Add a new migration by dropping a file — no code change needed.

### System Prompt (`soul.py`)

`to_prompt()` order matters for KV cache — static rules first, dynamic context last. Key rules:
- **Rule 3** NEVER STOP EARLY — keep calling tools until all steps complete
- **Rule 6** BROWSER MODES — `browser_open` = headless, `open_url` = show user, `browser_set_visible(true)` + browser tools = interact visibly
- **Rule 8** MEMORY DISCIPLINE (v0.17.12) — default is DON'T save; only durable facts that matter weeks later
- **Rule 11** Brave Search for web (Google/DuckDuckGo block headless)
- **Rule 12** After `write_file`, call `send_file`
- **Rule 14** NEW INTEGRATION (v0.18.x) — for service integrations (Gmail, Slack, custom skills) call `create_skill`, never `write_file` in skills/. After `create_skill` invocation: **STOP the turn**, don't run more tools — pipeline is async, notification fires later. Skills are single .py files, never directories.
- **Rule 16** EXTERNAL-WAIT (v0.18.x) — for browser OAuth / 2FA / hardware-key / email-confirm flows: don't run blocking commands (shell tool times out at 120s). Use `--no-launch-browser` / `--device-code` flags, surface URL via `open_url`, end the turn, resume on user's next message.

### Providers (`providers.py`)

OpenAI-compatible client for 10 providers (lmstudio, ollama, openai, openrouter, groq, together, deepseek + perplexity / cerebras / mistral added in v0.17.33). `list_all()` pings local providers in parallel (1s timeout, 30s cache). `detect_context_length()` probes LM Studio `/api/v0/models` or Ollama `/api/show` to discover the real context window (displayed in the Web UI Context Window gauge — denominator is `context_budget`, shown alongside the detected model_context).

### Skill Creator (`skills/skill_creator.py`)

User can chat-create new skills: "build me a meal logger that takes a photo and remembers what I ate" → `tool_search("skill")` → `create_skill(name, description)` → 5-step LLM pipeline writes a `.py` to `~/.qwe-qwe/skills/<name>.py`.

Pipeline phases (`_run_pipeline`, runs in background thread, 3 retry attempts):
1. **Plan** — JSON of `{docstring, instruction, tables, tools}`. Plan prompt instructs the planner to compose with the full agent runtime (memory.save, tools.execute("camera_capture"), secrets, http_request).
2. **Tool definitions** — JSON array of OpenAI function schemas.
3. **Mapping + assembly** — `_assemble_from_mapping()` recognises CRUD ops (add/list/delete/update/get/stats) and emits Python from templates without an LLM call. Custom ops fall through to a STEP3_CODE LLM call. **First branch must be `if`, not `elif`** when execute_body is empty — `_run_pipeline` instructs the LLM accordingly AND post-processes the output via regex (defense-in-depth, fixed in v0.18.3).
4. **Table DDL** — `_build_table_ddl(plan)`. **Tables MUST be prefixed `skill_<name>_*`** to avoid collisions with core agent tables (messages, kv, threads) and other skills' tables. Documented in INSTRUCTION + STEP1_PLAN, enforced by tests.
5. **Validate + smoke** — ast.parse, then `validate_skill()`, then `_smoke_test()` calls `execute()` for each declared tool. **Param-usage check (v0.18.4)** scopes search to `execute()` body via AST, no longer false-matches param names appearing in the TOOLS dict literal.

Soul rule 14 (v0.18.x): when user requests a service integration, agent calls `create_skill` and **STOPS the turn** — no parallel `write_file` to skills/, no manual scaffolding. Skills are SINGLE `.py` files at `~/.qwe-qwe/skills/<name>.py`, never directories.

`delete_skill(name)` parses CREATE TABLE statements, drops only `skill_<name>_*` matches via `_extract_skill_owned_tables` (regex with isidentifier guard), then unlinks the .py.

### Telemetry (`telemetry.py`) — opt-in, anonymous, audit-friendly

**Default OFF.** No data leaves the machine until the user explicitly opts in via the first-run modal (web) / TTY prompt (CLI) / Settings → Privacy → Telemetry.

Privacy contract (enforced at `track_event()`):
- 5 whitelisted events (`session_start`, `turn_complete`, `tool_error`, `skill_creator_pipeline`, `feature_first_use`) with type-strict prop schemas.
- String props that could carry free text use closed enums (`TOOL_CATEGORIES`, `ERROR_KINDS`, `SOURCES`, `PROVIDER_KINDS`, `MODEL_SIZE_BUCKETS`, `PIPELINE_OUTCOMES`, `FEATURES`). A future refactor adding a string field can't smuggle chat content past the validator.
- Anonymous_id is a random UUID generated on first opt-in. Not derived from any PII. `forget_me()` wipes; `reset_anonymous_id()` rotates without disabling.
- Two consent gates (`track_event` + `flush`) refuse to accept / send when `consent_needs_reprompt()` is True.

Wire formats (`telemetry.py::_default_sender` dispatches by `telemetry_format`):
- `raw` — single batched POST `{"events": [...]}` for custom collectors.
- `countly` — batched POST to Countly's `/i` with `device_id = anonymous_id` (cross-day per-user tracking works natively, unlike Plausible's daily-rotating salt). Lists become CSV strings; `duration_ms` becomes Countly `dur` (seconds).

Project defaults ship the Countly path pointing at `https://qwelytics.deepfounder.ai/i` with the project's public Countly app key. End-user UI surfaces only Enable/Disable + transparency lists — endpoint / format / app_key are NOT user-editable (operators / forks edit `config.py` defaults).

Consent versioning (`_CURRENT_CONSENT_VERSION` constant in `telemetry.py`): bump when `ALLOWED_EVENTS` shape changes OR default endpoint changes. Old consent → "policy updated, please re-confirm" banner; events queue but don't send until user re-stamps via opt_in.

Adding a new event: edit `ALLOWED_EVENTS` schema + bump `_CURRENT_CONSENT_VERSION`. Audit by grep `telemetry.track_event` — only path into the queue.

Full data inventory + privacy contract: `docs/PRIVACY.md`.

### Voice / Camera / Knowledge ingest

- **STT** (`stt.py`): auto/local/api; local = faster-whisper on CPU; API = OpenAI-compatible (Groq free tier works). PyAV fallback when ffmpeg missing.
- **TTS** (`tts.py`): auto-detects API style (OpenAI `/v1/audio/speech`, custom `/tts` w/ voice cloning, Fish Speech, s2.cpp).
- **Camera**: `camera_capture(prompt?)` grabs frame via WebSocket (browser) or OpenCV (direct). Persistent `_camera_cap` for fast repeat captures. WS event names unified `get_frame` / `frame_request` (v0.18.2). Client falls back to one-shot `getUserMedia` when PiP isn't active. OpenCV path has black-frame guard (mean<25 → up to 30 retries, sensor warmup) for Windows DirectShow gotchas. Auto-detect picks BRIGHTEST of indexes 0-3, not first non-pitch-black. Resolution + JPEG quality user-tunable via `camera_resolution` (auto/480p/720p/1080p) + `camera_quality` (1-100) settings.
- **URL/file indexing** (`rag.py`): MarkItDown handles PDF/DOCX/PPTX/XLSX/HTML/etc. YouTube-specific path uses `yt-dlp` with `player_client=["android","ios","web"]` to dodge DRM blocks, native-language preferred over auto-translated English.
- **SSRF** (v0.17.18): `/api/knowledge/url` blocks private/loopback/link-local IPs unless `QWE_ALLOW_PRIVATE_URLS=1`. Uses `socket.getaddrinfo` + `ipaddress.ip_address.is_private`.

### Web UI (`static/index.html`)

Single-file SPA, ~5500 lines of vanilla JS. **No build step** — `scripts/check_js.py` runs `node --check` on the extracted `<script>` at pre-commit + CI.

- Telegram-style pill composer; three-dot menu on mobile.
- Live Voice Mode: VAD → STT → LLM → TTS → auto-listen.
- Camera PiP overlay with capture-on-send.
- Inspector: Context Window gauge (`prompt_tokens / context_budget` — agent-side limit, NOT model context), Recalled memories (from `_emit_recall` via WS, shows `source: thread/wiki/entity/...`), Active tools, Latency bars.
- Knowledge Graph: force-directed layout + pan/zoom/drag (v0.17.11).
- Provider picker with `NEEDS KEY` badges (v0.17.17) + key modal with built-in URL hints per provider.

Render pattern: every state change rebuilds innerHTML. Event handlers attached via `wireEvents()` on each render. Globals attached ONCE (guard: `state._graphGlobalHandlersAttached = true`).

## CI + release pipeline (`.github/workflows/`)

`test.yml` runs on every push/PR:
1. `ruff check .` — lint
2. `ast.parse(... feature_version=(3, 11))` on every `.py` — catches PEP 701 leakage (3.12-only f-string escapes etc.)
3. `python scripts/check_js.py` — JS syntax in `static/index.html`
4. Import-time smoke — `python -c "import agent, server, rag, tools, ...; from server import app"` — surfaces runtime SyntaxError / ImportError that pytest doesn't touch
5. `pytest tests/ -v --cov` with `fail_under=24` in `pyproject.toml`

`release.yml` (v0.17.27) triggers on `workflow_run: Tests completed success`:
- Reads VERSION from `config.py`, verifies `pyproject.toml` matches
- If `v$VERSION` tag doesn't exist, creates tag + `gh release create` with `RELEASE_NOTES.md` body
- Idempotent — duplicate triggers are no-ops

**To release**: bump `VERSION` in `config.py` + `pyproject.toml` + README badge, write `RELEASE_NOTES.md`, commit, push to `main`. Workflow handles the rest.

## Key patterns + gotchas

- **Shell on Windows** = Git Bash (`_detect_shell()` auto-routes); always write UNIX shell in docs/prompts.
- **`_resolve_path`** handles Git Bash → Windows path conversion + write whitelist.
- **SafeConsole** wraps Rich to catch cp1251 encoding errors on Windows terminals. Avoid unicode emoji in code paths that print to CLI (doctor, setup).
- **Gemma support**: strips `<|channel>thought` tags from streaming + responses.
- **Self-check**: validates tool args before `shell` / `write_file`; `_pre_dispatch_safety_check` applies to BOTH native tool calls AND text-extracted ones.
- **Shared utilities**: `utils.py` — `strip_thinking()`, `extract_thinking()`. Single canonical implementation — imported by agent.py, agent_loop.py, tasks.py.
- **Preset isolation**: activating a preset atomically swaps thread + workspace + active skills + knowledge-tag filter. Deactivating restores originals.
- **Visible browser**: `browser_set_visible(true)` launches Playwright with `headless=False`; all 23 browser tools work on the visible window.
- **Warning suppression**: FastEmbed pooling + Qdrant local index warnings suppressed via `warnings.catch_warnings()`.

## Tests (`tests/`)

All ~495 tests run in a single pytest process (v0.17.24 — no more sys.modules pollution). Do NOT add `sys.modules[...] = mock_X` at module scope — use `monkeypatch` fixtures (see `tests/conftest.py` for `qwe_temp_data_dir`, `mock_llm`).

Notable files:
- `test_integration.py` — TestClient + mocked LLM end-to-end (added to catch v0.17.23-style lazy-import SyntaxErrors)
- `test_turn_context.py` — cross-source callback isolation (web vs telegram)
- `test_shell_safety.py` — obfuscation bypass catalog (39 cases)
- `test_secret_scrub.py` — regex patterns for API key shapes
- `test_migrations.py` — fresh + back-compat + idempotent + rollback
- `test_skill_creator_smoke.py` + `test_skill_creator_pipeline.py` — pure helpers + e2e pipeline with mocked LLM (camera-using skill regression test)
- `test_telemetry.py` — privacy contract + Countly + raw + consent gates (~58 tests)
- `test_text_to_tool_extraction.py` — all 5 patterns including the `!<function_call:>` Qwen variant (#10)
- `test_camera_settings.py` — preset table + helper, `pytest.importorskip("cv2")` for CI
- `conftest.py` — shared fixtures; `scope="session"` TestClient lives here

Coverage baseline 25.93%; floor 24% (`pyproject.toml::tool.coverage.report.fail_under`). Some modules under 10% (`cli.py`, `inference_setup.py`, `synthesis.py`) are candidates for future integration tests.

CI flake to know about: `tests/test_telemetry_wireup.py::test_tool_error_classifies_keyboard_interrupt_as_aborted` sometimes fails in full-suite collection (KeyboardInterrupt is special-cased by pytest); always passes in isolation.

## Data layout (`~/.qwe-qwe/` — override via `QWE_DATA_DIR`)

- `qwe_qwe.db` — SQLite (messages, threads, kv, settings, cron, secrets)
- `memory/` — Qdrant vectors (disk mode)
- `wiki/` — synthesized markdown pages
- `skills/` — user-dropped `.py` skills
- `uploads/` — images, docs, camera captures, TTS mp3s (startup sweep deletes files >14 days old; `uploads/kb/` kept — indexed knowledge sources)
- `workspace/` — default CWD for relative paths (swapped when preset active)
- `presets/<id>/` — installed presets (each with own `workspace/`, `knowledge/`, `skills/`)
- `logs/` — qwe-qwe.log (INFO+), errors.log (WARNING+)

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QWE_LLM_URL` | `http://localhost:1234/v1` | Provider base URL |
| `QWE_LLM_MODEL` | `qwen/qwen3.5-9b` | Active model id |
| `QWE_LLM_KEY` | `lm-studio` | API key |
| `QWE_DATA_DIR` | `~/.qwe-qwe` | Where state lives |
| `QWE_DB_PATH` | `$DATA_DIR/qwe_qwe.db` | SQLite path |
| `QWE_QDRANT_MODE` | `disk` | `memory` / `disk` / `server` |
| `QWE_PASSWORD` | — | Web UI auth (when exposing on LAN) |
| `QWE_STT_DEVICE` | `cpu` | faster-whisper device |
| `QWE_EMBED_DEVICE` | `cpu` | FastEmbed provider — CPU by design (v0.17.21). Set `cuda` only if you've installed `onnxruntime-gpu` + matching CUDA Toolkit manually. |
| `QWE_ALLOW_PRIVATE_URLS` | unset | Set to `1` to bypass SSRF block on `/api/knowledge/url` (dev only). |

Telemetry-related settings live in `EDITABLE_SETTINGS` (not env vars): `telemetry_enabled` (default 0), `telemetry_endpoint` (default `https://qwelytics.deepfounder.ai/i`), `telemetry_format` (`raw` / `countly`, default `countly`), `telemetry_countly_app_key`, `telemetry_anonymous_id`, `telemetry_consent_version`. Operators / forks edit defaults in `config.py`; end-users only see Enable/Disable.

## When adding a feature — quick checklist

1. **New .py module?** Add to `[tool.setuptools] py-modules` in `pyproject.toml` (else `pip install -e .` crashes on import for downstream installs).
2. **New tool?** Add to `tools.TOOLS` list + branch in `tools.execute()`. If it takes a `path` arg, call `_get_path_arg(args)` (models use various field names). If it's dangerous (writes, shells), it must pass `_pre_dispatch_safety_check`. Also map the new tool name to a category in `tools.TOOL_CATEGORIES_BY_NAME` so telemetry events bucket correctly.
3. **New per-turn state?** Put on `TurnContext`, not as a module global.
4. **New setting?** Add to `EDITABLE_SETTINGS` in `config.py` with `(kv_key, type, default, desc, min, max)`. `config.get("foo")` reads with defaults.
5. **New schema change?** New file `migrations/NNN_snake_case.sql`. `_apply_migrations()` picks it up.
6. **New doctor check?** Add to `cli.py:doctor()`. Must survive cp1251 terminals — no raw emoji in output.
7. **New WS event?** Emit via `ctx.on_*` callback, not a global. Client reads in `handleWSMessage` in `static/index.html`. If event is non-chat (notification / status / etc.), short-circuit at the top of `handleWsMessage` BEFORE the `state.streaming` creation gate — otherwise it triggers a ghost streaming message in the chat (lesson from `task_update` bug, fixed in v0.18.3).
8. **New telemetry event?** Add to `telemetry.ALLOWED_EVENTS` whitelist with type-strict prop schema. String props that could carry free text MUST use a closed enum. Bump `telemetry._CURRENT_CONSENT_VERSION` so existing opted-in users get a re-consent banner.
9. **Skills are gitignored except whitelisted.** When working on built-in skills (`skills/skill_creator.py` etc.), `.gitignore` entry `skills/` excludes them by default; whitelist (`!skills/skill_creator.py`) keeps the built-ins tracked. Side effect: `ruff check .` skips skills/ via gitignore — for those files run `ruff check skills/` explicitly.
10. **Before commit**: `ruff check .`, `python scripts/check_js.py` (if you touched `static/index.html`), `pytest tests/`.
