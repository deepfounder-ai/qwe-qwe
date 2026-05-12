# Per-session token analytics — design

**Date**: 2026-05-11
**Status**: Draft (pending review)
**Author**: deepfounder-ai + Claude
**Related issues** (competitor pain points from analysis): NousResearch/hermes-agent#23461, #23270, #23419

---

## 1. Problem statement

qwe-qwe today has no per-thread visibility into LLM token usage or cost. The only counter is a global singleton `session_completion_tokens` in the KV store, which sums everything across all sessions and reveals nothing about which thread, routine, or auxiliary worker (synthesis, compaction, skill creator) is burning tokens.

User pain (mirrored across the agent ecosystem — see competitor issue analysis):

- **"How much did this conversation cost me?"** — no way to answer.
- **"Which routine is the heaviest spender?"** — `routine_runs` tracks duration and status but not tokens.
- **"Why did my OpenAI bill jump last week?"** — synthesis worker (which can chew through 10k+ tokens in a single nightly pass) is invisible.

This spec covers the **foundational** layer: per-run token + cost tracking with online pricing data, surfaced through the existing Sessions list with a drilldown modal. It does NOT cover budget enforcement (separate spec: budget cap on routines) or interrupt-resume semantics (separate spec).

---

## 2. Goals

1. Record token usage and dollar cost for **every** LLM call qwe-qwe makes — main agent runs, routine firings, night synthesis, compaction (folded into parent), and skill creator pipelines.
2. Display per-thread totals (`tokens in / tokens out / cost USD / run count`) directly in the existing Sessions list — no new top-level page required for MVP.
3. Provide drilldown into individual runs of a thread, with model, source, status, duration, tokens, and cost per run.
4. Keep dollar cost calculations current via auto-fetched pricing JSON (LiteLLM community source) with offline cache and air-gapped fallback.
5. Allow enterprise / custom-contract users to override per-model pricing without code changes.
6. Replace the existing `routine_runs` table with the new universal `agent_runs` table — one source of truth.

**Non-goals (deferred to v2 / separate specs):**

- Charts, time-series visualizations, dedicated Analytics page.
- Budget enforcement (caps, alerts when limit hit).
- Auto-resume after interrupt.
- STT / TTS / embedding cost tracking (different cost model — duration- or call-based, not tokens).
- Multi-currency or tax handling — USD only.

---

## 3. Architecture overview

```
                ┌─────────────────────────────────────┐
                │   Online pricing JSON (LiteLLM)     │
                └────────────────┬────────────────────┘
                                 │ 24h refresh (background thread)
                                 ▼
                ┌─────────────────────────────────────┐
                │   pricing.py                        │
                │   - cache + fallback chain          │
                │   - get_price(model, kind) → $/tok  │
                │   - compute_cost(model, in, out)    │
                └────────────────┬────────────────────┘
                                 │
        ┌────────────────┬───────┴───────┬───────────────────┐
        │                │               │                   │
   ┌────▼────┐    ┌──────▼─────┐  ┌─────▼─────┐      ┌──────▼─────┐
   │agent_   │    │ synthesis  │  │compaction │      │skill_      │
   │loop.py  │    │ .py        │  │(folded    │      │creator     │
   │(main)   │    │            │  │into       │      │pipeline    │
   │         │    │            │  │parent run)│      │            │
   └────┬────┘    └──────┬─────┘  └─────┬─────┘      └──────┬─────┘
        │                │              │                    │
        └────────────────┴──────────────┴────────────────────┘
                                 │
                                 ▼ INSERT / UPDATE
                ┌─────────────────────────────────────┐
                │   agent_runs table                  │
                │   (replaces routine_runs)           │
                └────────────────┬────────────────────┘
                                 │
                                 ▼ aggregation queries
                ┌─────────────────────────────────────┐
                │   GET /api/threads                 │
                │   GET /api/threads/{id}/runs       │
                │   GET /api/analytics/period         │
                │   GET /api/pricing/status           │
                │   POST /api/pricing/refresh         │
                └────────────────┬────────────────────┘
                                 │
                                 ▼
                          Web UI: Sessions list +
                          SessionRunsModal +
                          Topline widget +
                          Settings → Cost tracking
```

