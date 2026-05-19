## v0.23.0 — Goals, Native Anthropic, Plugins

### Goals system (orchestrator + subagents)

Long-running multi-step tasks are now first-class citizens. Create a goal in the Goals view and Castor breaks it into a plan, dispatches subagents per subtask, and tracks progress live.

- **Orchestrator** (`orchestrator.py`) — manages goal lifecycle: plan → dispatch → accept/remediate → finalize
- **Subagent types** — inline worker, browser session per goal (parallel isolation), skills + MCP tools available to subagents
- **Done conditions** — each subtask declares acceptance criteria (`goal_validators.py`); 5 validator kinds (exact match, regex, file exists, HTTP check, LLM judge)
- **Acceptance gate** — on subtask completion the gate re-runs done_conditions; fails kick off a remediation loop with rejection feedback
- **Structured deliverables** — goals produce typed outputs: files (download), links (open), reports (save to memory)
- **Budget caps** — wall-clock + USD hard caps enforced per goal
- **Inline worker** — Goals work out of the box with no extra daemons; launchd/systemd optional for persistent background execution
- **Live Goals UI** — plan, events, facts tabs; live subtask progress; goal-detail modal with expandable description and markdown rendering

### Native Anthropic adapter

- Full native client for Claude models (`providers.py`) — no OpenAI shim
- Three workstreams merged: converters, stream reassembler, routing + 88 tests
- Model routing: local providers (LM Studio / Ollama) via OpenAI-compat, cloud Claude via native SDK
- `NEEDS KEY` badge + key modal in provider picker

### Agent infrastructure

- **Plugin slot framework** — hook points for extending agent behavior without forking core (Hermes-inspired)
- **JSONL trajectory recording** — every agent run saved as a structured trace for observability and debugging
- **Synthesis trickle mode** — continuous background curator instead of nightly-only batch (Hermes-inspired)
- **Centralized slash-command registry** — `/commands` now discoverable; plugins can register their own
- **Persistent tool_search activations** — active tools survive context compaction for cache stability
- **Rejection feedback channel** — subagents receive structured feedback from prior failed attempts

### Skills

- **Skill export** (agentskills.io format) — companion to `skill_import`; share skills with the community
- **Skill name normalization** — hyphens and underscores treated as equivalent in `_find_skill`

### Reliability

- **3-layer DB corruption protection** — rolling backups, startup integrity check (`PRAGMA integrity_check`), graceful shutdown WAL checkpoint
- **Auto-migration from `~/.qwe-qwe/`** — users upgrading from the old project name get all data (DB, Qdrant collections, uploads, skills) migrated automatically on first boot
- **SSL**: certifi CA bundle used for all outbound urllib requests
- **fastembed warnings** suppressed (loguru "Local file sizes do not match" spam gone)
- **Browser**: per-goal sessions for parallel isolation; auto-recovery on dead sessions; `execute()` runs in thread executor to avoid asyncio conflicts

### UI

- Collapse tool-call rows beyond N per category in chat
- Per-message token stats in chat
- Report outputs + final reply rendered as real markdown
- Goal-detail tabs styled properly; full report content shown inline
- Auto-dismiss prior aborts on new turn
