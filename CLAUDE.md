# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For a high-level system map see `ARCHITECTURE.md`. For contributor setup + release flow see `CONTRIBUTING.md`. This file is tuned for what an LLM agent actually needs to know to get work done.

## Recommended Claude Code skills

Castor is itself an agent harness — when designing or auditing castor's own
runtime (Phase 3+ of `docs/superpowers/plans/2026-05-15-long-running-agent-architecture.md`),
load the provider-neutral agentic-harness reference manual:

```bash
mkdir -p .claude/skills
git clone https://github.com/DenisSergeevitch/agents-best-practices.git \
  .claude/skills/agents-best-practices
```

That skill's 15 reference files (`agentic-loop.md`, `planning-and-goals.md`,
`tools-and-permissions.md`, `context-memory-compaction.md`, etc.) map almost
1:1 to castor's `agent_loop.py` / `orchestrator.py` / `subagent.py` modules
and use a shared vocabulary (loop invariants, agent legibility, planning
mode artifacts) that keeps internal docs consistent.

The `.claude/` directory is gitignored — skills install per-clone.

## Build & Run

```bash
./setup.sh            # Linux/Mac — creates .venv, installs deps, pre-loads embeddings
setup.bat             # Windows

python cli.py                                # Terminal chat
python cli.py --web --ssl --port 7861        # Web UI (HTTPS required for mic/camera)
castor --web --doctor                       # If installed as package; doctor runs 30+ checks
python -m worker                             # Goal worker daemon (long-running tasks)
python -m worker --once                      # Claim one goal, run it, exit (for tests)

# Optional: native Anthropic SDK (prompt caching + thinking budgets)
pip install 'castor[anthropic_native]'       # Activates providers_anthropic.AnthropicNativeClient
                                              # when provider=anthropic + ANTHROPIC_API_KEY set

# Tests
pytest tests/                                # All tests (~1337 currently)
pytest tests/test_integration.py -v          # Integration tests (TestClient + mocked LLM)
pytest tests/test_turn_context.py -v         # Per-request state isolation
pytest tests/test_telemetry.py -v            # Privacy contract + consent gates (74 tests)
pytest tests/test_blog_feed.py -v            # Blog RSS proxy in Presets view
pytest tests/test_ws_attachments.py -v       # WS image/document round-trip + reload contract
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

- **No artificial limits**: `max_turns=0`, `max_tool_calls=0`. Multi-period loop detection (period 1, 2, or 3 repetitions in the last 6 tool-call signatures → `_force_finish`) stops infinite loops. Helper: `_detect_loop_period(sigs)`.
- **Execute-level tool whitelist**: `run_loop(allowed_tools=...)` enforces a hard gate at `_run_tool()` — if a tool name isn't in the set, it returns an error string without executing. Subagents and orchestrator both pass their restricted sets through this gate, so even text-extracted tool calls can't escape the whitelist.
- **Tool result clearing**: before each LLM call, old tool results (keeping last 3 intact) become `[cleared — N chars of <tool_name> output]` stubs. **No bytes of original content preserved** (v0.17.18) — a tool that printed a secret can't leak it back via the cleared stub.
- **Tool result cap**: individual results capped at 4000 chars.
- **Text-to-tool extraction**: if model writes `<tool_call>{...}` in prose instead of emitting `delta.tool_calls`, regex extracts and executes. Every extracted call goes through `_pre_dispatch_safety_check` (same gate as native tool calls — shell safety, write_file whitelist).
- **Anti-hedge**: empty reply with only thinking → one nudge as assistant continuation. Never inject `[system]` messages as user role — breaks model flow (lesson from OpenCode).
- **Abort**: checked per streaming chunk + propagated into `shell` / `http_request` via `threading.local`.

### Tool System (`tools.py`)

**Core tools** (29 always-loaded — check with `grep -c '"name":' tools.py`): memory_save, memory_search, memory_delete, read_file, write_file, shell, http_request, spawn_task, tool_search, send_file, camera_capture, open_url, self_config + 6 browser quickstart tools + 10 meta-tools. `tool_search("keyword")` unlocks extended tools (notes, schedule, secret, mcp, profile, rag, skill, soul, timer + 17 more browser tools).

**`tool_search` activations persist per-thread** (v0.23.x): once a tool is activated within a thread via `tool_search` (or by `tool_search`-equivalent slash command), it stays active across subsequent turns. Storage: kv key `thread_active_tools_<tid>` (JSON array). `_load_active_tools_for_thread(tid)` runs at the top of every `agent.run` to restore the set; `_do_tool_search` persists new additions immediately. The legacy `_reset_active_tools()` is a compat-shim that now LOADS instead of CLEARING — explicit wipe via `_reset_active_tools_for_thread(tid)`. The win: tools array sent to the LLM stays stable across turns, prerequisite for Anthropic prompt-cache hits (tools list is part of the cached prefix).

**Shell safety** (`_check_shell_safety`): speed-bump against obvious bypasses (sudo, rm -rf /, eval $(...), $(curl ...) | sh, Cyrillic lookalikes, hex-encoded rm). **NOT a trust boundary** — agent runs with full user privileges. For real isolation, run in a container. Tests live in `tests/test_shell_safety.py`.

**Path resolution** (`_resolve_path`): Git Bash `/c/Users/...` → Windows `C:/Users/...`. Write whitelist: `~/.castor/workspace/`, `~/.castor/`, cwd.

**send_file**: copies to `uploads/`, queues in `_pending_files`. Server includes in WS reply. Rule 12 in soul.py says "after write_file call send_file".

**Imports**: local `import X` inside function branches ships time bombs (v0.17.7 `subprocess` UnboundLocalError, v0.17.23 rag.py f-string SyntaxError). Hoist to module top; use `importlib.import_module` only for circular-import dodges. Never use `import X as _X` alias just to re-bind a module-level name inside a function.

### Memory (`memory.py`)

**3-way hybrid search** (dense + sparse + BM25 FTS5, fused via RRF) in a single Qdrant collection (`castor`):
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
- `006_canvas_artifacts.sql` — canvas HTML persistence.
- `007_skill_imports.sql` — imported skill provenance.
- `008_agent_runs.sql` — per-turn cost tracking (replaces legacy `routine_runs`).
- `009_interrupted_runs.sql` — auto-resume support.
- `010_routine_budget.sql` — per-routine spending caps.
- `011_goals_subtasks_checkpoints.sql` — durable goal queue, subtask plan, orchestrator checkpoints + event log.
- `012_goal_facts.sql` — per-goal structured fact store (key/value scoped by goal_id).
- `013_goal_outputs.sql` — per-goal deliverables (file/link/report).
- `014_goal_done_conditions.sql` — **goal-level** acceptance criteria column (`goals.done_conditions TEXT` as JSON array). Per-subtask criteria live inside the existing `goals.plan` JSON column (no separate migration). Both validated by the 5 kinds in `goal_validators.py`: files_exist / min_count / regex_in_file / shell_returns_zero / http_200.
- `migrations/README.md` — convention: `NNN_snake_case.sql`, transaction-per-file.

`db._apply_migrations()` runs on first connection. Back-compat: if `schema_version` missing AND `messages` table exists, stamp at 1 without re-running baseline. Add a new migration by dropping a file — no code change needed.

### System Prompts (`soul.py` + `prompts/`)

Interactive chat uses `soul.py::to_prompt()` — order matters for KV cache (static rules first, dynamic context last). Goal orchestration and subagents use dedicated markdown prompts in `prompts/` (`orchestrator.md`, `subagent_research.md`, `subagent_browser.md`, `subagent_code.md`, `subagent_scraper.md`).

Key soul.py rules:
- **Rule 3** NEVER STOP EARLY — keep calling tools until all steps complete
- **Rule 6** BROWSER MODES — `browser_open` = headless, `open_url` = show user, `browser_set_visible(true)` + browser tools = interact visibly
- **Rule 8** MEMORY DISCIPLINE (v0.17.12) — default is DON'T save; only durable facts that matter weeks later
- **Rule 11** Brave Search for web (Google/DuckDuckGo block headless)
- **Rule 12** After `write_file`, call `send_file`
- **Rule 14** NEW INTEGRATION (v0.18.x) — for service integrations (Gmail, Slack, custom skills) call `create_skill`, never `write_file` in skills/. After `create_skill` invocation: **STOP the turn**, don't run more tools — pipeline is async, notification fires later. Skills are single .py files, never directories.
- **Rule 16** EXTERNAL-WAIT (v0.18.x) — for browser OAuth / 2FA / hardware-key / email-confirm flows: don't run blocking commands (shell tool times out at 120s). Use `--no-launch-browser` / `--device-code` flags, surface URL via `open_url`, end the turn, resume on user's next message.

### Providers (`providers.py`)

OpenAI-compatible client for 10 providers (lmstudio, ollama, openai, openrouter, groq, together, deepseek + perplexity / cerebras / mistral added in v0.17.33). `list_all()` pings local providers in parallel (1s timeout, 30s cache). `detect_context_length()` probes LM Studio `/api/v0/models` or Ollama `/api/show` to discover the real context window (displayed in the Web UI Context Window gauge — denominator is `context_budget`, shown alongside the detected model_context).

**Native Anthropic adapter (v0.23.x)** — when provider=`anthropic` + `ANTHROPIC_API_KEY` set, `get_client()` returns `AnthropicNativeClient` (in `providers_anthropic.py`) instead of the OpenAI-compatible wrapper. Unlocks **prompt caching** (50-90% cost reduction on cached prefixes) and **thinking budgets** for Sonnet 4.6+ / Opus 4. Modules: `providers_anthropic_convert.py` (request/response translation), `providers_anthropic_stream.py` (SSE → OpenAI-shape duck-typed chunks via `@dataclass`-based `_Chunk` / `_Choice` / `_Delta`), `providers_anthropic.py` (client + routing). Adapter is fully duck-typed against `OpenAI().chat.completions.create()`, so `agent_loop` never sees the difference. Falls back to OpenAI-compatible client when `anthropic` SDK absent or key missing. Optional via `pip install 'castor[anthropic_native]'` extra. End-to-end opt-in for OpenRouter Anthropic models via `setting:anthropic_native_routing=1`.

### Skill Creator (`skills/skill_creator.py`)

User can chat-create new skills: "build me a meal logger that takes a photo and remembers what I ate" → `tool_search("skill")` → `create_skill(name, description)` → 5-step LLM pipeline writes a `.py` to `~/.castor/skills/<name>.py`.

Pipeline phases (`_run_pipeline`, runs in background thread, 3 retry attempts):
1. **Plan** — JSON of `{docstring, instruction, tables, tools}`. Plan prompt instructs the planner to compose with the full agent runtime (memory.save, tools.execute("camera_capture"), secrets, http_request).
2. **Tool definitions** — JSON array of OpenAI function schemas.
3. **Mapping + assembly** — `_assemble_from_mapping()` recognises CRUD ops (add/list/delete/update/get/stats) and emits Python from templates without an LLM call. Custom ops fall through to a STEP3_CODE LLM call. **First branch must be `if`, not `elif`** when execute_body is empty — `_run_pipeline` instructs the LLM accordingly AND post-processes the output via regex (defense-in-depth, fixed in v0.18.3).
4. **Table DDL** — `_build_table_ddl(plan)`. **Tables MUST be prefixed `skill_<name>_*`** to avoid collisions with core agent tables (messages, kv, threads) and other skills' tables. Documented in INSTRUCTION + STEP1_PLAN, enforced by tests.
5. **Validate + smoke** — ast.parse, then `validate_skill()`, then `_smoke_test()` calls `execute()` for each declared tool. **Param-usage check (v0.18.4)** scopes search to `execute()` body via AST, no longer false-matches param names appearing in the TOOLS dict literal.

Soul rule 14 (v0.18.x): when user requests a service integration, agent calls `create_skill` and **STOPS the turn** — no parallel `write_file` to skills/, no manual scaffolding. Skills are SINGLE `.py` files at `~/.castor/skills/<name>.py`, never directories.

`delete_skill(name)` parses CREATE TABLE statements, drops only `skill_<name>_*` matches via `_extract_skill_owned_tables` (regex with isidentifier guard), then unlinks the .py.

### Telemetry (`telemetry.py`) — opt-in, anonymous, audit-friendly

**Default OFF.** No data leaves the machine until the user explicitly opts in via the first-run modal (web) / TTY prompt (CLI) / Settings → Privacy → Telemetry.

Privacy contract (enforced at `track_event()`):
- 6 whitelisted events (`session_start`, `turn_complete`, `tool_error`, `skill_creator_pipeline`, `feature_first_use`, `thread_created`) with type-strict prop schemas.
- String props that could carry free text use closed enums (`TOOL_CATEGORIES`, `ERROR_KINDS`, `SOURCES`, `PROVIDER_KINDS`, `MODEL_SIZE_BUCKETS`, `PIPELINE_OUTCOMES`, `FEATURES`). `SOURCES` widened to include `"preset"` in v0.18.5. A future refactor adding a string field can't smuggle chat content past the validator.
- Anonymous_id is a random UUID generated on first opt-in. Not derived from any PII. `forget_me()` wipes; `reset_anonymous_id()` rotates without disabling.
- Two consent gates (`track_event` + `flush`) refuse to accept / send when `consent_needs_reprompt()` is True.
- `thread_created` (added v0.18.5) emits once per `threads.create()` call — single `source` enum field (web/cli/telegram/scheduler/preset/other). Helper in `threads.py` is lazy-imported and swallows any error so a telemetry hiccup never breaks thread creation. Wired at 6 production call sites — never the thread name or meta.

Wire formats (`telemetry.py::_default_sender` dispatches by `telemetry_format`):
- `raw` — single batched POST `{"events": [...]}` for custom collectors.
- `countly` — batched POST to Countly's `/i` with `device_id = anonymous_id` (cross-day per-user tracking works natively, unlike Plausible's daily-rotating salt). Lists become CSV strings; `duration_ms` becomes Countly `dur` (seconds).

Project defaults ship the Countly path pointing at `https://qwelytics.deepfounder.ai/i` with the project's public Countly app key. End-user UI surfaces only Enable/Disable + transparency lists — endpoint / format / app_key are NOT user-editable (operators / forks edit `config.py` defaults).