**New modules**: `pricing.py` (~140 lines).
**Modified modules**: `agent_loop.py`, `synthesis.py`, `skills/skill_creator.py`, `scheduler.py`, `server.py`, `db.py`, `config.py`, `static/index.html`.
**New migrations**: `008_agent_runs.sql` (creates `agent_runs`, copies `routine_runs`, drops `routine_runs`).
**New tests**: `tests/test_pricing.py`, `tests/test_agent_runs.py`, `tests/test_analytics_api.py` + integration test additions.

---

## 4. Data schema

### 4.1 `agent_runs` table (new — migration 008)

```sql
CREATE TABLE agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,
    cron_id         INTEGER,              -- NULL for user-initiated runs
    source          TEXT NOT NULL,        -- web|cli|telegram|routine|synthesis|skill_creator
    scheduled_at    REAL,                 -- set when cron_id IS NOT NULL
    started_at      REAL NOT NULL,
    finished_at     REAL,                 -- NULL while running or if aborted without finish
    duration_ms     INTEGER,
    status          TEXT NOT NULL,        -- running|ok|err|aborted|missed|skipped
    error           TEXT,
    result_preview  TEXT,                 -- first 200 chars of agent reply
    model           TEXT,                 -- "gpt-4o-mini" / "claude-3-5-sonnet-20241022" / etc
    provider        TEXT,                 -- "openai" / "anthropic" / "lmstudio" / etc
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_usd        REAL                  -- NULL if pricing unknown for this model
);

CREATE INDEX idx_agent_runs_thread_id  ON agent_runs(thread_id);
CREATE INDEX idx_agent_runs_started_at ON agent_runs(started_at);
CREATE INDEX idx_agent_runs_cron_id    ON agent_runs(cron_id);
CREATE INDEX idx_agent_runs_source     ON agent_runs(source);
```

**Migration `008_agent_runs.sql`** also does data migration:

```sql
BEGIN;

CREATE TABLE agent_runs ( ... );  -- as above
CREATE INDEX ...;                  -- four indexes

-- Copy existing routine_runs into agent_runs with source='routine'
INSERT INTO agent_runs (cron_id, thread_id, scheduled_at, started_at, finished_at,
                        duration_ms, status, error, result_preview, source)
SELECT cron_id, COALESCE(thread_id, ''), scheduled_at, started_at, finished_at,
       duration_ms, status, error, result_preview, 'routine'
FROM routine_runs;

DROP TABLE routine_runs;

COMMIT;
```

**Backward compatibility**: `routine_runs` is from v0.17.32, young enough that breaking-replacement is acceptable. Migration runs in a single transaction — atomic rollback on failure.

### 4.2 Status values

| status | meaning |
|---|---|
| `running` | row inserted at run start, not yet finalized |
| `ok` | `agent.run` finished, no error marker in reply |
| `err` | `agent.run` raised, or reply matched `_DRY_RUN_ERROR_MARKERS` |
| `aborted` | `abort_event` fired mid-run; `finished_at` may be NULL, tokens captured partially |
| `missed` | server was offline at the routine's scheduled fire time; tokens=0, cost=0 |
| `skipped` | per-thread fire lock was held; tokens=0, cost=0 |

### 4.3 Pricing override (KV)

Reuses existing `kv` table. No new schema.

```
key:   pricing_override_<model>
value: JSON {"input": 0.05, "output": 0.20}    # $/1M tokens (LiteLLM-compatible unit)
```

### 4.4 Pricing cache file

```
~/.qwe-qwe/pricing_cache.json
```

```json
{
  "fetched_at": 1731321600,
  "source_url": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
  "models": {
    "gpt-4o-mini": {
      "input_cost_per_token":  0.00000015,
      "output_cost_per_token": 0.00000060
    },
    "claude-3-5-sonnet-20241022": {
      "input_cost_per_token":  0.00000300,
      "output_cost_per_token": 0.00001500
    }
  }
}
```

Atomic write (write to `.tmp` then `os.replace`) to survive interrupted refreshes.

### 4.5 Edge cases in data

| Case | Storage |
|---|---|
| Aborted mid-run | `status='aborted'`, `finished_at` may be NULL; `input_tokens` set (request was sent), `output_tokens` reflects partial stream |
| Pricing unknown | `cost_usd IS NULL` (NOT zero — zero would imply free, which is misleading) |
| Local model (lmstudio/ollama) | `cost_usd = 0.0` (explicit zero — actually free) |
| Compaction inside agent_loop | Tokens summed into parent run's `input_tokens/output_tokens`; NO separate row |
| Synthesis pass | One `agent_runs` row per LLM call inside the pass; `thread_id` = thread being synthesized |
| Skill creator | One `agent_runs` row per `_run_pipeline()` invocation; all 5 steps' tokens summed into that row |
| Routine missed | One `agent_runs` row with `status='missed'`, `started_at=scheduled_at`, tokens=0 |

