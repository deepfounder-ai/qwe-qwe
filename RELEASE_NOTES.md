# v0.17.31 — Routines, round 2: every fire visible, pauseable, organised

v0.17.30 shipped the routine ↔ thread binding but left enough rough edges that the UX was still "create a routine, watch nothing happen, hope for the best". This release is the finishing work: every firing is visibly accounted for, you can pause a routine without deleting it, threads organise into folders, and a new hard-deny list blocks the agent from wiping out its own data.

## 🔥 Routines — finishing the UX

**Auto-fire on create + live status poll**. POST /api/cron now kicks agent.run in a background thread immediately after save. HTTP returns in ~300ms with `thread_id`; UI auto-navigates into the routine's chat thread, and every 2 seconds silently polls history + cron state (surgical thread-header swap, no full render, so the composer textarea and chat scroll are preserved). Four-state badge: `running` (pulsing yellow while agent.run is in flight) / `active` (scheduled & healthy) / `last run failed` (red) / `paused` (gray). No more "thread just sits there empty" confusion.

**Per-thread fire serialisation**. Two concurrent fires on the same routine thread were producing the "3 user messages, no reply" state — each agent.run saved its own copy + raced over the assistant write. Added a non-blocking per-thread lock in `_execute_routine`: second caller cleanly no-ops rather than queuing up a runaway backlog. POST /api/cron/{id}/run surfaces this as `{already_running: true, hint: "..."}` so the UI toasts "already running — wait for current turn" instead of silently swallowing the click.

**Fire metrics now count for every trigger**. Previously only the scheduler loop bumped `run_count` / `last_run` / `last_status`. Auto-fires and manual Runs were invisible to the metrics — users saw `runs: 0, last_run: ""` after actually successful firings. Moved the metrics UPDATE into `_execute_routine`'s finally block; system tasks (heartbeat, synthesis, reminders) keep their own metrics path in `_check_and_run`.

**Pause / Resume toggle**. New `POST /api/cron/{id}/toggle` (empty body toggles, `{enabled: bool}` sets explicit). Routine cards grow a ⏸ Pause button that dims the card, hides Run-now, tags the title `paused`. Same control in the thread header. Scheduler loop honours `enabled=0` and skips paused rows until resumed.

**Tight routine ↔ thread lifecycle**. Deleting a routine now deletes its thread (cascade via `threads.delete`); deleting a routine's thread cascades the other way via `scheduler.remove_by_thread`. System tasks are skipped so the sidebar can't accidentally kill heartbeat. UI shows the cascade explicitly in the confirm dialog.

**Richer frequency picker in the New-routine modal**. Seven modes — Daily (with time), Weekly (day chips + Weekdays/Weekends/All/Clear quick-buttons + time), Every N days / hours / minutes, Once, Custom DSL — each with live schedule preview under the form (`= mon,wed,fri 14:00`).

**Parser additions**: `weekdays HH:MM`, `weekends HH:MM`, `mon HH:MM`, `mon,wed,fri HH:MM`, `every N days HH:MM`. Russian aliases too: `будни`, `выходные`, `пн,ср,пт`. `_check_and_run` re-parses on each fire so Mon,Wed,Fri hops to Wed (not next Mon) after a Mon firing.

**telegram_notify_owner core tool**. Cron tasks like "send me an error summary to Telegram" were failing because the agent had no single tool for it — it'd try to stitch `secret_get + chat_id discovery + http_request to api.telegram.org` together and burn 5+ rounds. New tool: one call using the already-configured bot token + verified owner_id. Returns `"Sent. delivered to owner_id=... (N chars)"` which passes the dry-run send-confirmation check automatically.

**Dry-run prompt rewrite**. `_execute_task`'s system prompt is now specific: names the log file paths, instructs `read_file` directly (don't shell-find), points at `telegram_notify_owner` for sends, lists `tool_search` for extended tools, and enforces a confirmation-phrase rule for send tasks (RU + EN). `max_tokens` 1024 → 2048.

**Fire-divider**. Repeated task messages (auto-fires) collapse into a compact `── fired · 22:29 ──` horizontal rule instead of stacking duplicate user bubbles. User-typed corrections between fires render normally.

**Tool-name fix**. WS `reply.tools` is a flat list of name strings; the client was dereferencing `.name` on each string and falling back to literal `'tool'` — every reloaded tool row showed "tool / tool / tool" under an "OTHER" category. Now accepts both string and dict shapes.

**Section reorder in assistant messages**. Tool-call list now renders BEFORE the thinking block so the concrete "what happened" summary is visible first.

