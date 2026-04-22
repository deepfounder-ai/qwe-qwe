# v0.17.14 — Knowledge ingest: real progress + recent activity + no more zombie tasks

Three fixes to the knowledge ingestion feedback loop. Previously you clicked "paste URL", saw a chip flash `indexing 1/1` for half a second, and that was it. No idea what just happened, no trace afterwards, and every URL/file batch run left a "running" zombie in the internal task registry forever.

## 🔧 Fix A — zombie tasks

`server._run_url()` called `tasks.register("knowledge_url", …)` at start but **never** called `tasks.update(id, "done")` at the end. Every URL import leaked a "running" row into `tasks._results`, which then got injected into the agent's system prompt via `tasks.get_running()` ("background tasks running right now: knowledge_url — Fetching https://…"). The agent started each turn believing there was a pending fetch that had actually completed hours ago.

Fixed: `_run_url` and `_run_knowledge_index` now both call `tasks.update(task_id, "done"/"error", summary)` in a `finally:` block so even crashes close the row.

## 🆕 Fix B — real progress strip

A proper strip appears below the upload zone while indexing runs:

```
⟳ FETCHING…  https://youtube.com/watch?v=dQw4w9WgXcQ   [████████▁▁▁▁]  1 / 1
```

- **Phase label** mapped from raw server phase: `fetch / convert / cpu / gpu / index / done` → `Fetching / Converting / Chunking / Embedding / Indexing / Done`.
- **File name** from current URL or filename being processed.
- **Determinate bar** — width = `current / total`.
- **Counter** in monospace, updates live as `pollKbStatus` ticks every 1.5s.

The tiny `indexing 1/1` chip in the page header is still there as a quick glance.

## 🆕 Fix D — Recent activity card

New card below the upload zone + progress strip. Shows the last 5 completed indexings (up to 20 retained server-side) with:

| Kind icon | Label | Chunks | Duration | Time ago |
|---|---|---|---|---|
| 🌐 globe | `Rick Astley - Never Gonna Give You Up` | 4 ch | 3.4s | 12s ago |
| 📦 package | `5 files` | 42 ch | 18.2s | 2m ago |
| 📘 book | `research.pdf` | 18 ch | 5.1s | 1h ago |

Status colours: green dot for done, amber for partial (some errors), red for error. Rows include `title` tooltip with full URL/path.

### Server plumbing

- New module-level `_knowledge_history: deque(maxlen=20)` (newest first).
- `_push_history(entry)` called from all three worker completion paths.
- New endpoint `GET /api/knowledge/recent` returns `{items: [...]}`.
- Each entry carries `{kind, label, url, status, chunks, duration_sec, converter, errors, ts}`.

### UI plumbing

- `state.kbRecent` populated by `loadKbRecent()` on every Memory view load and after each `pollKbStatus` run.
- `renderKbProgress()` + `renderKbRecent()` render into the Memory view between upload zone and the grid.
- "2 total" counter in the card header so you can see if the history is trimming.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

Now when you paste a URL:

1. Progress strip shows `Fetching…  URL  [▓▓▓▓▁▁▁▁]  0/1` immediately.
2. Flips to `Indexing… path  [▓▓▓▓▓▓▓▓▓▓]  1/1` when done.
3. Strip disappears, Recent activity gets a new row: `🌐 Rick Astley — … 4 ch 3.4s just now`.
4. The **tasks registry** is clean — no leaked "running" entries, the agent's system prompt stops hallucinating about pending fetches.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
