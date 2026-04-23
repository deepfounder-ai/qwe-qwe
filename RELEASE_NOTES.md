# v0.17.29 — scheduler analytics, cron-creation that actually works, Telegram groups unblocked

Three compound bugs shipped in v0.17.28 that each looked like a different problem to the user but were rooted in the same place: a feature landed without enough plumbing. This release fixes all three and backfills 25 tests that would have caught them.

## 📅 Scheduler got real analytics

Before: `TOTAL RUNS: 0` no matter how long jobs had been firing. Cards showed `next` but no `last run`, no status, no duration. The `scheduled_tasks` table had exactly one metric column (`last_run`) and the execution loop never even wrote to it on success — only on completion of a repeat cycle.

After: every run writes a full row of metrics.

- **`migrations/003_scheduled_tasks_metrics.sql`** — adds `run_count`, `last_status` (`ok`/`err`), `last_error`, `last_duration_ms`, `last_result`. Safe on legacy installs that predate the migrations system (guards with `CREATE TABLE IF NOT EXISTS` first).
- **`_check_and_run`** now times the execution, catches exceptions so one bad job doesn't freeze the loop, classifies the outcome via the new `_looks_like_error` heuristic (shared with dry-run), and updates all five columns in one statement.
- **`list_tasks()`** returns the full shape plus a precomposed `last` field (`"ok · 42ms"` / `"err · <short>"`) that the UI can render without caring about internals.
- **UI** stats row replaces the always-0 "JOBS" with **LAST ERRORS** (red when >0, grey "all healthy" otherwise). Each card now shows `last run` timestamp + `status` colour-coded + real `runs` count.

## 🔧 New-scheduled-job modal that doesn't create duplicates

Three different ways this was broken in v0.17.28:

1. **Schedule format mismatch.** The modal offered 5-field cron presets (`0 9 * * *`) but the parser only understood qwe-qwe's DSL (`every 1h`, `daily 09:00`). Every submit rejected with `{"error": "Can't parse schedule"}` — and the UI didn't check the response, so "job created" toasted while nothing was saved.
2. **No re-entry guard.** Clicking Create triggered a 30-120 second LLM dry-run. During the wait, the modal stayed open and the button stayed enabled. Impatient clicks = duplicate jobs.
3. **No escape hatch for complex tasks.** Dry-run hit `max_rounds=5` on anything moderately real (e.g. "send error logs to telegram" needed 6 rounds just to explore the filesystem) and its "did-send confirmation" check was English-only, rejecting tasks where the agent confirmed in Russian.

All fixed:

- **Modal primitive** (`wireModal`) now sets `data-busy=1` on the clicked action, disables every action button in the footer, and shows a custom `busyLabel` (Create → `"Creating… (running dry-run)"`). The synchronous re-entry guard means even a 5-click hammer produces exactly one POST. If the handler returns `false` (validation error) or throws, buttons re-enable for retry.
- **Schedule presets** rewritten to the DSL the parser actually speaks (`every 1h`, `every 4h`, `daily 09:00`, `in 30m`, `in 2h`).
- **`skip_dry_run`** checkbox wired through from modal → `POST /api/cron` → `scheduler.add(..., skip_dry_run=bool)`. Trivial tasks now save in ~200ms.
- **Dry-run tuning**: `max_rounds` bumped 5 → 8; `"task completed (max rounds)"` removed from the strict failure markers (complex ≠ broken); send-task confirmation check recognises 9 RU + 8 EN phrases (`отправил`, `успешно`, `готово`, `delivered`, `posted`, `message_id`, …).
- **Auto-skip offer**: when dry-run fails with a confirmation-check miss, server returns `offer_skip: true`. UI auto-checks the Skip Validation box, flashes an amber outline around it, and the next Create click saves without re-running — no form re-typing.

## 💬 Telegram groups unblocked (two compound bugs, one symptom)

User reported: "bot added to group, group whitelisted in settings, I write there, bot doesn't see anything." Two bugs stacking up:

1. **Mode dropdown semantics mismatch.** The v2 UI offered `disabled / allowlist / any` as `group_mode`, but `_handle_group_message` only checked for `all` or `mention`. Every saved value from the UI was unknown → `should_respond` stayed False → bot silently ignored every group message with no log line. The "respond to all / mentions only" option that users remembered was simply gone from the UI.
2. **Allowed-groups int coercion missing.** The UI sent chat IDs as strings (`["-1003803066123"]`) and `set_allowed_groups` just `json.dumps`'d them through. Telegram delivers `chat_id` as int. `chat_id not in ["-100..."]` was always True → silently ignored even with the correct mode.

Fixes:

- **`group_mode`** canonicalised to `all` / `mention` / `off`; default changed from `"mention"` to `"all"` (what users actually expect for a personal bot). Legacy values (`any`, `disabled`, `allowlist`) auto-heal on read AND on save via `_GROUP_MODE_ALIASES`. Unknown modes fall back to `all` — safer to over-respond than to go silent.
- **`get_allowed_groups()` / `set_allowed_groups()`** coerce to `int` on both sides, filter garbage (non-numeric entries dropped). Legacy stringified lists heal on first read.
- **Group handler** explicitly handles `"off"` (return without touching chat) and treats any unknown mode as `"all"`.
- **UI dropdown** rewritten with human labels: "all messages" / "mentions & replies only" / "off (ignore groups)". "Allowed groups" description clarified — empty = all groups allowed, mode still decides reply.

Existing installs heal on first read of each affected KV key. No manual migration needed.

## 🧪 +25 tests

- `tests/test_telegram_groups.py` (13) — default is `all`, every legacy alias normalises to canonical on read AND write, unknown modes safe-default, allowed-groups int coercion on set + get, empty-list = all-allowed, junk filtering, and a compound regression: `chat_id in healed_allowed_groups` must be True (the symptom).
- `tests/test_scheduler_cron.py` (12) — skip_dry_run saves fast, bad schedule clean error, `every Nh` format accepted, list_tasks returns every expected metric field, _check_and_run stamps ok/err states with run_count+1 + duration + last_run, last_status=err when output matches an error marker, max-rounds no longer a failure, RU + EN send confirmations pass, `offer_skip` path, legacy-schema heals via migration.

Suite: 206 → **231 passing** (~44s local).

## Upgrade

```bash
pip install --upgrade qwe-qwe   # or re-run ./setup.sh
```

Migration runs automatically on first DB touch. Stringified `allowed_groups` and legacy `group_mode` values heal transparently — if your bot was silent in v0.17.28 with groups configured, it'll start responding as soon as you restart the server.
