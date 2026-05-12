# Auto-resume after interrupt

qwe-qwe v0.20.0 makes every abort recoverable: closed tab, dropped network, clicked Stop, or server restart no longer throws away an in-progress agent turn. The agent picks up from where it left off, not from scratch.

## What's resumable

| Source | Triggers | TTL (default) |
|---|---|---|
| Web | WS disconnect, `/api/abort` (Stop), server crash | 7 days |
| Telegram | Same triggers, in chat-scoped thread | 24 hours |
| Routines | Server crash mid-fire | 5 minutes (auto-fires on next startup) |
| CLI | — | n/a (Ctrl+C is intentional stop) |

## What's NOT resumable

- CLI Ctrl+C — explicit stop signal from the user
- `err` runs — the turn raised; user fixes and re-sends
- Missed routine slots — a slot that lapsed entirely while the server was offline is `missed`, not `aborted`; that's a different mechanic (existing `routine_runs` history)
- Tool side effects that already happened — the agent sees prior tool calls in conversation history, so it shouldn't repeat them, but exact "don't re-do anything" guarantees aren't possible without server-side tool state (out of scope for now)

## How resume works

When an agent run is aborted:

1. The partial assistant content (whatever text was streamed before abort) is saved to `messages` with `meta.interrupted=true` and a `run_id` reference.
2. The `agent_runs` row is finalized with `status='aborted'`.
3. On server restart, any orphaned `running` rows are promoted to `aborted` (with `meta.crash_recovery=true`).

When the user clicks **Resume** (Web banner / `/resume` in Telegram / auto-fire for routines):

1. A new agent.run is started with `system_note="The previous turn was interrupted before completing. Continue from where you left off..."` — this is a real `{role: "system"}` message in the next LLM call (NOT a user-role `[system]` prefix, which would break model flow).
2. The conversation history already includes the partial assistant message, so the model literally sees its own incomplete output and gets the system_note as a continuation instruction.
3. The new run's `agent_runs` row links to the original via `resumed_from_run_id`, so analytics can chain them ("run #142 (resumed #138)").

## UX per source

**Web** — On WS reconnect, the server emits an `interrupted_turn` event. The SPA shows a banner above the chat:

> Previous turn was interrupted (3 min ago) — "I'll start by searching for X..." [Resume] [Dismiss]

Click Resume to continue; click Dismiss to mark the run as no longer resumable.

**Telegram** — Send `/resume` to the bot. If there's an eligible interrupted task within the TTL window for that chat, the bot replies "Resuming previous task..." and runs the resume in the background. Regular messages do NOT auto-resume; they start a new turn.

**Routines** — On every server startup, the scheduler scans for aborted routine runs within `resume_ttl_routine_sec` (default 5 min). Each one is auto-fired so short server blips don't drop a scheduled fire. Older aborted routine runs are left in place — the cron's next slot has already moved on.

**CLI** — No resume button. Ctrl+C is explicit stop. Aborted CLI runs are still recorded in `agent_runs` (visible in Web UI as analytics), but you can't resume them from CLI.

## TTL configuration

Settings → Cost → Auto-resume:

- Resume window — Web (days, default 7)
- Resume window — Telegram (hours, default 24)
- Resume window — Routines (minutes, default 5)
- Auto-resume routines on server start (checkbox, default on)

Stored in KV as `resume_ttl_web_sec`, `resume_ttl_telegram_sec`, `resume_ttl_routine_sec`, `resume_routine_auto`.

## Privacy

No new outbound network traffic. The partial assistant content is stored locally in `~/.qwe-qwe/qwe_qwe.db` (same place as the rest of your conversation history). No telemetry payload carries content — see `docs/PRIVACY.md`.

## Analytics

The Sessions list shows a chip on threads with un-dismissed interrupted runs (clickable; opens the runs modal). The drilldown shows the resume chain: an aborted run row followed by its resume run row, with `resumed_from_run_id` linking them.

## Troubleshooting

- **"Resume button doesn't appear"** — either the run is older than the TTL, was dismissed, or was a CLI run. Check `agent_runs` directly.
- **"Resume produced different output than the original"** — expected when the model is non-deterministic or has been swapped. The original run's model is preserved in the audit trail (`agent_runs.model`); the resume run records the currently-active model.
- **"I see a routine re-fire I didn't expect"** — auto-resume re-runs an aborted routine if it crashed within the last 5 min. Turn off `resume_routine_auto` in Settings to disable.
- **"Resume failed" toast in the UI** — the run was already dismissed or already resumed (e.g., two tabs raced). The first call wins; the second sees a 400.