Consent versioning (`_CURRENT_CONSENT_VERSION` constant in `telemetry.py`, currently `5`): bump when `ALLOWED_EVENTS` shape changes OR default endpoint changes. Old consent → "policy updated, please re-confirm" banner; events queue but don't send until user re-stamps via opt_in. v4→v5 was bumped when `text_extractions` field was added to `turn_complete`.

Adding a new event: edit `ALLOWED_EVENTS` schema + bump `_CURRENT_CONSENT_VERSION`. Audit by grep `telemetry.track_event` — only path into the queue.

Full data inventory + privacy contract: `docs/PRIVACY.md`.

### Cost tracking (`pricing.py`, `agent_runs` table)

**Added v0.19.0.** Every LLM call site records one row in `agent_runs` (migration `008_agent_runs.sql`, which atomically replaces the legacy `routine_runs` table). Columns: `thread_id`, `source`, `started_at`, `finished_at`, `model`, `provider`, `input_tokens`, `output_tokens`, `cost_usd`, `status`.

**Instrumentation points** (each wraps its LLM call in `db.insert_agent_run` / `db.finalize_agent_run`):
- `agent_loop.run_loop()` — main user turns (source = `web` / `cli` / `telegram` / `scheduler`)
- `synthesis.run_synthesis()` — night entity/wiki extraction (source = `synthesis`)
- `skills/skill_creator.py::_run_pipeline()` — each pipeline attempt (source = `skill_creator`)
- `scheduler` routine firings — same `run_loop` bracket, source = `scheduler`

