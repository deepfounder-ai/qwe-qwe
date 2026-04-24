# v0.17.32 — Routine run history + offline-gap detection

Users reported: when the server is off, scheduled firings silently vanish — no way to tell a routine "fired 5 times this week" from one that "should have fired 5 times but the laptop was closed". This release adds honest per-fire history with status=ok/err/missed/skipped, detects offline gaps at startup, and surfaces the timeline as a sparkline on each routine card.

## New: per-fire history table

`migrations/005_routine_runs.sql` adds `routine_runs` with one row per fire attempt:

```
cron_id          → which routine
scheduled_at     → when the fire was supposed to happen
started_at       → when agent.run actually began (NULL for missed)
finished_at      → when agent.run returned (NULL for missed/running)
duration_ms      → wall-clock duration
status           → ok / err / missed / skipped
error            → error message if status=err or skipped/missed reason
result_preview   → first 500 chars of the reply
thread_id        → link to the routine thread (same value on every row)
```

Indexed by `cron_id` and `scheduled_at`.

## Every fire logs a row

`_execute_routine` in `scheduler.py` now brackets the agent.run call with a `_log_run` write:

- Successful run → `status=ok`, duration/result captured
- Raised exception or error-marker in reply → `status=err`, error text stored
- Per-thread lock held (concurrent fire attempt) → `status=skipped` instead of silent no-op

Same aggregation fields on `scheduled_tasks` (last_run, last_status, last_duration_ms, last_result, run_count) continue to update so the card stats are cheap to read — `routine_runs` is for the timeline view.

## Startup detects offline-gap misses

New `scheduler.detect_missed_runs()` runs once when the scheduler loop starts. Strategy:

1. Read the `scheduler:last_check` KV stamp (updated every loop tick)
2. For every enabled user-created routine with a repeating schedule:
   - Walk backward from `next_run` by `interval` steps
   - For each scheduled slot that falls inside `(last_check, now)`, insert a `status=missed` row
3. Cap at 10 missed rows per routine — a 24h outage on a 10-min routine won't create 144 entries

System tasks (`__heartbeat__`, `__synthesis__`) are skipped — they self-correct on the next tick and their theoretical misses would just be noise.

If there's no `last_check` stamp yet (fresh install), `detect_missed_runs` just stamps "now" without inventing fake history.

## API + UI

**New endpoint**: `GET /api/cron/{id}/runs?limit=20` — returns the N most recent run rows for a routine, newest first. UI calls this for the detail view.

**`/api/cron` list** now includes `recent: {counts, series}` per row:
- `counts`: `{ok: N, err: N, missed: N, skipped: N}` over the last 20 fires
- `series`: `["ok", "missed", "ok", "err", ...]` oldest→newest for the sparkline

**Sparkline on routine cards** — under the `last ok · Nms · M runs` line, a row of colored dots:
- 🟢 green = ok
- 🔴 red = err
- 🟡 yellow = missed (server was offline)
- ⚪ grey = skipped (concurrent fire rejected)

Tooltip on the sparkline shows the counts breakdown. When there's at least one missed fire, a `N missed` label appears next to the dots in warn-colored mono font.

## Tests

`tests/test_routine_runs.py` (+8):

- Successful fire → `status=ok` row with duration
- Crashed fire → `status=err` row with error text
- Concurrent fire → one `status=ok` + one `status=skipped`
- First-boot `detect_missed_runs` is a no-op (no `last_check` stamp)
- 5.5h gap on a 1h routine → 5 `status=missed` rows at the right scheduled times
- 24h gap on a 10-min routine → exactly 10 rows (capped)
- Heartbeat/synthesis left alone by missed-detection
- `list_tasks` exposes the `recent` counts/series correctly

Suite: 307 → **315 passing** (~45s).

## Upgrade

```bash
pip install --upgrade qwe-qwe   # or re-run ./setup.sh
```

Migration 005 runs automatically on first DB touch. Existing routines start with empty history; their next firings populate the table normally.

## Known follow-ups

- Runs detail drawer in UI (click a dot → see the full `routine_runs` row with error text, duration, linked thread turn)
- Configurable missed-run cap per routine (currently hardcoded 10)
- Missed-run catch-up mode for send-type routines where you want a single "summary of the missed period" fire instead of silent misses