---

## 5. `pricing.py` module

New file, ~140 lines.

### 5.1 Module surface

```python
def get_price(model: str, kind: Literal["input", "output"]) -> float | None:
    """Return $/token for (model, kind). None if unknown."""

def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Compute total cost. None if either price unknown."""

def refresh_pricing(force: bool = False) -> bool:
    """Refresh from remote if cache stale or force=True. Thread-safe."""

def last_updated() -> Optional[float]:
    """Unix timestamp of last successful refresh, or None."""

def all_known_models() -> list[str]:
    """Sorted list of models with known pricing (for autocomplete)."""

def start_background_refresher() -> None:
    """Start the 24h refresh thread. Called from server.py on startup."""
```

### 5.2 Lookup chain (in `get_price`)

`get_price()` **never performs network I/O** — all remote fetches are owned by the background refresher (Section 5.5) and the explicit `POST /api/pricing/refresh` endpoint. This keeps the hot path predictable and bounded.

1. **KV override**: `db.kv_get(f"pricing_override_{model}")` — JSON parse, return matching field. Log warning on invalid JSON; continue chain.
2. **Local providers**: if model starts with `lmstudio:`, `ollama:`, or `local:` — return `0.0`.
3. **Loaded pricing dict** (memory cache, lazy-loaded via `_ensure_loaded()` on first call):
   - Memory dict hit → return.
4. **Disk cache** (`~/.qwe-qwe/pricing_cache.json`) — read once into memory on first `_ensure_loaded()`, then served from memory thereafter.
5. **Bundled fallback** dict (top-10 models hardcoded in `pricing.py`) — last resort if disk cache missing entirely.
6. Return `None` (caller writes `cost_usd = NULL`).

If the disk cache is older than `CACHE_TTL_SEC` (24h), the background refresher will replace it on its next tick — but `get_price()` itself keeps returning whatever's in memory until that happens. Never blocks.

### 5.3 Bundled fallback

```python
_BUNDLED_FALLBACK = {
    "gpt-4o-mini":                {"input": 0.00000015, "output": 0.00000060},
    "gpt-4o":                     {"input": 0.00000250, "output": 0.00001000},
    "gpt-4-turbo":                {"input": 0.00001000, "output": 0.00003000},
    "claude-3-5-sonnet-20241022": {"input": 0.00000300, "output": 0.00001500},
    "claude-3-5-haiku-20241022":  {"input": 0.00000080, "output": 0.00000400},
    "claude-3-opus-20240229":     {"input": 0.00001500, "output": 0.00007500},
    "deepseek-chat":              {"input": 0.00000014, "output": 0.00000028},
    "groq/llama-3.3-70b-versatile": {"input": 0.00000059, "output": 0.00000079},
    "groq/llama-3.1-8b-instant":  {"input": 0.00000005, "output": 0.00000008},
    "mistral-large-latest":       {"input": 0.00000200, "output": 0.00000600},
}
```

Top 10 by likely user popularity (OpenAI, Anthropic, DeepSeek, Groq, Mistral). Updated manually per release.

### 5.4 Network safety

`refresh_pricing()` does:

- `urllib.request.urlopen(url, timeout=10)` with explicit timeout.
- Reuses SSRF guard from `/api/knowledge/url` — block private / loopback / link-local IPs unless `QWE_ALLOW_PRIVATE_URLS=1`.
- Body size cap: 5 MB (LiteLLM JSON is ~500 KB).
- JSON parse in try/except — on any exception, log warning and return False without mutating cache.
- Atomic write to disk cache only on full success.

### 5.5 Background refresher

Started from `server.py`'s startup hook:

```python
def start_background_refresher():
    if not config.get("pricing_auto_update"):
        return
    def loop():
        while True:
            try:
                refresh_pricing(force=False)
            except Exception as e:
                _log.warning(f"pricing refresh failed: {e}")
            time.sleep(CACHE_TTL_SEC)  # 24h
    t = threading.Thread(target=loop, daemon=True, name="pricing-refresher")
    t.start()
```