**Pricing** (`pricing.py`):
- Primary: LiteLLM community JSON (`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`), fetched and cached locally at `~/.castor/pricing_cache.json`.
- Fallback: bundled top-10 model table used when cache is absent or stale.
- Override: `db.kv_set("pricing_override_<model>", json.dumps({"input": X, "output": Y}))` pins exact per-token USD rates. Configurable via Settings UI or `pricing_url` setting for air-gapped mirrors.

**Web UI surfaces**: Sessions list token/cost chips, per-thread run drilldown modal, topline 30-day widget, Routines page Cost (30d) column, Settings → Cost tracking section.

**API additions**: `GET /api/threads` extended with aggregate token/cost fields; `GET /api/threads/{id}/runs`; `GET /api/analytics/period`; `GET /api/pricing/status`; `POST /api/pricing/refresh`.

User-facing doc: `docs/COST_TRACKING.md`.

### Auto-resume after interrupt

Every abort (WS disconnect, Stop button, server crash) is recoverable. Migration 009 added `resumed_from_run_id` + `dismissed_at` to `agent_runs`. The existing agent_loop `finally:` block was extended to flush partial assistant content into `messages` with `meta.interrupted=true` (and `run_id` linking back to the aborted `agent_runs` row). A startup hook in `server.py` promotes any orphaned `running` rows to `aborted` so crashes don't leave zombies.

`agent.resume_interrupted_run(run_id)` is the universal executor. It validates the run is resumable (not dismissed, not itself a resume, not already resumed), builds a TurnContext carrying the original source/cron_id, and fires a normal `agent.run` with a one-shot `system_note=` — a real `{role: "system"}` message that prepends the next LLM call only. **Do NOT inject `[system]` prefixes as user-role messages** (CLAUDE.md OpenCode lesson) — the `system_note` parameter is the clean alternative.

Trigger paths per source:
- **Web**: WS connect emits `interrupted_turn` event; UI banner shows Resume/Dismiss
- **Telegram**: `/resume` command, scoped by `source='telegram'`
- **Routine**: `scheduler.detect_missed_runs` auto-fires aborted routine runs within `resume_ttl_routine_sec` (default 5 min)
- **CLI**: no resume (Ctrl+C is intentional)

TTLs live in `EDITABLE_SETTINGS` — see `docs/AUTO_RESUME.md` for the user-facing guide.

### Routine budget caps (v0.21.0)

Per-routine USD spending caps over a configurable rolling window. Migration `010_routine_budget.sql` adds `budget_usd_cap` (NULL = no cap) and `budget_period_sec` (default 86400) to `scheduled_tasks`.

- `db.get_routine_budget(cron_id)` — returns `{"cap": float, "period_sec": int}` or `None`.
- `db.get_routine_period_spend(cron_id, period_sec)` — sums `agent_runs.cost_usd` over the window. NULL costs treated as 0 (local/unknown-price models never hit caps).
- `scheduler._execute_routine` checks budget **after** acquiring the fire lock (atomic w.r.t. concurrent fires). On cap exceeded: calls `db.insert_skipped_run(..., reason="skipped")` then sets `error='budget_exceeded'` on that row, releases the lock, and returns without running `agent.run`.
- API: `GET /api/routines/{id}/budget` returns `{cap, period_sec, spent}`; `POST /api/routines/{id}/budget` sets or clears the cap. `cap=null` disables enforcement.
- UI: Routines page shows a color-coded budget chip per routine (green < 80%, orange 80–99%, red >= 100%). Click opens a `prompt()` dialog to set/clear cap + period. `loadRoutineBudgets()` runs alongside `loadRoutineCosts()` on every non-silent Routines view load.

### Goal Runtime — long-running autonomous tasks (v0.22.0)

**Architecture**: "Goal → Plan → Subagent dispatch" — inspired by Claude Code's `/goal` mode. A separate `castor-worker` daemon claims goals from a durable SQLite queue and executes them, surviving WS disconnects, process restarts, and context-window pressure. Full design doc: `docs/superpowers/plans/2026-05-15-long-running-agent-architecture.md`.

