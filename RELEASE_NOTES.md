## v0.23.0 — Goal Runtime, Native Anthropic, Plugin Framework

The biggest release since v0.18.7. Goals turn castor from a chat assistant into an autonomous agent that can work for hours on multi-step tasks — surviving disconnects, process restarts, and context-window pressure.

---

### Goals — long-running autonomous tasks

Create a goal ("Research construction costs in Argentina and write a report"), walk away, come back to a completed deliverable. The system plans, delegates to specialized subagents, validates results, and retries on failure — all without user input.

**Architecture**: Goal -> Plan -> Subagent dispatch. A separate `castor-worker` daemon claims goals from a durable SQLite queue. Full design doc in `docs/superpowers/plans/`.

- **Worker daemon** (`python -m worker`) — claims goals via lease, heartbeats, survives crashes. Also runs inline (`--once` for tests, auto-start in web mode).
- **Orchestrator** — breaks the goal into subtasks, dispatches subagents, tracks progress via structured facts.
- **4 subagent types** — `research`, `browser`, `code`, `scraper` — each with a restricted tool whitelist (the security boundary). Fresh LLM context per subtask, 20-round cap.
- **Acceptance gate** — after the orchestrator returns, validators check each subtask's `done_condition` (5 kinds: `files_exist`, `min_count`, `regex_in_file`, `shell_returns_zero`, `http_200`). Failures inject a remediation note and re-enter the orchestrator (up to 3 attempts).
- **Structured deliverables** — files, links, reports attached via `goal_attach_output`. UI renders Download/Open/Save buttons.
- **Per-goal browser sessions** — parallel goals get isolated browser contexts.
- **Budget enforcement** — wall-clock seconds + USD caps, enforced at the runner level.
- **Live UI** — Goals view with plan progress, events timeline, facts tab. Polling at 2s while running, 10s when idle.

New migrations: `011_goals_subtasks_checkpoints.sql` through `014_goal_done_conditions.sql`.

---

### Native Anthropic provider

Direct Anthropic API support without the OpenAI compatibility shim. Three workstreams merged:

- **Converters** — bidirectional message/tool format translation between OpenAI and Anthropic schemas.
- **Stream reassembler** — handles Anthropic's SSE delta format (content_block_delta, tool_use blocks) and reassembles into the internal streaming shape.
- **Client + routing** — `providers.py` auto-routes to the native adapter when the active provider is `anthropic`.

88 new tests across the three workstreams.

---

### Plugin framework (Hermes-inspired)

Extensible slot-based plugin system for hooking into agent lifecycle events. Plugins can observe/modify behavior at defined extension points without touching core code.

---

### Synthesis trickle mode

Background knowledge curator runs continuously (not just overnight), extracting entities and wiki summaries from recent conversations. Keeps the knowledge graph fresh without waiting for the nightly synthesis run.

---

### Centralized command registry

Slash commands (`/goal`, `/resume`, `/status`, etc.) now registered via a central registry instead of ad-hoc string matching. Easier to add new commands, consistent help output.

---

### Skill export

Companion to skill import (v0.18.7) — export castor skills to the agentskills.io SKILL.md format for sharing via skills.sh or GitHub.

---

### JSONL trajectory recording

Every agent run optionally records a full JSONL trajectory (messages, tool calls, results, timing) for offline analysis, evals, and debugging.

---

### Persistent tool_search activations

`tool_search` activations now persist per-thread across page reloads. Previously, extended tools unlocked via `tool_search("browser")` would disappear on refresh.

---

### DB corruption protection

3-layer defense: rolling backups on startup, SHA-256 integrity check, graceful WAL checkpoint on shutdown. Recovers automatically from the most recent valid backup if corruption is detected.

---

### Notable fixes

- **Orchestrator browser tool leak** — built-in browser skill tools (24) leaked into the orchestrator's tool set, causing the LLM to bypass `dispatch_subagent` and burn 80+ rounds driving a browser directly. Fixed via `_ORCHESTRATOR_EXCLUDED_TOOLS` blacklist.
- **Goal plan validation** — error message listed wrong `done_condition` kinds; fuzzy matching now suggests corrections (`files_exists` -> `files_exist`); empty plans no longer pass the acceptance gate vacuously.
- **UI scroll jumps** — clicking nav links with `href="#"` scrolled to top; `render()` only preserved scroll for chat view. Now all `.scroll-col` containers retain position across re-renders.
- **Failed goals UI** — failed goals wouldn't open in detail view (`!gR.value.error` guard rejected them). Fixed to check `gR.value.id`.
- **Streaming tool results** — reply event was wiping tool results accumulated during streaming. `allStrings` guard preserves them.
- **Soul trait [object Object]** — built-in trait descriptions passed raw objects to `esc()`.
- **Tool-call collapse** — chat UI collapses tool-call rows beyond N per category to reduce visual noise.
- **16 audit hardening fixes** — security, robustness, and observability improvements.
- **Auto-migrate from ~/.qwe-qwe/** — seamless data migration on project rename to Castor.

---

### By the numbers

- **1354 tests passing** (was ~725 at v0.22.1), 24 skipped
- **14 SQLite migrations** (was 10)
- **Coverage floor** unchanged at 24%
- **~60 commits** since v0.22.1

---

### Upgrade

```bash
git pull
pip install -e . --upgrade
python cli.py --web --ssl --port 7861
```

Four new migrations apply automatically on first boot. No config changes required. Telemetry consent unchanged.

---

