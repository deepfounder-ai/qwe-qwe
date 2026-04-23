# v0.17.30 — Routines: one thread per routine, full chat history of every run

Scheduler reworked as **Routines**. Each routine is bound to its own permanent chat thread at creation time. Every firing appends a new turn to that thread — so a routine IS a chat that grows over time, with full tool-call history, thinking steps, and assistant replies. Click a routine card → jump straight to the thread.

## Architecture

- **Migration `004_routine_thread_id.sql`** adds a nullable `thread_id TEXT` column to `scheduled_tasks`. Legacy rows lazy-heal on their next firing via `_ensure_routine_thread`.
- **`scheduler.add()`** creates the routine's thread once at save time (`threads.create("Routine · <name>", meta={kind: "routine", …})`) and stores its id in the new column. System tasks (heartbeat, synthesis) and quick reminders don't get a thread — they're infrastructure, not user-facing.
- **`_check_and_run`** routes real routines through the new `_execute_routine(task, name, cron_id, thread_id)` which calls `agent.run(task, thread_id=thread_id, source="routine")` with a headless `TurnContext`. Every firing appends user + assistant messages to the thread via the normal `db.save_message` path — no special-case persistence code.
- **`scheduler.remove()`** archives the routine's thread when the routine is deleted (threads stay readable under "All threads" but drop out of the active list).
- **New source `"routine"`** flows through `agent.run` so other subsystems can tell cron turns apart from web/telegram/cli.

## UI rework

- **Rename**: "Scheduler" → "Routines" everywhere — nav, palette, section header, card language.
- **Simplified card**: name + human-friendly schedule ("Every day at ~9:00 · Next run tomorrow at ~9:00") + `last ok · 42ms · 5 runs` footer. **Click the card** → switches to the routine's thread and goes to chat. Delete button (🗑) stops propagation so it doesn't navigate-on-delete.
- **Awake-only banner** on both the Routines page and the New-routine modal: *"Local routines only run while your computer is awake."* Sets expectations: qwe-qwe is local-first, no cloud cron daemon.
- **New-routine modal redesign** — matches the Claude Routines pattern:
  - Name (required, kebab-case)
  - Description (required, one-line)
  - Task prompt (optional long-form; falls back to description)
  - Frequency dropdown: Daily (with time picker), Hourly, Every 2/4 hours, Every 30 min, Once in…, Custom (raw DSL)
  - Live preview of the effective schedule (`= daily 09:00`)
  - Skip validation checkbox with clear copy about side effects
- **System tasks** (heartbeat, synthesis) still visible in the count but hidden from the main card grid — they're infra, not routines.

## Backend-quality improvements that landed alongside

### High-level tool: `telegram_notify_owner(text)`

Cron tasks like "send me error logs on telegram" were failing because the agent had no single tool for it. It tried to discover the bot token from secrets + the owner chat_id from log files + craft an HTTPS POST — 5+ wasted rounds of filesystem spelunking.

New core tool:
- Reads `telegram:bot_token` and `telegram:owner_id` from KV (already stored from the bot's activation flow)
- Calls `telegram_bot.send_message(owner_id, text, token)` (which handles MarkdownV2 → HTML → plain-text fallback + 4000-char chunking)
- Returns `"Sent. delivered to owner_id=12345 (234 chars)"` — the phrase `Sent` automatically passes the scheduler's dry-run confirmation check

Ship-ready replacement for the "use http_request to POST to api.telegram.org" path. The scheduler system prompt now directs routines at this tool explicitly.

### Scheduler dry-run prompt

Rewrote the system prompt that wraps `_execute_task` dry-runs. Was a vague two-liner; small models (GLM, Llama 70B) would burn 6-7 rounds on `shell find/ls/Get-ChildItem` discovering files whose paths were already known to the caller. New prompt:

- Names the log paths explicitly (`~/.qwe-qwe/logs/qwe-qwe.log`, `~/.qwe-qwe/logs/errors.log`)
- *"use read_file DIRECTLY, don't shell-find first"*
- Cheat-sheet for Telegram (`telegram_notify_owner`, not `http_request` + API)
- Final-reply rules enforce the dry-run confirmation contract (RU + EN phrases: `Отправил`, `Sent`, `message_id`, …)
- `max_tokens` 1024 → 2048 (was clipping summaries mid-sentence)

### Modal primitive: no backdrop-close

Filling a 30-second cron form and losing it to an accidental click on the dim backdrop was the top modal complaint. Overlay no longer has `data-modal-close` — close paths are now **Cancel button**, **× button**, **ESC**. Applies to every modal in the app, not just the new-routine one.

### Re-entry guard on modal actions

Clicking Create during a 30s dry-run used to trigger another POST (another LLM dry-run) on each impatient click → duplicate routines. Modal now sets `data-busy=1` synchronously on click, disables every footer button, shows a custom `busyLabel` (`"Creating… (running dry-run)"`). A 5-click hammer produces exactly one POST.

### `offer_skip` escape hatch

When dry-run fails with a confirmation-check miss on a complex task, server returns `offer_skip: true`. UI auto-checks the Skip Validation box, flashes an amber outline around it, and the next Create click saves without re-running dry-run. One-click recovery, no form re-typing.

## +6 tests

- `tests/test_scheduler_cron.py` grew from 12 → 14:
  - `test_routine_gets_dedicated_thread_on_create` — `sched.add()` returns a `thread_id`, `list_tasks` exposes the same id
  - `test_routine_thread_persists_across_multiple_firings` — two synthetic firings keep the same `thread_id` (one routine = one thread)
- `tests/test_telegram_notify_tool.py` (4) — tool is a core tool, rejects empty text, returns clean actionable error with no verified owner, happy-path round-trip invokes `telegram_bot.send_message`

Suite: 231 → **237 passing** (~40s local).

## Upgrade

```bash
pip install --upgrade qwe-qwe   # or re-run ./setup.sh
```

Migration 004 runs automatically on first DB touch. Existing routines continue to work — their `thread_id` column is NULL until next firing, which lazy-creates a thread and stamps it back. No manual action needed.