**New modules:**
- **`worker.py`** — standalone daemon (`python -m worker`, or `--once` for tests). Polls `goals` table, claims runnable goals via lease (`worker_id + lease_expires_at`), heartbeats throughout. Intentionally does NOT import `server.py` / FastAPI — can run in minimal containers. Identity: `hostname_pid_uuid6`. Lease 60s, heartbeat 20s.
- **`goal_runner.py`** — bridges asyncio worker loop to orchestrator. Loads last checkpoint, invokes orchestrator, marks goals done/paused/failed. **Acceptance gate** (v0.22.1): after every orchestrator return, runs `goal_validators.run_validator()` over each subtask's `done_condition`. Failures inject a remediation `system_note` and re-enter the orchestrator (up to `MAX_GATE_ATTEMPTS=3` rounds). Exhaustion → `mark_goal_failed`.
- **`goal_validators.py`** — 5 validator kinds: `files_exist`, `min_count`, `regex_in_file`, `shell_returns_zero`, `http_200`. Stdlib only (no requests/httpx). `run_validator()` never raises — all failures become `(False, "<diagnostic>")`.
- **`orchestrator.py`** — the main LLM for goals. Uses `prompts/orchestrator.md` (NOT `soul.py`). Restricted tool set: `goal_plan_set`, `subtask_update`, `dispatch_subagent`, `fact_save`, `fact_get`, `memory_save`, `memory_search`, `http_request`, basic tools. Manages a linear `subtasks` plan (same model as Claude Code's TodoWrite).
- **`subagent.py`** — fresh LLM context per subtask. 4 types: `research`, `browser`, `code`, `scraper` — each with a restricted tool whitelist (the load-bearing security boundary). Hard 20-round cap. Only the final result string flows back to the orchestrator (keeps context lean). Dedicated system prompts in `prompts/subagent_*.md`. Passes `allowed_tools` to `run_loop()` so even text-extracted tool calls can't escape the whitelist.

**Budget & Events (supporting modules):**
- **`agent_budget.py`** — `BudgetLimits` dataclass: `max_turns`, `max_tool_calls`, `max_input_tokens`, `max_output_tokens`. Constructed via `BudgetLimits.from_config()`. Used by both interactive turns and goal orchestration.
- **`agent_events.py`** — typed `AgentEvent` dataclass with 8 event types (content/thinking/tool/turn/status deltas, budget warnings). Replaces callback spaghetti for fine-grained instrumentation.

**State model:**
- `goals` table — durable queue + status machine. Budget caps in wall-clock seconds + USD. Lease protocol for automatic failover.
- `goal_checkpoints` — gzipped messages blob + plan JSON + facts snapshot. Created at subtask boundaries AND every `checkpoint_round_interval` (default 3) rounds mid-subtask.
- `goal_facts` — structured key/value store scoped per-goal. Survives context compaction — source of truth for intermediate findings. Readable/writable from subagents.
- `goal_outputs` — durable deliverables (kind: file/link/report). Lets UI render Download/Open/Save buttons without parsing prose.
- `goal_events` — append-only event log for observability.

**Config**: `checkpoint_round_interval` (default 3), `worker_concurrency`, `worker_poll_interval_sec`, `worker_inline`, `orchestrator_max_turns`, `subagent_default_max_rounds`, `acceptance_gate_max_attempts` in `EDITABLE_SETTINGS`. `max_tool_rounds` (default 0 = unlimited) controls tool call rounds per turn.

**`spawn_task` still exists** as fire-and-forget short-task tool (<5 min, in-memory). `goal_create` / `dispatch_subagent` are the durable alternatives for hours-long work.

### Acceptance gate — anti-capitulation defense (v0.22.1+, migration 014)

Three-layer protection against the orchestrator marking a goal "done" without producing the deliverable the user asked for. Inspired by Anthropic's Stop-hook pattern (`{decision: block, reason: <remediation>}`):

- **Subtask-level (in `db.update_subtask`)**: when LLM calls `subtask_update("st_N", "completed")`, the subtask's stored `done_condition` runs through `goal_validators.run_validator`. Fail → status held (NOT advanced to completed), `attempts++`, `last_validation_failure=<remediation>`. Tool wrapper surfaces `"Subtask X NOT marked complete: validator failed. Remediation: ..."` to the LLM as the tool result so it knows what to fix.
- **Goal-level (`goals.done_conditions` JSON column, migration `014_goal_done_conditions.sql`)**: 0..N criteria attached to the goal itself, run by `goal_runner` AFTER all subtask validations pass, BEFORE `mark_goal_done`. Set via `POST /api/goals` `done_conditions: [...]` or `POST /api/goals/{id}/done-conditions`.
- **Plan-required guard**: orchestrator returning with no subtasks AND no goal-level conditions → `mark_goal_failed("no_plan_created")` after `MAX_GATE_ATTEMPTS=3`. Prevents the orchestrator from short-circuiting the entire plan with a one-shot summary.

`goal_validators.py` ships 5 kinds: `files_exist`, `min_count`, `regex_in_file`, `shell_returns_zero`, `http_200`. Each returns `(passed: bool, remediation: str)` and NEVER raises. `goal_runner` retry loop: failures → build a remediation `system_note` + re-enter orchestrator. After 3 attempts of full goal-level retry: `mark_goal_failed("acceptance_gate_exhausted: N subtask + M goal-level condition(s) still failing")`. Configurable via `acceptance_gate_max_attempts`.

**Auto-attach workspace outputs** (`db.auto_attach_workspace_outputs`) — runtime safety net on goal finalize. Scans `~/.castor/workspace/` for files with `mtime > started_at`, attaches each as `kind=file` goal_output. Blacklists `browser_sessions/`, `memory/`, `kb/`, `module_data/`, hidden files, `.pyc/.log/.tmp/.bak/.swp`, files >10MB. Dedups vs orchestrator's explicit `goal_attach_output` calls. Runs in `goal_runner.run`'s `finally` block — fires on EVERY terminal path (done / failed / paused / cancelled) so partial progress is always visible in the UI even when the orchestrator capitulated mid-goal.

**Subagent rejection-reason feedback (Variant A, v0.23.x)** — `dispatch_subagent` accepts `previous_attempt_feedback: str` when re-dispatching the same subtask after rejecting a prior result. Stored on plan as `last_rejection_reason` for UI/audit (rendered as amber callout in the Plan tab, distinct from the red validator-remediation box). Injected as a 2nd `{role:"system"}` message right after the role prompt, BEFORE the user prompt, so the subagent reads it as a "what to avoid this time" directive. Empty/whitespace → treated as None. Capped at 4000 chars before storage AND before prompt injection.

### Skill loader (`skills/__init__.py`)

**Integrity verification** (v0.22.1): user skills at `~/.castor/skills/` are SHA-256 hashed on first load (manifest stored in `skill_hashes.json`). Subsequent loads verify the hash — mismatch → `ImportError` + log.warning. Built-in skills (under `skills/` in repo) are exempt.

**Namespace collision detection**: `get_tools()` tracks `seen_names` — if two active skills define the same tool name, the first wins and a log.warning is emitted for the duplicate.

### Discovery service (`discovery.py`)

Auto-discovers LLM servers on the local network by scanning known ports (LM Studio 1234, Ollama 11434, llama.cpp 8080). Returns discovered servers with host/port/provider/model lists. Used by the provider picker UI.

### Skill import from skills.sh / GitHub (`skills/skill_import.py`)

**Added v0.18.x.** Imports community skills following the agentskills.io SKILL.md spec (YAML frontmatter + markdown body + optional `scripts/` `references/` `assets/`). Two layers:

1. **Adapter generation**: skills.sh skills are markdown-instructions-for-LLM, castor skills are `TOOLS + execute()` Python modules. The importer writes a thin adapter `.py` to `~/.castor/skills/<name>.py` with `DESCRIPTION` / short `INSTRUCTION` / one tool `<name>_help` returning the SKILL.md body verbatim. Generated via `repr()`-substituted source so Windows paths with `\U` etc. don't crash the parser.
2. **Asset staging**: scripts / references / assets land at `~/.castor/skills_imported/<name>/`. Agent reads them via the regular `read_file` / `shell` tools.

Safety surface:
- Domain allowlist: `skills.sh`, `github.com`, `raw.githubusercontent.com`, `api.github.com`. Everything else → 403 `host_not_allowed`.
- SSRF guard (private/loopback IPs blocked, `CASTOR_ALLOW_PRIVATE_URLS=1` opt-out — same env var as `/api/knowledge/url`).
- Name validation matches the agentskills.io regex `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤64 chars.
- **Built-in skills are NOT overridable** even with `overwrite=true` — typosquatting defense via `_BUILTIN_SKILL_NAMES` set.
- License surfacing: non-OSS-marker licenses (e.g. Anthropic's "Complete terms in LICENSE.txt") return HTTP 451 `license_confirm_required` with the license text in `details`. Web UI shows confirm panel; CLI re-POSTs with `accept_license: true`.
- Caps: SKILL.md ≤100 KB, total fetch ≤1 MB, ≤50 files, binary/image extensions filtered out.

Persistence: `skill_imports` SQLite table (migration `007`) records `name` PK, source URL, source kind, SHA-256 hash, license, imported_at — provenance for audit + future "check for upstream updates".

REST: `POST /api/skills/import` (declared BEFORE `POST /api/skills/{name}` to avoid the catch-all swallowing `import` as a skill name — same FastAPI ordering gotcha as `/api/presets/onboarding`), `GET /api/skills/imports`, `DELETE /api/skills/imports/{name}`.

Tests in `tests/test_skill_import.py` (33 tests) mock HTTP via `monkeypatch.setattr(si, "_fetch_url", ...)`. Full doc at `docs/SKILLS_IMPORT.md`.

### Skill export to agentskills.io format (`skills/skill_export.py`)

Companion to `skill_import.py` — exports a Castor `.py` skill into the agentskills.io v1 bundle: `<slug>/SKILL.md` (YAML frontmatter + markdown body) + `scripts/<slug>.py` (source preserved verbatim) + optional `references/CASTOR_TOOLS.md` (rendered TOOLS schema reference). AST-based metadata extraction NEVER imports/executes the source (canary test guards top-level side effects). One-step `export_skill_to_zip()` for "Download SKILL.zip" UI. Name slugified `my_skill` → `my-skill` to match agentskills.io regex.

### Commands registry (`commands.py`)

Single source of truth for slash commands across CLI / Telegram / Web. Each entry: `CommandDef(name, description, category, surfaces: frozenset[str], aliases, args_hint)`. Helpers: `by_name(token)` (strips leading `/`, case-insensitive), `resolve(token)` (handles aliases, takes first word only), `for_surface("tg"|"cli"|"web")`, `categories_for(surface)` (grouped for /help rendering).

Consumers derive from it automatically:
- `telegram_bot.get_commands()` reads `for_surface("tg")` and pushes to BotFather's `setMyCommands`
- `telegram_bot._handle_bot_command` `/help` renders `categories_for("tg")` grouped
- `GET /api/commands?surface=web|cli|tg` exposes the list as JSON for future Web autocomplete
- CLI's `if user_input.startswith("/X")` chain stays surface-specific (handlers differ per surface) but can validate against `by_name`

Adding a new command: append a `CommandDef` literal + handler branch in each surface's dispatcher. Tests pin shape (no leading-slash in name, unique names, ≤256-char descriptions for Telegram limit, aliases don't collide).

### Plugin slot framework (`plugin_registry.py`)

Entry-point-based plugin discovery via Python's standard `importlib.metadata.entry_points`. Slots declared in `KNOWN_SLOTS`:
- `SLOT_MEMORY_BACKEND` — alternatives to in-tree Qdrant (e.g. Honcho, mem0, supermemory)
- `SLOT_CONTEXT_ENGINE` — alternative context-compression strategies
- `SLOT_MODEL_PROVIDER` — alternative LLM backends beyond the OpenAI-compatible / native-Anthropic pair
- `SLOT_OBSERVABILITY` — metrics / traces / logs sinks

Plugins register in their `pyproject.toml`:
```toml
[project.entry-points."castor.memory_backend"]
honcho = "castor_honcho:Plugin"
```

Lookup: `plugin_registry.find(slot, name) -> value` / `.list_all(slot) -> [names]`. Lazy discovery, per-slot cache (`clear_cache(slot)` forces rescan). Defensive: broken plugin (ImportError, missing attribute) is logged at WARNING and skipped — never kills the whole slot. Test injection via `_override_for_test(slot, {name: value})` so unit tests don't need a real plugin package installed.

In-tree modules (`memory.py`, `providers.py`, etc.) remain the default for each slot. Migrating an in-tree module to use the registry pattern is a per-slot decision in a future PR — the framework is ready, but no slot is forced into it yet.

### Trajectory recording (`trajectory.py`)

Opt-in JSONL output per agent run at `~/.castor/trajectories/<run_id>.jsonl` — for audit, debug, and future training data. Each event = one JSON line: `run_start`, `tool_start`, `tool_end`, `content`, `thinking`, `turn_end`, `run_end`. JSONL (not JSON) so partial files from crashes stay readable up to last `\n`, and replay tools can stream-process.

Public API: `trajectory.start(source, model=..., thread_id=...) -> TrajectoryRecorder | None`. Context-manager form `with trajectory.recording(source, ...) as rec:` auto-finalises on exit (writes `run_end` even on exception, captures error class+message). Returns a `_NullRecorder` no-op stub when disabled so callers don't need `if rec is not None` guards. `attach_to_emitter(emitter, recorder)` bridges an existing `agent_events.EventEmitter`.

Inspection: `trajectory.load_run(run_id)` (skips malformed lines), `list_runs(limit=50)` (newest first), `prune_old(days)` for auto-cleanup. Settings: `trajectory_enabled` (default 0 = opt-in), `trajectory_keep_days` (default 30). All write paths swallow OSError / JSONEncodeError — broken file never crashes the calling agent.

### Continuous synthesis trickle (`synthesis.py`)

In addition to the once-a-day-at-03:00 batch (`__synthesis__`), scheduler also fires `__synthesis_continuous__` every `synthesis_continuous_interval_min` minutes (default 15) with a small batch of `synthesis_continuous_max_per_run` items (default 5). New memory becomes searchable within minutes instead of waiting until the next 03:00 run. The nightly batch keeps running as catch-up for anything the trickle missed.

Settings: `synthesis_continuous_enabled` (default 1, on; disable doesn't affect nightly), `synthesis_continuous_interval_min`, `synthesis_continuous_max_per_run`. Wrapper `synthesis.run_continuous()` respects its own enable flag separately from the master `synthesis_enabled`. Registration via `scheduler._register_synthesis_continuous()` is idempotent: re-running at startup updates the schedule when the interval setting changes.

### Canvas skill (`skills/canvas.py`) — sandboxed HTML side panel

**Added v0.18.7.** Lets the agent render rich UI (forms / dashboards / mockups) in a 480px right-side panel. Auto-active (in `_DEFAULT_SKILLS`).

Five tools: `canvas_render` (fire-and-forget), `canvas_prompt` (BLOCKS until user submits — mirrors `camera_capture`'s `_pending_frame_requests`), `canvas_save`, `canvas_load`, `canvas_list`. `canvas_prompt` is the form-submission entry point; agent emits HTML with a postMessage handler, blocks until user fills the form, gets back the data as a JSON tool-result.

**Iframe sandbox is the load-bearing security boundary.** `<iframe sandbox="allow-scripts allow-forms" srcdoc="...">`. **No `allow-same-origin`** — iframe origin is `"null"`, so it can't read parent cookies/localStorage/DOM. Trust check on the postMessage listener filters by `event.source === iframe.contentWindow` (origin-string filtering is useless when origin is `"null"`). 256 KB HTML cap at both skill entry and `/api/canvas/artifacts` POST.

**Mutually exclusive with the inspector** — same right-column slot. Opening canvas auto-closes inspector. Hidden on screens <1100px.

Persistence: `canvas_artifacts` table (slug PK, title, html ≤256 KB, created/updated_at, thread_id, meta JSON). Migration `006_canvas_artifacts.sql`. REST: `GET/POST/DELETE /api/canvas/artifacts*`. New left-nav **Canvases** view (`renderCanvasesView()`) browses saved artifacts as a card grid.

Pattern reused from `camera_capture`: `_pending_canvas_prompts[req_id] = {event, data, closed}`; `request_canvas_prompt_sync()` opens the panel + awaits the asyncio.Event. Pattern reused from `task_update`: `canvas_render` / `canvas_close` WS events short-circuit BEFORE the streaming-message gate at line ~1985 in `handleWsMessage` (otherwise rendering a dashboard would also pop a ghost assistant message).

Full data inventory + postMessage protocol + reference HTML template: `docs/CANVAS.md`.

### Project blog feed (`/api/feed/blog`)

**Added v0.18.5.** Server-side proxy of `https://deepfounder.ai/tag/castor/rss/`, rendered as a "From the blog" strip above the preset grid. **NOT telemetry** — empty body, no `anonymous_id`. Only signal deepfounder.ai sees is "an install asked for the feed" (IP + `castor/<ver>` UA).

- 30-min in-process cache (`_feed_cache` + lock), 15s urlopen timeout, 10 items max.
- Parser bounded everywhere: title ≤300, desc ≤500, ≤8 categories, etc. — a malicious upstream can't blow up the response.
- On any fetch error the endpoint still returns 200 with the last-known cached items + `error` field. **Never raises into the UI.** Cold-cache + upstream-down → empty list + error, Presets view still works.
- Frontend lazy-loads on Presets view entry; `state.blogFeedLoaded` flag prevents re-fetching on rapid view switches.
- Documented under "Other project-controlled outbound HTTP" in `docs/PRIVACY.md` so users can audit.

### Voice / Camera / Knowledge ingest

- **STT** (`stt.py`): auto/local/api; local = faster-whisper on CPU; API = OpenAI-compatible (Groq free tier works). PyAV fallback when ffmpeg missing.
- **TTS** (`tts.py`): auto-detects API style (OpenAI `/v1/audio/speech`, custom `/tts` w/ voice cloning, Fish Speech, s2.cpp).
- **Camera**: `camera_capture(prompt?)` grabs frame via WebSocket (browser) or OpenCV (direct). Persistent `_camera_cap` for fast repeat captures. WS event names unified `get_frame` / `frame_request` (v0.18.2). Client falls back to one-shot `getUserMedia` when PiP isn't active. OpenCV path has black-frame guard (mean<25 → up to 30 retries, sensor warmup) for Windows DirectShow gotchas. Auto-detect picks BRIGHTEST of indexes 0-3, not first non-pitch-black. Resolution + JPEG quality user-tunable via `camera_resolution` (auto/480p/720p/1080p) + `camera_quality` (1-100) settings.
- **URL/file indexing** (`rag.py`): MarkItDown handles PDF/DOCX/PPTX/XLSX/HTML/etc. YouTube-specific path uses `yt-dlp` with `player_client=["android","ios","web"]` to dodge DRM blocks, native-language preferred over auto-translated English.
- **SSRF** (v0.17.18): `/api/knowledge/url` blocks private/loopback/link-local IPs unless `CASTOR_ALLOW_PRIVATE_URLS=1`. Uses `socket.getaddrinfo` + `ipaddress.ip_address.is_private`.

### Web UI (`static/index.html`)

Single-file SPA, ~5500 lines of vanilla JS. **No build step** — `scripts/check_js.py` runs `node --check` on the extracted `<script>` at pre-commit + CI.

- Telegram-style pill composer; three-dot menu on mobile.
- Live Voice Mode: VAD → STT → LLM → TTS → auto-listen.
- Camera PiP overlay with capture-on-send.
- Inspector: Context Window gauge (`prompt_tokens / context_budget` — agent-side limit, NOT model context), Recalled memories (from `_emit_recall` via WS, shows `source: thread/wiki/entity/...`), Active tools, Latency bars.
- Knowledge Graph: force-directed layout + pan/zoom/drag (v0.17.11).
- Provider picker with `NEEDS KEY` badges (v0.17.17) + key modal with built-in URL hints per provider.

Render pattern: every state change rebuilds innerHTML. Event handlers attached via `wireEvents()` on each render. Globals attached ONCE (guard: `state._graphGlobalHandlersAttached = true`).

**`api()` helper contract (v0.18.5):** every JSON API call sets `cache: 'no-store'` so browsers never replay stale GET responses. FastAPI doesn't send `Cache-Control` headers, so without this, browsers heuristic-cache JSON and serve pre-mutation state right after a POST. Concrete bug this prevents: boot → GET `/status` (cdm:false) → cached → user clicks Enable → POST opt-in (stores cdm:true) → reload → browser serves cached cdm:false → telemetry modal re-opens forever. Pinned by `tests/test_telemetry.py::test_api_helper_disables_http_cache`.

**File rendering — live + reload paths must agree:** `splitFiles()` (around line 1876) splits a `files` list into `images` (by `is_image` flag OR `\.(png|jpe?g|gif|webp|bmp|svg)$/i` extension) and `others`. Images go to `_images` (inline `<img>`); others to `_files` (download chips). BOTH the live WS handler (`type === 'reply' / 'files'`) AND the reload mapper (`loadActiveMessages` over `meta.files`) must call `splitFiles` — otherwise an image the agent sent via `send_file` shows inline during the live turn but flips to a download link after the user leaves the thread and comes back. Pinned by `tests/test_ws_attachments.py::test_reload_path_runs_meta_files_through_splitfiles`.

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

`docker.yml` builds and pushes a Docker image to GitHub Container Registry on push to `main` / tags. Uses Docker Buildx with cache.

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

All ~1337 tests run in a single pytest process (v0.17.24 — no more sys.modules pollution). Do NOT add `sys.modules[...] = mock_X` at module scope — use `monkeypatch` fixtures (see `tests/conftest.py` for `qwe_temp_data_dir`, `mock_llm`).

Notable files:
- `test_integration.py` — TestClient + mocked LLM end-to-end (added to catch v0.17.23-style lazy-import SyntaxErrors)
- `test_turn_context.py` — cross-source callback isolation (web vs telegram)
- `test_shell_safety.py` — obfuscation bypass catalog (39 cases)
- `test_secret_scrub.py` — regex patterns for API key shapes
- `test_migrations.py` — fresh + back-compat + idempotent + rollback
- `test_skill_creator_smoke.py` + `test_skill_creator_pipeline.py` — pure helpers + e2e pipeline with mocked LLM (camera-using skill regression test)
- `test_telemetry.py` — privacy contract + Countly + raw + consent gates + JS contract tests for `api()` no-store directive (70 tests)
- `test_blog_feed.py` — RSS parser bounds + endpoint cache TTL + graceful fallback on upstream-down (12 tests)
- `test_ws_attachments.py` — WS image/document round-trip + reload-path `splitFiles` contract
- `test_text_to_tool_extraction.py` — all 5 patterns including the `!<function_call:>` Qwen variant (#10)
- `test_camera_settings.py` — preset table + helper, `pytest.importorskip("cv2")` for CI
- `test_orchestrator.py` — orchestrator loop + plan management
- `test_subagent.py` — subagent dispatch + tool whitelist enforcement
- `test_worker_lifecycle.py` — worker daemon claim/lease/heartbeat
- `test_goal_plan_facts.py` — goal facts and plan state
- `test_goals_ui_contracts.py` — goal UI WebSocket event contracts
- `test_db_protection.py` — schema protection (skill tables must be prefixed) + FTS5 escape sanitization
- `test_acceptance_gate.py` + `test_acceptance_gate_e2e.py` — 5 validator kinds + gate re-entry loop
- `test_goal_level_done_conditions.py` — per-subtask done_condition wiring
- `test_path_safety.py` — symlink resolution + write whitelist enforcement
- `test_skill_loading.py` — skill integrity hash verification + namespace collision detection
- `test_loop_detection.py` — multi-period (1/2/3) loop detection helper
- `test_anthropic_convert.py` + `test_anthropic_stream.py` + `test_anthropic_client.py` — native Anthropic adapter (88 tests: 34 converter + 41 stream + 13 client/routing)
- `test_tool_search_persistence.py` — per-thread tool activations survive turn boundary (12 tests)
- `test_commands_registry.py` — slash-command single-source-of-truth (26 tests)
- `test_synthesis_continuous.py` — trickle synthesis scheduler + dispatch (14 tests)
- `test_skill_export.py` — agentskills.io export bundle + zip (33 tests)
- `test_plugin_registry.py` — entry-point-based plugin discovery (17 tests)
- `test_trajectory.py` — opt-in JSONL recording for agent runs (24 tests)
- `test_subagent_feedback.py` — `previous_attempt_feedback` channel from orchestrator → subagent (13 tests)
- `test_browser_subagent_propagation.py` — ctx propagation pin for per-goal session routing (5 tests)
- `test_browser_per_goal_sessions.py` — `BrowserSession` registry isolation
- `test_goal_auto_attach.py` — workspace-diff auto-attach of files written during a goal (13 tests)
- `conftest.py` — shared fixtures; `scope="session"` TestClient lives here

**JS contract tests live in pytest.** Two exist so far: `test_api_helper_disables_http_cache` (pins `cache:'no-store'`) and `test_reload_path_runs_meta_files_through_splitfiles` (pins live/reload symmetry). Pattern: `Path(...).read_text()` on `static/index.html`, locate a stable anchor string, assert the contract holds in a window after it. Cheap regression guards for JS-side bugs that pytest would otherwise miss entirely.

Coverage baseline 25.93%; floor 24% (`pyproject.toml::tool.coverage.report.fail_under`). Some modules under 10% (`cli.py`, `inference_setup.py`, `synthesis.py`) are candidates for future integration tests.

CI flake to know about: `tests/test_telemetry_wireup.py::test_tool_error_classifies_keyboard_interrupt_as_aborted` sometimes fails in full-suite collection (KeyboardInterrupt is special-cased by pytest); always passes in isolation.

## Data layout (`~/.castor/` — override via `CASTOR_DATA_DIR`)

- `castor.db` — SQLite (messages, threads, kv, settings, cron, secrets)
- `memory/` — Qdrant vectors (disk mode)
- `wiki/` — synthesized markdown pages
- `skills/` — user-dropped `.py` skills
- `uploads/` — images, docs, camera captures, TTS mp3s (startup sweep deletes files >14 days old; `uploads/kb/` kept — indexed knowledge sources)
- `workspace/` — default CWD for relative paths (swapped when preset active)
- `presets/<id>/` — installed presets (each with own `workspace/`, `knowledge/`, `skills/`)
- `logs/` — castor.log (INFO+), errors.log (WARNING+)

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CASTOR_LLM_URL` | `http://localhost:1234/v1` | Provider base URL |
| `CASTOR_LLM_MODEL` | `qwen/qwen3.5-9b` | Active model id |
| `CASTOR_LLM_KEY` | `lm-studio` | API key |
| `CASTOR_DATA_DIR` | `~/.castor` | Where state lives |
| `CASTOR_DB_PATH` | `$DATA_DIR/castor.db` | SQLite path |
| `CASTOR_QDRANT_MODE` | `disk` | `memory` / `disk` / `server` |
| `CASTOR_PASSWORD` | — | Web UI auth (when exposing on LAN) |
| `CASTOR_STT_DEVICE` | `cpu` | faster-whisper device |
| `CASTOR_EMBED_DEVICE` | `cpu` | FastEmbed provider — CPU by design (v0.17.21). Set `cuda` only if you've installed `onnxruntime-gpu` + matching CUDA Toolkit manually. |
| `CASTOR_ALLOW_PRIVATE_URLS` | unset | Set to `1` to bypass SSRF block on `/api/knowledge/url` (dev only). |

Telemetry-related settings live in `EDITABLE_SETTINGS` (not env vars): `telemetry_enabled` (default 0), `telemetry_endpoint` (default `https://qwelytics.deepfounder.ai/i`), `telemetry_format` (`raw` / `countly`, default `countly`), `telemetry_countly_app_key`, `telemetry_anonymous_id`, `telemetry_consent_version`. Operators / forks edit defaults in `config.py`; end-users only see Enable/Disable.

Hardening-related settings (v0.22.1): `routine_dry_run_mock` (int, default 1 — stubs dangerous tools during dry-run validation), `system_task_budget_usd` (float, default 5.0 — USD cap for synthesis/heartbeat tasks), `failure_alert_threshold` (int, default 3 — consecutive routine failures before log.error), `tool_timeout_shell` (int, default 120, range 5–600), `tool_timeout_http` (int, default 5, range 1–60).

## When adding a feature — quick checklist

1. **New .py module?** Add to `[tool.setuptools] py-modules` in `pyproject.toml` (else `pip install -e .` crashes on import for downstream installs).
2. **New tool?** Add to `tools.TOOLS` list + branch in `tools.execute()`. If it takes a `path` arg, call `_get_path_arg(args)` (models use various field names). If it's dangerous (writes, shells), it must pass `_pre_dispatch_safety_check`. Also map the new tool name to a category in `tools.TOOL_CATEGORIES_BY_NAME` so telemetry events bucket correctly.
3. **New per-turn state?** Put on `TurnContext`, not as a module global.
4. **New setting?** Add to `EDITABLE_SETTINGS` in `config.py` with `(kv_key, type, default, desc, min, max)`. `config.get("foo")` reads with defaults.
5. **New schema change?** New file `migrations/NNN_snake_case.sql`. `_apply_migrations()` picks it up.
6. **New doctor check?** Add to `cli.py:doctor()`. Must survive cp1251 terminals — no raw emoji in output.
7. **New WS event?** Emit via `ctx.on_*` callback, not a global. Client reads in `handleWSMessage` in `static/index.html`. If event is non-chat (notification / status / etc.), short-circuit at the top of `handleWsMessage` BEFORE the `state.streaming` creation gate — otherwise it triggers a ghost streaming message in the chat (lesson from `task_update` bug, fixed in v0.18.3).
8. **New telemetry event?** Add to `telemetry.ALLOWED_EVENTS` whitelist with type-strict prop schema. String props that could carry free text MUST use a closed enum. Bump `telemetry._CURRENT_CONSENT_VERSION` so existing opted-in users get a re-consent banner. Wire the emitter near the action it observes (e.g. `_emit_thread_created_telemetry` lives in `threads.py`); always lazy-import telemetry + swallow exceptions so a queue/network blip can't break the host operation.
9. **Skills are gitignored except whitelisted.** When working on built-in skills (`skills/skill_creator.py` etc.), `.gitignore` entry `skills/` excludes them by default; whitelist (`!skills/skill_creator.py`) keeps the built-ins tracked. Side effect: `ruff check .` skips skills/ via gitignore — for those files run `ruff check skills/` explicitly.
10. **New subagent type?** Add to `subagent.SUBAGENT_TOOLS` with a restricted tool whitelist. Add a matching system prompt in `prompts/subagent_<type>.md`. The whitelist is the security boundary — every extra tool is a chance for the LLM to wander off.
11. **Before commit**: `ruff check .`, `python scripts/check_js.py` (if you touched `static/index.html`), `pytest tests/`.
12. **New top-level slash command?** Edit `commands.py::COMMAND_REGISTRY` with a `CommandDef`, set `surfaces=frozenset({...})` for the surfaces you support, then add the handler branch in each surface's dispatcher: `cli.py` for CLI, `telegram_bot._handle_bot_command` for Telegram, the send-side intercept in `static/index.html` for Web. Telegram's BotFather menu auto-updates from the registry on next `_register_commands` call.
13. **New plugin slot?** Edit `plugin_registry.py::KNOWN_SLOTS` + add a `SLOT_<NAME>` constant. Document the entry-point group name as `castor.<slot>` and what the loaded value's interface should look like (class? factory? module?). Don't force-migrate existing in-tree modules; the framework is for new third-party additions.
14. **New goal validator kind?** Edit `goal_validators.py::_KINDS`, write `_run_<kind>(spec) -> (bool, remediation_str)`, extend `validate_criterion` with the shape check (required spec fields, types). Update `docs/specs/2026-05-16-acceptance-gate.md`. Tests: ≥4 cases per kind (pass / fail / missing field / type error). Validators MUST never raise — wrap everything in try/except and convert to `(False, "<diagnostic>")`.
15. **New trajectory event type?** Just call `recorder.event(custom_type, payload)` — the format is open-ended. If it deserves a typed convenience method, add to `TrajectoryRecorder` next to `tool_start` / `tool_end`.
16. **Want native Anthropic features (caching, thinking budgets)?** Use `pip install 'castor[anthropic_native]'` + set `ANTHROPIC_API_KEY` + switch provider to `anthropic`. To opt OpenRouter Anthropic models into the native adapter: `setting:anthropic_native_routing=1`.