Daemon thread — terminates with the process. Never blocks startup. Never crashes the server.

### 5.6 LiteLLM JSON normalization

LiteLLM's format:

```json
{
  "gpt-4o-mini": {
    "max_tokens": 16384,
    "max_input_tokens": 128000,
    "max_output_tokens": 16384,
    "input_cost_per_token": 0.00000015,
    "output_cost_per_token": 0.00000060,
    "litellm_provider": "openai",
    "mode": "chat",
    "supports_function_calling": true,
    "supports_vision": true
  },
  "sample_spec": { ... },
  "ft:gpt-4o-mini:my-org": { ... }
}
```

`_normalize_litellm()`:

- Skip entry if not a dict, or if `input_cost_per_token` / `output_cost_per_token` missing.
- Skip entries with `mode` in `{"embedding", "image_generation", "audio_transcription", "audio_speech"}` — they have different cost units.
- Skip the `sample_spec` meta-entry.
- Normalize keys: keep as-is (LiteLLM uses provider-prefixed names like `groq/llama-3.3-70b-versatile`; we match what `agent_loop` reports as `model`).

---

## 6. Instrumentation points

### 6.1 `agent_loop.py` — main loop (primary instrumentation)

**Prerequisite:** add a `cron_id: int | None = None` field to `TurnContext` (CLAUDE.md rule: "new per-turn state → put it on TurnContext"). `scheduler._check_and_run` populates it when firing a routine; everything else leaves it as None.

`run_loop()` already tracks tokens in `BudgetStats.add_tokens()` (called inside the streaming loop where `chunk.usage` arrives). Wire it to `agent_runs`:

```python
# Top of run_loop, before any LLM call:
run_id = db.insert_agent_run(
    thread_id=thread_id,
    source=ctx.source if ctx else "cli",
    started_at=time.time(),
    status="running",
    cron_id=ctx.cron_id if ctx else None,    # populated only when scheduler fires
    model=model,
    provider=provider_name,
)

# Finally block at end (success or exception):
try:
    cost = pricing.compute_cost(model, stats.input_tokens, stats.output_tokens)
except Exception:
    cost = None
db.finalize_agent_run(
    run_id,
    finished_at=time.time(),
    duration_ms=int((time.time() - start_ts) * 1000),
    status=final_status,                # ok | err | aborted
    error=err_msg,
    result_preview=(reply or "")[:200],
    input_tokens=stats.input_tokens,
    output_tokens=stats.output_tokens,
    cost_usd=cost,
)
```

**Compaction**: when `_run_compaction()` fires its own LLM call, it adds tokens to the SAME `stats` object via `stats.add_tokens()`. Those tokens automatically roll up into the parent run's totals at finalization. No separate row.

### 6.2 `synthesis.py` — night synthesis worker

Synthesis makes 1-N LLM calls per pass. Each call wraps with `db.insert_agent_run` / `db.finalize_agent_run` with `source='synthesis'` and `thread_id` = the thread being synthesized.

If a synthesis pass touches multiple threads, each thread's calls get their own runs (one per LLM call).

### 6.3 `skills/skill_creator.py` — pipeline

`_run_pipeline()` makes up to 5 LLM calls + retries. Wrap the entire pipeline call:

```python
run_id = db.insert_agent_run(
    thread_id=thread_id,
    source="skill_creator",
    started_at=time.time(),
    status="running",
    model=model,
    provider=provider_name,
)
agg_in = agg_out = 0
try:
    for step in (plan, tools_def, mapping, ddl, validate):
        step_tokens = run_step(...)
        agg_in += step_tokens.in
        agg_out += step_tokens.out
    final_status = "ok"
except Exception as e:
    final_status = "err"
    err = str(e)
finally:
    db.finalize_agent_run(
        run_id,
        finished_at=time.time(),
        duration_ms=...,
        status=final_status,
        error=err if final_status == "err" else None,
        result_preview=f"created skill: {skill_name}" if final_status == "ok" else None,
        input_tokens=agg_in,
        output_tokens=agg_out,
        cost_usd=pricing.compute_cost(model, agg_in, agg_out),
    )
```

### 6.4 `scheduler.py` — routine firings

Currently writes to `routine_runs`. After migration: writes to `agent_runs` with `cron_id` and `source='routine'`. Schema is compatible — same fields plus tokens/cost.

`missed` and `skipped` rows continue to be written eagerly by the existing logic:

```python
db.insert_skipped_run(
    cron_id=cron_id,
    thread_id=thread_id,
    scheduled_at=fire_time,
    reason='missed',  # or 'skipped'
)
# Internally: insert with status=reason, started_at=fire_time, tokens=0, cost=0
```

### 6.5 `db.py` — new helpers

```python
def insert_agent_run(
    thread_id: str, source: str, started_at: float,
    status: str = "running", cron_id: int | None = None,
    model: str | None = None, provider: str | None = None,
    scheduled_at: float | None = None,
) -> int:
    """Insert a new row. Returns the new run_id."""

def finalize_agent_run(
    run_id: int, finished_at: float | None, duration_ms: int | None,
    status: str, error: str | None = None, result_preview: str | None = None,
    input_tokens: int = 0, output_tokens: int = 0,
    cost_usd: float | None = None,
) -> None:
    """Update a previously-inserted run with final metrics.

    For aborted runs without a clean finish, finished_at and duration_ms
    can both be None; status='aborted' captures the partial-progress case.
    """

def insert_skipped_run(
    cron_id: int, thread_id: str, scheduled_at: float, reason: str = "missed"
) -> int:
    """For routines that were never executed (missed/skipped)."""

def get_runs_for_thread(thread_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Per-thread run history, newest first."""

def get_thread_totals(thread_id: str) -> dict:
    """Returns {input_tokens, output_tokens, cost_usd, run_count}."""

def get_period_totals(
    start_ts: float, end_ts: float, source: str | None = None
) -> dict:
    """Aggregated metrics for a time window. by_source breakdown included."""

def get_runs_for_routine(cron_id: int, limit: int = 50) -> list[dict]:
    """Per-routine run history (replaces old routine_runs query)."""
```

All helpers use parametrized SQL — no string interpolation.

---

## 7. API endpoints

### 7.1 Modified: `GET /api/threads`