**Backdrop click no longer discards the modal**. Filling a 30-second cron form and then losing it to an accidental click on the dim backdrop was the top modal complaint. Close paths: Cancel button, × button, ESC.

**Dry-run kept but no longer default in UI**. The `skip_dry_run=true` path is now the only path the Web UI ever takes. Dry-run validation remains available to API callers but isn't surfaced as a checkbox — instant save, first run is visible in the thread as-it-happens. UI is honest about what's going on.

## 📁 Thread folders

Thread sidebar grows user-defined folders. Click 📦 on a thread row → modal with a datalist of existing folders + free-text input (create new on the fly). Empty input ungroups. Folders render as collapsible groups between PINNED and RECENT, with chevron + count. Collapse state persists in localStorage per folder name.

Data model: a folder is just a string under `threads.meta.folder`. No schema change, no migration — distinct folder list derived on read. Sort is case-insensitive. Server exposes `POST /api/threads/{id}/folder` + `GET /api/folders`.

## 🛡 Integrity hard-denies

Added a proactive hard-deny list for operations that would wipe qwe-qwe's own operational state with no recovery. No confirmation dialog — these are simply refused at the tool-dispatch gate.

**Shell** (`_check_shell_safety`):
- `rm` (any flag combo) of `qwe_qwe.db` or vault files
- `rm -r` of `~/.qwe-qwe/` (+ `memory/`, `vault/` subdirs)
- `rm -r` of `.git` anywhere
- Redirect-truncate (`>`) onto the DB or vault
- `dd of=<agent file>`
- `sqlite3 qwe_qwe.db 'DROP ...'` or `DELETE FROM messages`
- Word-boundary negative lookahead on `.qwe-qwe` so lookalike dirs (`~/.qwe-qwe-backup`) don't false-positive

**write_file** (`_resolve_path(for_write=True)`):
- `qwe_qwe.db` + WAL/SHM sidecars
- Vault files (encrypted secrets)
- Anything under `~/.qwe-qwe/memory/` (Qdrant binary storage)
- Anywhere under a `.git/` directory
- qwe-qwe's own Python source tree (.py / pyproject.toml in the package dir) — overridable with `QWE_ALLOW_SELF_MODIFY=1` for interactive dev sessions where the user wants the agent to refactor qwe-qwe

Qdrant and SQLite continue writing their own files normally — the hard-deny only catches paths that come through the agent-tool layer. Documented intent: "the integrity block is a shield on the agent's hands, not on qwe-qwe's own runtime".

## 🧪 Test coverage

Suite grew 235 → **307 tests** since v0.17.29. Additions:

- `tests/test_scheduler_cron.py` (17 → 19) — pause/resume toggle end-to-end: toggle flips state, `_check_and_run` skips disabled rows, toggle on → fires again.
- `tests/test_schedule_parser.py` (23) — every new DSL branch: weekly day-of-week incl. Russian aliases (`будни`, `выходные`, `пн,ср,пт`), every-N-days, reschedule-hops-to-next-matching-day regression, garbage-rejects matrix.
- `tests/test_telegram_notify_tool.py` (4) — core-tool membership, empty-text rejection, no-verified-owner actionable error, happy-path sends via `telegram_bot.send_message`.
- `tests/test_thread_folders.py` (8) — set/clear (both `""` and `None`), trim + 60-char cap, missing-id error, `list_folders` distinct+sorted case-insensitive, `list_all` exposes meta.folder.
- `tests/test_integrity_blocks.py` (24) — 13 shell block cases (with fault-injection verifying each fires), 3 read-still-allowed, word-boundary vs `.qwe-qwe-backup`, 7 write-path cases including the `QWE_ALLOW_SELF_MODIFY` override path.
- `tests/test_routine_endpoints.py` (10, **new**) — HTTP integration: POST /api/cron auto-fires + correct shape, reject bad schedule, /run returns thread_id + fires background agent.run, /run returns `already_running: true` under held lock, 404 on missing id, /toggle implicit + explicit, /threads/{id}/folder roundtrip + 404, `list_tasks` shape includes every key the UI reads.

## Upgrade

```bash
pip install --upgrade qwe-qwe   # or re-run ./setup.sh
```

Migration 004 (routine thread_id column) runs automatically on first DB touch. Existing routines that predate it lazy-create their thread on next firing. No manual action needed.

## Known follow-ups

- Drag-and-drop threads between folders (currently: modal with text input)
- Streaming routine progress to the WS client while it's running (currently: 2s silent poll)
- Per-routine model override ("use Sonnet for this one") — UI hook exists, server plumbing TBD