(qwe-qwe's "threads" are what the UI labels as "Sessions" in the Sessions list — same data, different name.)

Add four fields to each row:

```json
[
  {
    "thread_id": "abc123",
    "title": "Project plan",
    "last_ts": 1731321600,
    "msg_count": 42,
    "input_tokens": 12340,
    "output_tokens": 4521,
    "cost_usd": 0.034,
    "run_count": 8
  }
]
```

Threads with no runs get `input_tokens=0, output_tokens=0, cost_usd=null, run_count=0`.

### 7.2 New: `GET /api/threads/{thread_id}/runs?limit=50&offset=0`

```json
[
  {
    "id": 42,
    "thread_id": "abc123",
    "cron_id": null,
    "source": "web",
    "started_at": 1731321600.123,
    "finished_at": 1731321601.456,
    "duration_ms": 1333,
    "status": "ok",
    "model": "gpt-4o-mini",
    "provider": "openai",
    "input_tokens": 320,
    "output_tokens": 180,
    "cost_usd": 0.000156,
    "result_preview": "Here is the plan...",
    "error": null
  }
]
```

### 7.3 New: `GET /api/analytics/period?days=30&source=all`

```json
{
  "start_ts": 1728729600,
  "end_ts": 1731321600,
  "total_input_tokens": 1230000,
  "total_output_tokens": 450000,
  "total_cost_usd": 4.21,
  "run_count": 312,
  "by_source": {
    "web":           {"input_tokens": ..., "output_tokens": ..., "cost_usd": ..., "run_count": ...},
    "telegram":      {...},
    "cli":           {...},
    "routine":       {...},
    "synthesis":     {...},
    "skill_creator": {...}
  }
}
```

Query params:

- `days` (int, default 30) — window size from now backwards.
- `source` (string, optional) — filter. Absent or empty = no filter (all sources aggregated). Otherwise must match one of the documented `source` enum values. The literal string `"all"` is treated the same as absent.

### 7.4 New: `GET /api/pricing/status`

```json
{
  "last_updated": 1731321600.0,
  "model_count": 423,
  "source_url": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
  "auto_update": true,
  "cache_age_sec": 7200
}
```

### 7.5 New: `POST /api/pricing/refresh`

Triggers `pricing.refresh_pricing(force=True)`. Returns:

```json
{"ok": true, "model_count": 423, "fetched_at": 1731321600}
```

On error: `{"ok": false, "error": "network timeout"}` with HTTP 502.

### 7.6 Modified: `GET /api/routines/{cron_id}/runs`

Now reads from `agent_runs WHERE cron_id=?`. Response shape additive — same fields as before plus the new `input_tokens / output_tokens / cost_usd / model / provider` fields. Existing UI code reading the old fields continues to work; new fields fill the new columns.

---

## 8. UI changes (`static/index.html`)

### 8.1 Sessions list — extended columns

Current columns: Title, Last activity, Messages.

New columns:

| Title | Last activity | Messages | Tokens (in/out) | Cost |
|---|---|---|---|---|
| Project plan | 2h ago | 42 | 12.3k / 4.5k | $0.034 |

- Row click → opens **SessionRunsModal** (new component).
- Tokens column: format with k/M suffixes (e.g. `1.2M / 450k`).
- Cost column: format as `$0.034`, `$2.41`, `—` (em-dash) if `cost_usd IS NULL`.

### 8.2 SessionRunsModal — new component

~150 lines of vanilla JS, fits existing single-file SPA style.

```
┌─────────────────────────────────────────────────────────────┐
│  Project plan                                          [×]  │
├─────────────────────────────────────────────────────────────┤
│  Total: 12.3k in / 4.5k out · $0.034 · 8 runs               │
├─────────────────────────────────────────────────────────────┤
│  Started              Source     Model       Tokens    Cost │
│  2026-05-11 14:32     web        gpt-4o-     1.2k /    $0.005 │
│    "Help me plan..."             mini        0.4k             │
│  ──────────────────────────────────────────────────────────  │
│  2026-05-11 14:35     synthesis  gpt-4o-     3.4k /    $0.001 │
│    (background)                  mini        0.2k             │
│  ──────────────────────────────────────────────────────────  │
│  2026-05-11 14:40     web        gpt-4o      8.0k /    $0.028 │
│    ⚠ aborted                                 3.9k             │
└─────────────────────────────────────────────────────────────┘
```

- Status colors: `ok` green, `err` red, `aborted` gray, `missed` yellow, `skipped` muted.
- Click row → expand `result_preview` + `error` (if any).
- Pagination: 50 runs per page, "Load more" button at bottom.
- Sort: newest first (by `started_at DESC`).

### 8.3 Topline widget on Sessions page

Above the sessions table:

```
Past 30 days:  1.23M tokens in · 450k out · $4.21  ·  312 runs
[All sources ▼]                            [Refresh pricing now]
```

- Selector: All sources / Web only / Routines only / Synthesis only / Skill creator / CLI / Telegram.
- "Refresh pricing now" button: POSTs `/api/pricing/refresh`, shows spinner, shows toast on result.

### 8.4 Settings → Cost tracking (new sub-section)

```
Cost tracking
─────────────
  Pricing source URL:  [https://raw.githubusercontent.com/BerriAI/...]
                                                            [Restore default]
  Auto-update prices:  [✓] every 24h
  Last updated:        2026-05-10 03:14  (423 models known)
                                                            [Refresh now]

  Per-model overrides (advanced):
    gpt-4o-mini    custom    $0.10 / $0.40 per 1M    [Remove]
    [+ Add override]
```

### 8.5 Routines page — Cost column

In the existing routines list, add a "Cost (30d)" column showing each routine's spend in the last 30 days. Computed via `SELECT SUM(cost_usd) FROM agent_runs WHERE cron_id=? AND started_at > ?`.

This is the most user-visible payoff: "this routine costs me $2/day, do I really need it firing every hour?"

---

## 9. Settings (new in `config.py::EDITABLE_SETTINGS`)

| key | type | default | range | description |
|---|---|---|---|---|
| `pricing_url` | str | `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` | — | URL for pricing JSON. Override for air-gapped mirrors. |
| `pricing_auto_update` | bool | `True` | — | Refresh pricing every 24h in background. |

Per-model overrides live in the `kv` table directly (key pattern `pricing_override_<model>`), not in `EDITABLE_SETTINGS` — too many possible keys.

---

## 10. Privacy + security

- **No data leaves the machine** as a result of this feature, except the pricing JSON fetch from GitHub (public URL, no user data sent — just a GET).
- **SSRF guard** on `pricing_url`: reuses the same allow-list as `/api/knowledge/url`. Private / loopback / link-local IPs blocked unless `QWE_ALLOW_PRIVATE_URLS=1`.
- **No tokens / cost data sent to telemetry** — this stays in local SQLite forever.
- **`result_preview` cap** (200 chars) prevents inadvertent secret capture from agent replies. The full reply still lives in `messages` table (separate concern).
- **Pricing cache atomic write** prevents corrupted-cache attacks (interrupted writes don't leave half-files).

---

## 11. Telemetry (opt-in only)

If telemetry is enabled (default: off), add ONE new event using the existing `feature_first_use` event family rather than a brand-new event type. Add a new value to the `FEATURES` enum:

```python
FEATURES = (..., "cost_tracking")  # new value
# Fired via: telemetry.track_event("feature_first_use", {"feature": "cost_tracking"})
```

The `anonymous_id` is stamped automatically by `track_event` — events themselves never carry it as a property (privacy contract). The event fires once per anonymous_id, the first time the user opens the SessionRunsModal. Helps measure adoption without leaking any cost data.

Bump `_CURRENT_CONSENT_VERSION` in `telemetry.py` so opted-in users get a re-consent banner.

---

## 12. Testing

### 12.1 `tests/test_pricing.py` (~30 cases)

- `_normalize_litellm()` on real LiteLLM sample (`tests/fixtures/litellm_sample.json` — copy ~20 entries from the real JSON).
- Fallback chain: cache hit / cache miss → remote / remote fail → bundled / unknown → None.
- KV override beats everything (including local provider zero).
- Local providers (`lmstudio:`, `ollama:`, `local:`) return 0.0.
- Unknown model returns None.
- Corrupt cache file → graceful re-fetch.
- Network error → stale cache served (not None).
- `pricing_auto_update=False` → no background thread started.
- `refresh_pricing(force=True)` ignores TTL.
- Concurrent `get_price()` from multiple threads → thread-safe (no race, no corruption).
- `compute_cost()` returns None if either price unknown.
- Bundled fallback includes the documented top-10 models.
- Atomic disk write (write-temp-then-rename) survives mid-write SIGTERM (simulated by patching `os.replace`).
- SSRF: `pricing_url` set to `http://127.0.0.1` → refused unless `QWE_ALLOW_PRIVATE_URLS=1`.
- Body size cap: response larger than 5 MB → rejected.

### 12.2 `tests/test_agent_runs.py` (~25 cases)

- `insert_agent_run` returns numeric id, row has `status='running'` and `started_at` set.
- `finalize_agent_run` updates `finished_at`, `duration_ms`, `status`, tokens, cost.
- `get_thread_totals` sums correctly across multiple runs (including some with `cost_usd IS NULL`).
- `get_runs_for_thread` honors limit + offset, orders by `started_at DESC`.
- `get_period_totals` filters by source correctly.
- Aborted run: `finished_at IS NULL`, tokens captured partially.
- Routine run: `cron_id` set, `source='routine'`.
- Missed run: tokens=0, cost=0.
- `cost_usd IS NULL` is treated as missing in SUM aggregation (uses COALESCE).
- Migration 008: fresh install creates table, runs from scratch.
- Migration 008: back-compat — populated `routine_runs` is copied into `agent_runs` with `source='routine'`.
- Migration 008: idempotent — applying twice is a no-op (well, an error on second drop, which the migration runner catches).
- Migration 008: transactional — if INSERT fails midway, `routine_runs` not dropped.

### 12.3 `tests/test_analytics_api.py` (~15 cases)

- `/api/threads` returns tokens/cost per thread.
- `/api/threads/{id}/runs` pagination works.
- `/api/threads/{id}/runs` for empty thread returns `[]`, not 404.
- `/api/analytics/period?days=30` correct aggregation.
- `/api/analytics/period?source=routine` filters.
- `/api/pricing/status` returns timestamp + model count.
- `/api/pricing/refresh` triggers fetch, returns new model count.
- `/api/pricing/refresh` on network error returns 502 with `ok:false`.
- `/api/routines/{cron_id}/runs` post-migration still works.

### 12.4 Integration tests (extend `test_integration.py`)

- Full turn through TestClient → `agent_runs` row created with correct tokens & non-null cost (when pricing known) or null cost (when pricing missing for mocked model).
- Synthesis call → row with `source='synthesis'`.
- Routine fire → row with `cron_id` set, `source='routine'`.
- Compaction inside a turn → tokens fold into parent run (no separate row).

### 12.5 Coverage

Coverage floor stays at 24% (current value in `pyproject.toml`). Adding ~70 new tests on top of ~495 should comfortably hold or improve coverage.

---

## 13. Rollout

### 13.1 Migration risk

`008_agent_runs.sql` is the highest-risk change (drops a table). Safeguards:

- Atomic transaction (CREATE + INSERT + DROP all in one BEGIN/COMMIT).
- CI smoke test: apply on a snapshot DB containing real `routine_runs` data → verify row count matches in `agent_runs`.
- Migration runner already handles per-file transactions (see `migrations/README.md` and existing `_apply_migrations` logic in `db.py`).

If migration fails on a user's machine: their DB stays at schema_version=7 (pre-008). They get a server startup error pointing to logs. No data loss. They can downgrade qwe-qwe and continue.

### 13.2 Backward compatibility

- API consumers reading `/api/routines/{cron_id}/runs` continue to work — additive change.
- The old `routine_runs` table is gone — anything that referenced it by name will break. Grep confirmed: only `scheduler.py` and tests reference it. All updated in this rollout.
- Threads with no runs (pre-existing data) show `0 / 0 tokens, — cost`. UI handles this gracefully.

### 13.3 Performance

- Hot path overhead: 1 INSERT at run start, 1 UPDATE at run end. ~50µs each on SQLite local. Negligible vs LLM latency.
- `GET /api/threads` query: needs SUM/GROUP BY over `agent_runs`. Indexed on `thread_id`, so even 100k rows is sub-millisecond.
- `GET /api/analytics/period`: SUM over time window with optional source filter, indexed on `started_at`. Same story.

### 13.4 Release plan

- Version bump: v0.18.x → v0.19.0 (minor — new schema, new module, additive APIs).
- `RELEASE_NOTES.md`: "Cost tracking + per-session analytics" entry.
- `docs/COST_TRACKING.md`: new public-facing doc — how to read the stats, where to find the URL for an air-gapped mirror, how to add a per-model override.
- `CLAUDE.md`: add "Cost tracking" sub-section under Architecture, mirroring the structure of the existing "SQLite migrations" and "Telemetry" sections.

---

## 14. Open questions / known limitations

1. **Streaming usage capture timing**: Some providers send `usage` only in the final `[DONE]` chunk. If the connection drops before that chunk arrives, `input_tokens` / `output_tokens` may be zero even though the request was billable on the provider's side. Mitigation: when `status='aborted'`, surface a small "tokens may be incomplete" hint in the UI. Long-term: estimate from content length as a floor.

2. **Provider-prefixed model names**: LiteLLM uses `groq/llama-3.3-70b` but qwe-qwe sometimes reports just `llama-3.3-70b`. We need to ensure model strings line up. Risk: pricing lookup misses, `cost_usd IS NULL`. Mitigation: extend `provider_kind()` lookup to map back to LiteLLM's prefixed names — a small dict in `pricing.py`.

3. **Custom fine-tuned models**: `ft:gpt-4o-mini:my-org:v1` — LiteLLM has the base model price but not the fine-tuned suffix. Resolution: in `get_price`, if exact match misses, strip `ft:.*:` prefix and retry the base model. Logged as a warning so users know we're using base pricing.

4. **Pricing JSON schema drift**: LiteLLM could rename `input_cost_per_token` someday. `_normalize_litellm()` skips entries silently if fields are missing — we'd lose pricing for everything in a worst case, falling back to bundled top-10. Acceptable risk; we'd notice from increased `cost_usd IS NULL` rates.

---

## 15. Definition of done

- [ ] `pricing.py` module implemented and ~30 unit tests passing.
- [ ] Migration `008_agent_runs.sql` written, idempotent on legacy installs, atomic on rollout.
- [ ] All five instrumentation points wired (`agent_loop`, `synthesis`, `skill_creator`, `scheduler`, finalization in `db.py`).
- [ ] New / modified API endpoints return the documented shapes; ~15 API tests passing.
- [ ] Sessions list shows Tokens + Cost columns; SessionRunsModal opens on row click; topline widget on top of page.
- [ ] Settings → Cost tracking section functional.
- [ ] Routines page shows Cost (30d) column.
- [ ] `docs/COST_TRACKING.md` written; `CLAUDE.md` updated.
- [ ] `RELEASE_NOTES.md` entry written.
- [ ] `ruff check .` clean; `pytest tests/` all green; coverage ≥ 24%.
- [ ] `python scripts/check_js.py` clean (new JS in `static/index.html`).
