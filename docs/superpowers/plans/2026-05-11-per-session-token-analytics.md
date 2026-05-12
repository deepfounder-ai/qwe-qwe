# Per-Session Token Analytics Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-run token & USD-cost tracking across every LLM call site in qwe-qwe, surface totals + drilldown in the Sessions list, and replace `routine_runs` with a unified `agent_runs` table fed by an online pricing source.

**Architecture:** A new module `pricing.py` fetches LiteLLM's pricing JSON and caches it locally (chain: KV override → memory → disk → bundled). A new `agent_runs` table (replacing `routine_runs`) records one row per LLM-call site (main loop, synthesis, skill creator, scheduler). New `db.py` helpers (`insert_agent_run` / `finalize_agent_run`) bracket each call. Sessions list gains `Tokens` + `Cost` columns plus a `SessionRunsModal` drilldown. Settings gains a Cost-tracking section. Routines page gains a `Cost (30d)` column.

**Tech Stack:** Python 3.11+, SQLite (WAL), urllib (no new deps), FastAPI (existing), single-file vanilla-JS SPA (`static/index.html`), pytest. LiteLLM pricing JSON: <https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json>.

**Spec:** [`docs/superpowers/specs/2026-05-11-per-session-token-analytics-design.md`](../specs/2026-05-11-per-session-token-analytics-design.md)

---

## File Structure

### New files

- `pricing.py` — online pricing fetcher, lookup chain, cost computer (~140 lines).
- `migrations/008_agent_runs.sql` — creates `agent_runs`, copies `routine_runs`, drops it.
- `tests/test_pricing.py` — ~30 unit tests for pricing.
- `tests/test_agent_runs.py` — ~25 unit tests for the new db helpers + migration.
- `tests/test_analytics_api.py` — ~15 unit tests for the new endpoints.
- `tests/fixtures/litellm_sample.json` — trimmed snapshot of LiteLLM's JSON (~20 entries).
- `docs/COST_TRACKING.md` — public-facing doc.

### Modified files

- `turn_context.py` — add `cron_id: int | None = None`.
- `db.py` — add 6 helpers (`insert_agent_run`, `finalize_agent_run`, `insert_skipped_run`, `get_runs_for_thread`, `get_thread_totals`, `get_period_totals`, `get_runs_for_routine`).
- `agent_loop.py` — bracket each `run_loop()` with `insert_agent_run` / `finalize_agent_run`.
- `synthesis.py` — bracket each LLM call.
- `skills/skill_creator.py` — bracket whole `_run_pipeline()`.
- `scheduler.py` — replace `_append_run` / `list_runs` to use `agent_runs`. Pass `cron_id` via ctx.
- `server.py` — extend `/api/threads`, add 4 new endpoints, start pricing refresher on boot.
- `config.py` — add `pricing_url` + `pricing_auto_update` to `EDITABLE_SETTINGS`.
- `telemetry.py` — add `"cost_tracking"` to `FEATURES`, bump `_CURRENT_CONSENT_VERSION`.
- `static/index.html` — Sessions list columns, `SessionRunsModal`, topline widget, Settings section, Routines page column.
- `tests/test_integration.py` — assertions on `agent_runs` rows.
- `tests/test_migrations.py` — coverage for migration 008.
- `CLAUDE.md` — new "Cost tracking" sub-section under Architecture.
- `RELEASE_NOTES.md` — entry for the new minor version.

---

## Implementation Phases

The 29 tasks group into 6 phases. Each phase is independently testable. Phases 1-2 are pure foundation (no user-visible change). Phase 3 starts emitting rows. Phase 4 exposes the data. Phase 5 lights up the UI. Phase 6 wraps with telemetry + docs.

---

## Phase 1 — Foundation

### Task 1: Add `cron_id` to TurnContext

**Files:**
- Modify: `turn_context.py`
- Test: `tests/test_turn_context.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_turn_context.py`:

```python
def test_turn_context_has_cron_id_default_none():
    from turn_context import TurnContext
    ctx = TurnContext()
    assert ctx.cron_id is None

def test_turn_context_accepts_cron_id():
    from turn_context import TurnContext
    ctx = TurnContext(cron_id=42)
    assert ctx.cron_id == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_turn_context.py::test_turn_context_has_cron_id_default_none tests/test_turn_context.py::test_turn_context_accepts_cron_id -v`
Expected: FAIL (`TypeError: TurnContext.__init__() got unexpected keyword argument 'cron_id'`).

- [ ] **Step 3: Add the field**

In `turn_context.py`, inside `@dataclass class TurnContext:`, after the `session_id` field (around line 71), add:

```python
    # ── Scheduler binding (set by scheduler._check_and_run; None otherwise) ──
    cron_id: Optional[int] = None
```

- [ ] **Step 4: Re-run test**

Run: `pytest tests/test_turn_context.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add turn_context.py tests/test_turn_context.py
git commit -m "feat(turn_context): add cron_id field for scheduler binding"
```

---

### Task 2: Migration 008 — `agent_runs` table

**Files:**
- Create: `migrations/008_agent_runs.sql`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: Write the migration**

Create `migrations/008_agent_runs.sql`:

```sql
-- v0.19.0: unified agent_runs table replaces routine_runs.
--
-- Tracks one row per LLM-call site (main agent loop, synthesis, skill
-- creator, routine fire). Replaces routine_runs as the single source of
-- truth for per-run history; the old data is copied across with
-- source='routine' so existing UI continues to work.
--
-- status values:
--   running  — row inserted at run start, not yet finalized
--   ok       — agent.run finished, no error marker
--   err      — agent.run raised or reply matched a dry-run error marker
--   aborted  — abort_event fired mid-run; finished_at may be NULL
--   missed   — routine slot lapsed while server was offline; tokens=0
--   skipped  — per-thread fire lock held; tokens=0
BEGIN;

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,
    cron_id         INTEGER,
    source          TEXT NOT NULL,
    scheduled_at    REAL,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    duration_ms     INTEGER,
    status          TEXT NOT NULL,
    error           TEXT,
    result_preview  TEXT,
    model           TEXT,
    provider        TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_usd        REAL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_thread_id  ON agent_runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_cron_id    ON agent_runs(cron_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_source     ON agent_runs(source);

-- Copy existing routine_runs into agent_runs (best-effort; legacy installs
-- that never created the table get a no-op via the EXISTS clause).
INSERT INTO agent_runs
    (cron_id, thread_id, scheduled_at, started_at, finished_at,
     duration_ms, status, error, result_preview, source)
SELECT cron_id, COALESCE(thread_id, ''), scheduled_at, started_at, finished_at,
       duration_ms, status, error, result_preview, 'routine'
FROM routine_runs
WHERE EXISTS (SELECT name FROM sqlite_master WHERE type='table' AND name='routine_runs');

DROP TABLE IF EXISTS routine_runs;

COMMIT;
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_migrations.py`:

```python
def test_migration_008_creates_agent_runs(qwe_temp_data_dir):
    import db, sqlite3
    db._migrated = False  # force re-run
    conn = db._get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_runs'"
    ).fetchone()
    assert row is not None, "agent_runs table not created"
    cols = {c[1] for c in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    expected = {"id", "thread_id", "cron_id", "source", "scheduled_at",
                "started_at", "finished_at", "duration_ms", "status",
                "error", "result_preview", "model", "provider",
                "input_tokens", "output_tokens", "cost_usd"}
    assert expected.issubset(cols), f"missing cols: {expected - cols}"

def test_migration_008_drops_routine_runs(qwe_temp_data_dir):
    import db
    db._migrated = False
    conn = db._get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routine_runs'"
    ).fetchone()
    assert row is None, "routine_runs should be gone"

def test_migration_008_copies_legacy_routine_runs(qwe_temp_data_dir):
    import db, sqlite3, time
    # Build a DB at schema_version=7 with routine_runs populated, then
    # let the migration runner fast-forward to 8.
    conn = sqlite3.connect(qwe_temp_data_dir / "qwe_qwe.db")
    conn.executescript(open("migrations/001_initial.sql").read())
    for n in range(2, 8):
        path = f"migrations/00{n}_"
        import glob
        f = glob.glob(path + "*.sql")[0]
        conn.executescript(open(f).read())
    conn.execute("INSERT OR REPLACE INTO kv (key,value,ts) VALUES ('schema_version','7',?)", (time.time(),))
    conn.execute("INSERT INTO routine_runs (cron_id, scheduled_at, started_at, status, thread_id) "
                 "VALUES (1, 1000.0, 1001.0, 'ok', 't1')")
    conn.commit(); conn.close()
    db._migrated = False
    conn = db._get_conn()
    rows = conn.execute("SELECT cron_id, thread_id, status, source FROM agent_runs").fetchall()
    assert (1, 't1', 'ok', 'routine') in rows
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_migrations.py -v -k "008"`
Expected: FAIL (table doesn't exist / migration file missing).

- [ ] **Step 4: Run tests after creating the migration file**

Run: `pytest tests/test_migrations.py -v -k "008"`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/008_agent_runs.sql tests/test_migrations.py
git commit -m "feat(db): migration 008 — agent_runs table replaces routine_runs"
```

---

### Task 3: `db.py` helpers — insert / finalize / queries

**Files:**
- Modify: `db.py` (append new helpers at end)
- Test: `tests/test_agent_runs.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_runs.py`:

```python
"""Unit tests for db.py helpers added in v0.19.0 cost-tracking work."""
import time
import pytest


def test_insert_agent_run_returns_id(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(
        thread_id="t1", source="web", started_at=time.time(),
        status="running", model="gpt-4o-mini", provider="openai",
    )
    assert isinstance(rid, int) and rid > 0


def test_insert_agent_run_row_visible(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    row = db._get_conn().execute(
        "SELECT thread_id, source, started_at, status FROM agent_runs WHERE id=?",
        (rid,)).fetchone()
    assert row == ("t1", "web", 1000.0, "running")


def test_finalize_agent_run_updates_metrics(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    db.finalize_agent_run(rid, finished_at=1001.5, duration_ms=1500,
                          status="ok", result_preview="reply",
                          input_tokens=100, output_tokens=50, cost_usd=0.001)
    row = db._get_conn().execute(
        "SELECT finished_at, duration_ms, status, input_tokens, output_tokens, cost_usd "
        "FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row == (1001.5, 1500, "ok", 100, 50, 0.001)


def test_finalize_handles_null_finished_at(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None,
                          status="aborted", input_tokens=80, output_tokens=20)
    row = db._get_conn().execute(
        "SELECT finished_at, duration_ms, status FROM agent_runs WHERE id=?",
        (rid,)).fetchone()
    assert row == (None, None, "aborted")


def test_insert_skipped_run_writes_zero_tokens(qwe_temp_data_dir):
    import db
    rid = db.insert_skipped_run(cron_id=5, thread_id="t1",
                                scheduled_at=1000.0, reason="missed")
    row = db._get_conn().execute(
        "SELECT status, started_at, input_tokens, output_tokens "
        "FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row == ("missed", 1000.0, 0, 0)


def test_get_thread_totals_sums_correctly(qwe_temp_data_dir):
    import db
    for (i, o, c) in [(100, 50, 0.01), (200, 80, 0.02), (50, 30, None)]:
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=100,
                              status="ok", input_tokens=i, output_tokens=o,
                              cost_usd=c)
    totals = db.get_thread_totals("t1")
    assert totals["input_tokens"] == 350
    assert totals["output_tokens"] == 160
    # COALESCE on cost_usd treats NULL as 0 in the sum
    assert abs(totals["cost_usd"] - 0.03) < 1e-9
    assert totals["run_count"] == 3


def test_get_thread_totals_empty(qwe_temp_data_dir):
    import db
    totals = db.get_thread_totals("ghost")
    assert totals == {"input_tokens": 0, "output_tokens": 0,
                      "cost_usd": 0.0, "run_count": 0}


def test_get_runs_for_thread_ordering_and_limit(qwe_temp_data_dir):
    import db
    ids = []
    for t in [1000.0, 2000.0, 3000.0]:
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=t, status="running")
        ids.append(rid)
    rows = db.get_runs_for_thread("t1", limit=2)
    assert [r["id"] for r in rows] == [ids[2], ids[1]]


def test_get_period_totals_filters_by_source(qwe_temp_data_dir):
    import db
    for src, tok in [("web", 100), ("routine", 200), ("synthesis", 50)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=1500.0, status="running")
        db.finalize_agent_run(rid, finished_at=1501.0, duration_ms=1000,
                              status="ok", input_tokens=tok, output_tokens=0)
    t_routine = db.get_period_totals(1000.0, 2000.0, source="routine")
    assert t_routine["total_input_tokens"] == 200
    t_all = db.get_period_totals(1000.0, 2000.0)
    assert t_all["total_input_tokens"] == 350
    assert "by_source" in t_all
    assert t_all["by_source"]["synthesis"]["input_tokens"] == 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent_runs.py -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'insert_agent_run'`.

- [ ] **Step 3: Implement the helpers**

Append to `db.py`:

```python
# --- Agent runs (cost tracking) -------------------------------------------
# See migrations/008_agent_runs.sql for the table shape.

def insert_agent_run(
    thread_id: str,
    source: str,
    started_at: float,
    status: str = "running",
    cron_id: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    scheduled_at: float | None = None,
) -> int:
    """Insert a new run row. Returns the new id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO agent_runs (thread_id, cron_id, source, scheduled_at, "
        " started_at, status, model, provider) VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, cron_id, source, scheduled_at, started_at, status, model, provider),
    )
    conn.commit()
    return int(cur.lastrowid)


def finalize_agent_run(
    run_id: int,
    finished_at: float | None,
    duration_ms: int | None,
    status: str,
    error: str | None = None,
    result_preview: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = None,
) -> None:
    """Update a previously-inserted run with final metrics."""
    conn = _get_conn()
    conn.execute(
        "UPDATE agent_runs SET finished_at=?, duration_ms=?, status=?, "
        " error=?, result_preview=?, input_tokens=?, output_tokens=?, cost_usd=? "
        "WHERE id=?",
        (finished_at, duration_ms, status, error,
         (result_preview or "")[:200] or None,
         int(input_tokens or 0), int(output_tokens or 0), cost_usd, run_id),
    )
    conn.commit()


def insert_skipped_run(
    cron_id: int, thread_id: str, scheduled_at: float, reason: str = "missed"
) -> int:
    """For routine fires that never executed (missed/skipped)."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO agent_runs (thread_id, cron_id, source, scheduled_at, "
        " started_at, status, input_tokens, output_tokens, cost_usd) "
        "VALUES (?,?,?,?,?,?,0,0,0.0)",
        (thread_id or "", cron_id, "routine", scheduled_at, scheduled_at, reason),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_runs_for_thread(thread_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Per-thread run history, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, thread_id, cron_id, source, scheduled_at, started_at, "
        "       finished_at, duration_ms, status, error, result_preview, "
        "       model, provider, input_tokens, output_tokens, cost_usd "
        "FROM agent_runs WHERE thread_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (thread_id, int(limit), int(offset)),
    ).fetchall()
    cols = ("id", "thread_id", "cron_id", "source", "scheduled_at",
            "started_at", "finished_at", "duration_ms", "status", "error",
            "result_preview", "model", "provider",
            "input_tokens", "output_tokens", "cost_usd")
    return [dict(zip(cols, r)) for r in rows]


def get_thread_totals(thread_id: str) -> dict:
    """Returns {input_tokens, output_tokens, cost_usd, run_count}."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        "       COALESCE(SUM(cost_usd),0.0), COUNT(*) "
        "FROM agent_runs WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    return {
        "input_tokens": int(row[0]),
        "output_tokens": int(row[1]),
        "cost_usd": float(row[2]),
        "run_count": int(row[3]),
    }


def get_period_totals(
    start_ts: float, end_ts: float, source: str | None = None
) -> dict:
    """Aggregated metrics for a time window. by_source breakdown included."""
    conn = _get_conn()
    params: list = [start_ts, end_ts]
    src_clause = ""
    if source:
        src_clause = " AND source=?"
        params.append(source)
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        "       COALESCE(SUM(cost_usd),0.0), COUNT(*) "
        f"FROM agent_runs WHERE started_at>=? AND started_at<?{src_clause}",
        params,
    ).fetchone()
    by_src_rows = conn.execute(
        "SELECT source, COALESCE(SUM(input_tokens),0), "
        "       COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0.0), "
        "       COUNT(*) FROM agent_runs WHERE started_at>=? AND started_at<? "
        "GROUP BY source",
        (start_ts, end_ts),
    ).fetchall()
    by_source = {
        r[0]: {
            "input_tokens": int(r[1]),
            "output_tokens": int(r[2]),
            "cost_usd": float(r[3]),
            "run_count": int(r[4]),
        }
        for r in by_src_rows
    }
    return {
        "start_ts": float(start_ts),
        "end_ts": float(end_ts),
        "total_input_tokens": int(row[0]),
        "total_output_tokens": int(row[1]),
        "total_cost_usd": float(row[2]),
        "run_count": int(row[3]),
        "by_source": by_source,
    }


def get_runs_for_routine(cron_id: int, limit: int = 50) -> list[dict]:
    """Per-routine run history (replaces routine_runs query)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, thread_id, scheduled_at, started_at, finished_at, "
        "       duration_ms, status, error, result_preview, "
        "       input_tokens, output_tokens, cost_usd, model, provider "
        "FROM agent_runs WHERE cron_id=? ORDER BY id DESC LIMIT ?",
        (int(cron_id), int(limit)),
    ).fetchall()
    cols = ("id", "thread_id", "scheduled_at", "started_at", "finished_at",
            "duration_ms", "status", "error", "result_preview",
            "input_tokens", "output_tokens", "cost_usd", "model", "provider")
    return [dict(zip(cols, r)) for r in rows]
```

- [ ] **Step 4: Re-run tests**

Run: `pytest tests/test_agent_runs.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_agent_runs.py
git commit -m "feat(db): helpers for agent_runs — insert/finalize/totals/queries"
```

---

## Phase 2 — Pricing Module

### Task 4: `pricing.py` skeleton + bundled fallback

**Files:**
- Create: `pricing.py`
- Test: `tests/test_pricing.py`
- Modify: `pyproject.toml` (add `pricing` to `[tool.setuptools] py-modules`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_pricing.py`:

```python
"""Unit tests for the pricing module (cost tracking)."""
import pytest


def test_bundled_fallback_has_gpt4o_mini():
    import pricing
    assert "gpt-4o-mini" in pricing._BUNDLED_FALLBACK
    assert pricing._BUNDLED_FALLBACK["gpt-4o-mini"]["input"] > 0


def test_local_provider_zero_cost(qwe_temp_data_dir):
    import pricing
    assert pricing.get_price("lmstudio:llama-3", "input") == 0.0
    assert pricing.get_price("ollama:qwen2.5", "output") == 0.0
    assert pricing.get_price("local:any-model", "input") == 0.0


def test_compute_cost_local_zero(qwe_temp_data_dir):
    import pricing
    assert pricing.compute_cost("ollama:llama-3", 1000, 500) == 0.0


def test_get_price_unknown_model_returns_none(qwe_temp_data_dir):
    import pricing
    assert pricing.get_price("totally-fake-model-9000", "input") is None


def test_compute_cost_unknown_returns_none(qwe_temp_data_dir):
    import pricing
    assert pricing.compute_cost("totally-fake-model-9000", 1000, 500) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pricing.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'pricing'`).

- [ ] **Step 3: Create the module skeleton**

Create `pricing.py`:

```python
"""Online pricing fetcher + cache + fallback chain.

Fetches LiteLLM's community-maintained model_prices_and_context_window.json,
caches it on disk, and falls back to a bundled minimal dict for air-gapped
or offline scenarios. Network I/O is owned by the background refresher
(start_background_refresher) and POST /api/pricing/refresh — get_price()
itself is purely in-memory and never blocks.

Lookup chain (in get_price):
  1. KV override:     pricing_override_<model>
  2. Local provider:  lmstudio:/ollama:/local: prefix → 0.0
  3. Memory cache:    populated by _ensure_loaded()
  4. Bundled fallback: top-10 hardcoded models
  5. None             (caller writes cost_usd = NULL)
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, Optional

import config
import db
import logger

_log = logger.get("pricing")

DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_TTL_SEC = 24 * 3600
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB hard cap on JSON download
REMOTE_TIMEOUT_SEC = 10
SKIP_MODES = {"embedding", "image_generation",
              "audio_transcription", "audio_speech"}

# Top-10 fallback. Values in $/token (NOT $/1M tokens).
_BUNDLED_FALLBACK: dict[str, dict[str, float]] = {
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

_LOCAL_PREFIXES = ("lmstudio:", "ollama:", "local:")

_lock = threading.Lock()
_pricing_cache: dict[str, dict[str, float]] | None = None
_cache_fetched_at: float | None = None


def _cache_path() -> Path:
    return Path(config.DATA_DIR) / "pricing_cache.json"


def get_price(model: str, kind: Literal["input", "output"]) -> float | None:
    """$/token for (model, kind); None if unknown. Never does network I/O."""
    if not model:
        return None
    # 1. KV override
    raw = db.kv_get(f"pricing_override_{model}")
    if raw:
        try:
            return float(json.loads(raw)[kind])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            _log.warning(f"invalid pricing_override for {model}")
    # 2. Local providers
    if model.startswith(_LOCAL_PREFIXES):
        return 0.0
    # 3. Memory / disk cache → 4. Bundled fallback
    pricing = _ensure_loaded()
    entry = pricing.get(model)
    if entry and kind in entry:
        return entry[kind]
    fb = _BUNDLED_FALLBACK.get(model)
    if fb:
        return fb[kind]
    return None


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Total $ cost. None if either side's price is unknown."""
    in_p = get_price(model, "input")
    out_p = get_price(model, "output")
    if in_p is None or out_p is None:
        return None
    return float(input_tokens) * in_p + float(output_tokens) * out_p


def _ensure_loaded() -> dict[str, dict[str, float]]:
    """Lazy-load disk cache into memory. Empty dict if neither present."""
    global _pricing_cache, _cache_fetched_at
    if _pricing_cache is not None:
        return _pricing_cache
    with _lock:
        if _pricing_cache is not None:
            return _pricing_cache
        path = _cache_path()
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                _pricing_cache = payload.get("models") or {}
                _cache_fetched_at = float(payload.get("fetched_at") or 0)
                return _pricing_cache
            except (json.JSONDecodeError, OSError, ValueError) as e:
                _log.warning(f"corrupt pricing cache, ignoring: {e}")
        _pricing_cache = {}
        _cache_fetched_at = None
        return _pricing_cache


def last_updated() -> Optional[float]:
    _ensure_loaded()
    return _cache_fetched_at


def all_known_models() -> list[str]:
    return sorted(set(_ensure_loaded().keys()) | set(_BUNDLED_FALLBACK.keys()))


# refresh_pricing() and start_background_refresher() come in later tasks.
def refresh_pricing(force: bool = False) -> bool:
    """Stub; full implementation in Task 9."""
    return False


def start_background_refresher() -> None:
    """Stub; full implementation in Task 10."""
    return None
```

- [ ] **Step 4: Register the module in pyproject.toml**

Open `pyproject.toml`, find the `[tool.setuptools]` `py-modules = [...]` list, and add `"pricing"`.

- [ ] **Step 5: Re-run tests**

Run: `pytest tests/test_pricing.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add pricing.py tests/test_pricing.py pyproject.toml
git commit -m "feat(pricing): module skeleton with bundled fallback + local providers"
```

---

### Task 5: KV override + disk cache loading

**Files:**
- Modify: `pricing.py` (already covers this — add tests only)
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pricing.py`:

```python
def test_kv_override_beats_bundled(qwe_temp_data_dir):
    import pricing, db, json
    db.kv_set("pricing_override_gpt-4o-mini",
              json.dumps({"input": 9.99e-7, "output": 1.23e-6}))
    assert pricing.get_price("gpt-4o-mini", "input") == 9.99e-7
    assert pricing.get_price("gpt-4o-mini", "output") == 1.23e-6


def test_kv_override_invalid_json_warns_and_continues(qwe_temp_data_dir, caplog):
    import pricing, db
    db.kv_set("pricing_override_gpt-4o-mini", "{not json")
    with caplog.at_level("WARNING"):
        v = pricing.get_price("gpt-4o-mini", "input")
    assert v == pricing._BUNDLED_FALLBACK["gpt-4o-mini"]["input"]
    assert any("invalid pricing_override" in r.message for r in caplog.records)


def test_disk_cache_loaded_on_first_call(qwe_temp_data_dir):
    import pricing, json
    pricing._pricing_cache = None  # force reload
    payload = {
        "fetched_at": 1700000000.0,
        "source_url": "test",
        "models": {"my-custom-model": {"input": 1e-6, "output": 2e-6}},
    }
    pricing._cache_path().write_text(json.dumps(payload))
    assert pricing.get_price("my-custom-model", "input") == 1e-6
    assert pricing.last_updated() == 1700000000.0


def test_corrupt_cache_file_falls_back_gracefully(qwe_temp_data_dir):
    import pricing
    pricing._pricing_cache = None
    pricing._cache_path().write_text("{ malformed ")
    # Should not raise; falls back to bundled
    assert pricing.get_price("gpt-4o-mini", "input") > 0
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_pricing.py -v -k "override or disk or corrupt"`
Expected: ALL PASS (logic was already in Task 4's pricing.py).

- [ ] **Step 3: Commit**

```bash
git add tests/test_pricing.py
git commit -m "test(pricing): KV override + disk cache lookup coverage"
```

---

### Task 6: LiteLLM JSON normalization

**Files:**
- Modify: `pricing.py`
- Create: `tests/fixtures/litellm_sample.json`
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/litellm_sample.json`:

```json
{
  "sample_spec": {
    "max_tokens": 8192,
    "input_cost_per_token": 0.0,
    "output_cost_per_token": 0.0,
    "litellm_provider": "openai",
    "mode": "chat",
    "supports_function_calling": true
  },
  "gpt-4o-mini": {
    "max_tokens": 16384,
    "input_cost_per_token": 0.00000015,
    "output_cost_per_token": 0.00000060,
    "litellm_provider": "openai",
    "mode": "chat"
  },
  "claude-3-5-sonnet-20241022": {
    "max_tokens": 8192,
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015,
    "litellm_provider": "anthropic",
    "mode": "chat"
  },
  "text-embedding-3-small": {
    "max_tokens": 8191,
    "input_cost_per_token": 0.00000002,
    "litellm_provider": "openai",
    "mode": "embedding"
  },
  "dall-e-3": {
    "input_cost_per_pixel": 0.000040,
    "litellm_provider": "openai",
    "mode": "image_generation"
  },
  "whisper-1": {
    "input_cost_per_second": 0.0001,
    "litellm_provider": "openai",
    "mode": "audio_transcription"
  },
  "broken-entry": {
    "litellm_provider": "openai"
  }
}
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_pricing.py`:

```python
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "litellm_sample.json"


def test_normalize_litellm_keeps_chat_models():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "gpt-4o-mini" in out
    assert out["gpt-4o-mini"] == {"input": 0.00000015, "output": 0.00000060}
    assert "claude-3-5-sonnet-20241022" in out


def test_normalize_litellm_skips_sample_spec():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "sample_spec" not in out


def test_normalize_litellm_skips_non_chat_modes():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "text-embedding-3-small" not in out
    assert "dall-e-3" not in out
    assert "whisper-1" not in out


def test_normalize_litellm_skips_entries_missing_prices():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "broken-entry" not in out
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_pricing.py -v -k "normalize"`
Expected: FAIL (`AttributeError: ... _normalize_litellm`).

- [ ] **Step 4: Add the normalizer to `pricing.py`**

Insert above `def refresh_pricing`:

```python
def _normalize_litellm(raw: dict) -> dict[str, dict[str, float]]:
    """Convert LiteLLM's JSON into our flat {model: {input, output}} shape.

    Skips:
      - the 'sample_spec' meta-entry
      - any entry where mode is in SKIP_MODES (embeddings, images, audio)
      - any entry missing input_cost_per_token or output_cost_per_token
    """
    out: dict[str, dict[str, float]] = {}
    for name, entry in raw.items():
        if name == "sample_spec" or not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if mode in SKIP_MODES:
            continue
        try:
            in_p = float(entry["input_cost_per_token"])
            out_p = float(entry["output_cost_per_token"])
        except (KeyError, TypeError, ValueError):
            continue
        out[name] = {"input": in_p, "output": out_p}
    return out
```

- [ ] **Step 5: Re-run tests**

Run: `pytest tests/test_pricing.py -v -k "normalize"`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add pricing.py tests/test_pricing.py tests/fixtures/litellm_sample.json
git commit -m "feat(pricing): LiteLLM JSON normalizer + fixture"
```

---

### Task 7: Remote fetch with SSRF guard + size cap

**Files:**
- Modify: `pricing.py`
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_pricing.py`:

```python
from unittest import mock


def test_refresh_pricing_writes_disk_cache(qwe_temp_data_dir, monkeypatch):
    import pricing, json
    pricing._pricing_cache = None
    fake_body = json.dumps({
        "gpt-4o-mini": {"input_cost_per_token": 1e-7,
                        "output_cost_per_token": 2e-7,
                        "litellm_provider": "openai", "mode": "chat"}
    }).encode()
    class _Resp:
        def read(self, n=None): return fake_body[:n] if n else fake_body
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        headers = {"Content-Length": str(len(fake_body))}
    monkeypatch.setattr(pricing.urllib.request, "urlopen", lambda *a, **kw: _Resp())
    ok = pricing.refresh_pricing(force=True)
    assert ok is True
    assert pricing._cache_path().exists()
    payload = json.loads(pricing._cache_path().read_text())
    assert "gpt-4o-mini" in payload["models"]


def test_refresh_pricing_ssrf_blocks_loopback(qwe_temp_data_dir, monkeypatch):
    import pricing, config
    monkeypatch.setattr(config, "get", lambda key: "http://127.0.0.1/x" if key=="pricing_url" else False)
    ok = pricing.refresh_pricing(force=True)
    assert ok is False


def test_refresh_pricing_respects_body_size_cap(qwe_temp_data_dir, monkeypatch):
    import pricing
    big_body = b"x" * (pricing.MAX_BODY_BYTES + 1)
    class _Resp:
        def read(self, n=None): return big_body[:n] if n else big_body
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        headers = {"Content-Length": str(len(big_body))}
    monkeypatch.setattr(pricing.urllib.request, "urlopen", lambda *a, **kw: _Resp())
    ok = pricing.refresh_pricing(force=True)
    assert ok is False


def test_refresh_pricing_network_error_keeps_stale_cache(qwe_temp_data_dir, monkeypatch):
    import pricing, json
    # Pre-populate disk cache
    pricing._cache_path().write_text(json.dumps({
        "fetched_at": 1, "source_url": "x", "models": {"old": {"input": 1, "output": 2}}
    }))
    pricing._pricing_cache = None
    def boom(*a, **kw):
        raise urllib.error.URLError("nope")
    import urllib.error
    monkeypatch.setattr(pricing.urllib.request, "urlopen", boom)
    pricing.refresh_pricing(force=True)
    # Stale cache still wins
    assert pricing.get_price("old", "input") == 1
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_pricing.py -v -k "refresh"`
Expected: FAIL — `refresh_pricing` is a stub.

- [ ] **Step 3: Replace the stub `refresh_pricing` in `pricing.py`**

```python
def _ssrf_allowed(url: str) -> bool:
    """Block private/loopback/link-local unless QWE_ALLOW_PRIVATE_URLS=1."""
    import os
    if os.environ.get("QWE_ALLOW_PRIVATE_URLS") == "1":
        return True
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            ip = ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
    except (OSError, ValueError):
        return False
    return True


def refresh_pricing(force: bool = False) -> bool:
    """Refresh pricing from remote. Returns True on success.

    Thread-safe; concurrent callers serialize on _lock. Never raises.
    """
    global _pricing_cache, _cache_fetched_at
    url = config.get("pricing_url") or DEFAULT_PRICING_URL
    if not force and _cache_fetched_at and (time.time() - _cache_fetched_at) < CACHE_TTL_SEC:
        return True
    if not _ssrf_allowed(url):
        _log.warning(f"pricing_url blocked by SSRF guard: {url}")
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "qwe-qwe-pricing/1.0"})
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT_SEC) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            _log.warning(f"pricing response > {MAX_BODY_BYTES} bytes, refusing")
            return False
        raw = json.loads(body.decode("utf-8"))
        models = _normalize_litellm(raw)
        if not models:
            _log.warning("pricing JSON yielded zero usable models; keeping cache")
            return False
        payload = {
            "fetched_at": time.time(),
            "source_url": url,
            "models": models,
        }
        tmp = _cache_path().with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(_cache_path())
        with _lock:
            _pricing_cache = models
            _cache_fetched_at = payload["fetched_at"]
        _log.info(f"pricing refreshed: {len(models)} models")
        return True
    except Exception as e:
        _log.warning(f"pricing refresh failed: {e}")
        return False
```

- [ ] **Step 4: Re-run tests**

Run: `pytest tests/test_pricing.py -v -k "refresh"`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add pricing.py tests/test_pricing.py
git commit -m "feat(pricing): remote fetch with SSRF guard + 5MB size cap"
```

---

### Task 8: Background refresher thread

**Files:**
- Modify: `pricing.py`
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pricing.py`:

```python
def test_background_refresher_disabled_when_off(qwe_temp_data_dir, monkeypatch):
    import pricing, config
    monkeypatch.setattr(config, "get",
        lambda key: False if key == "pricing_auto_update" else "")
    called = []
    monkeypatch.setattr(pricing, "refresh_pricing",
                        lambda force=False: called.append(force) or True)
    pricing.start_background_refresher()
    import time as _t; _t.sleep(0.05)
    assert called == []  # never fired


def test_background_refresher_runs_when_on(qwe_temp_data_dir, monkeypatch):
    import pricing, config
    monkeypatch.setattr(config, "get",
        lambda key: True if key == "pricing_auto_update" else "")
    monkeypatch.setattr(pricing, "CACHE_TTL_SEC", 0.05)  # rapid fire
    fired = []
    def fake(force=False):
        fired.append(time.time()); return True
    monkeypatch.setattr(pricing, "refresh_pricing", fake)
    pricing.start_background_refresher()
    time.sleep(0.18)
    assert len(fired) >= 2  # at least two fires in ~150ms
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_pricing.py -v -k "background"`
Expected: FAIL — stub doesn't fire.

- [ ] **Step 3: Implement the thread**

Replace the stub `start_background_refresher` in `pricing.py`:

```python
_refresher_started = False
_refresher_lock = threading.Lock()


def start_background_refresher() -> None:
    """Start a daemon thread that calls refresh_pricing() every CACHE_TTL_SEC.

    Idempotent — safe to call multiple times; only starts one thread.
    No-op when pricing_auto_update is disabled.
    """
    global _refresher_started
    if not config.get("pricing_auto_update"):
        return
    with _refresher_lock:
        if _refresher_started:
            return
        _refresher_started = True

    def _loop():
        while True:
            try:
                refresh_pricing(force=False)
            except Exception as e:
                _log.warning(f"pricing refresher loop error: {e}")
            time.sleep(CACHE_TTL_SEC)

    t = threading.Thread(target=_loop, name="pricing-refresher", daemon=True)
    t.start()
    _log.info("pricing background refresher started")
```

- [ ] **Step 4: Re-run tests**

Run: `pytest tests/test_pricing.py -v -k "background"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pricing.py tests/test_pricing.py
git commit -m "feat(pricing): background refresher daemon thread"
```

---

### Task 9: Settings for pricing (config.py)

**Files:**
- Modify: `config.py`
- Test: `tests/test_pricing.py` (additional case)

- [ ] **Step 1: Add settings**

In `config.py`, locate `EDITABLE_SETTINGS` and append:

```python
    ("pricing_url",         str,  "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json", "URL for online pricing JSON (LiteLLM format). Override for air-gapped mirrors.", None, None),
    ("pricing_auto_update", bool, True, "Refresh pricing every 24h in background.", None, None),
```

(Exact tuple shape must match the existing entries in `EDITABLE_SETTINGS` — check current file.)

- [ ] **Step 2: Write the test**

Append to `tests/test_pricing.py`:

```python
def test_pricing_settings_have_defaults(qwe_temp_data_dir):
    import config
    assert config.get("pricing_url").startswith("https://")
    assert config.get("pricing_auto_update") is True
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_pricing.py::test_pricing_settings_have_defaults -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add config.py tests/test_pricing.py
git commit -m "feat(config): add pricing_url + pricing_auto_update settings"
```

---

### Task 10: Wire pricing startup into `server.py`

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Find the server startup**

Look for the FastAPI startup event handler (search `@app.on_event("startup")` or the lifespan handler near the top of `server.py`).

- [ ] **Step 2: Add the call**

Inside the startup handler, after existing init lines:

```python
    import pricing
    pricing.start_background_refresher()
```

- [ ] **Step 3: Smoke-test via import**

Run: `python -c "import server; print('ok')"`
Expected: prints `ok`. No crash.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(server): start pricing background refresher on startup"
```

---

## Phase 3 — Instrumentation

### Task 11: Instrument `agent_loop.py` main loop

**Files:**
- Modify: `agent_loop.py`
- Test: `tests/test_integration.py`

- [ ] **Step 1: Add the bracket logic**

Inside `run_loop()` (in `agent_loop.py`), capture the start time and run_id at the very top of the function (before the first LLM call):

```python
import db, pricing  # at top of file if not already present
import time as _time

# ... inside run_loop, after we know `model`, `provider`, `thread_id`, `ctx`:
_run_started = _time.time()
_run_id = db.insert_agent_run(
    thread_id=thread_id,
    source=(ctx.source if ctx else "cli"),
    started_at=_run_started,
    status="running",
    cron_id=(ctx.cron_id if ctx else None),
    model=model,
    provider=provider,
)
_final_status = "ok"
_final_error: str | None = None
```

Wrap the existing body in `try/except` and a `finally`:

```python
try:
    # ... existing loop body unchanged ...
except Exception as e:
    _final_status = "err"
    _final_error = str(e)[:500]
    raise
finally:
    _finished = _time.time()
    if ctx and ctx.abort_event.is_set() and _final_status == "ok":
        _final_status = "aborted"
    _cost = None
    try:
        _cost = pricing.compute_cost(model, stats.input_tokens, stats.output_tokens)
    except Exception:
        pass
    db.finalize_agent_run(
        run_id=_run_id,
        finished_at=(None if _final_status == "aborted" and ctx and ctx.abort_event.is_set() else _finished),
        duration_ms=(None if _final_status == "aborted" else int((_finished - _run_started) * 1000)),
        status=_final_status,
        error=_final_error,
        result_preview=(reply or "")[:200] if 'reply' in locals() else None,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        cost_usd=_cost,
    )
```

- [ ] **Step 2: Write the integration test**

Append to `tests/test_integration.py`:

```python
def test_agent_run_writes_agent_runs_row(qwe_temp_data_dir, mock_llm):
    import agent, db
    agent.run("hello", thread_id="t1", source="cli")
    rows = db.get_runs_for_thread("t1")
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "cli"
    assert r["status"] in ("ok", "err")  # depends on mock
    assert r["input_tokens"] >= 0
    assert r["output_tokens"] >= 0
```

- [ ] **Step 3: Run integration tests**

Run: `pytest tests/test_integration.py::test_agent_run_writes_agent_runs_row -v`
Expected: PASS.

- [ ] **Step 4: Run full suite to catch regressions**

Run: `pytest tests/ -q`
Expected: existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent_loop.py tests/test_integration.py
git commit -m "feat(agent_loop): instrument run_loop with agent_runs bracket"
```

---

### Task 12: Instrument `synthesis.py` LLM calls

**Files:**
- Modify: `synthesis.py`

**Context:** The public entry is `run_synthesis()` (no args) which iterates `pending` groups via `_process_group(client, model, group_name, chunks)`. The actual LLM call site is **`_extract_entities(client, model, text)` at line 128** — that's the only `client.chat.completions.create(...)` in the file. Synthesis chunks carry their originating `thread_id` via the chunk dict; when missing, default to a sentinel "__synthesis__" string so the run still groups together in analytics.

- [ ] **Step 1: Confirm the LLM call site**

Run: `grep -n "client.chat.completions.create\|client\.chat" synthesis.py`
Expected: exactly one hit around line 128 inside `_extract_entities`.

- [ ] **Step 2: Change `_extract_entities` signature to accept `thread_id`**

Modify the signature to take `thread_id: str = "__synthesis__"`:

```python
def _extract_entities(client, model: str, text: str, thread_id: str = "__synthesis__") -> dict | None:
```

And in `_process_group`, pass it through:

```python
# in _process_group, find chunks[0] thread_id (fall back to group_name)
src_thread = chunks[0].get("thread_id") or group_name or "__synthesis__"
extraction = _extract_entities(client, model, full_text, thread_id=src_thread)
```

- [ ] **Step 3: Wrap the LLM call**

Inside `_extract_entities`, before the existing `try:` that wraps `client.chat.completions.create(...)`, add the bracket:

```python
import db, pricing, time as _t
import providers as _providers
_started = _t.time()
_rid = db.insert_agent_run(
    thread_id=thread_id, source="synthesis",
    started_at=_started, status="running",
    model=model, provider=_providers.current_kind(),
)
_status = "ok"; _err = None; in_tok = out_tok = 0
try:
    resp = client.chat.completions.create(...)
    if getattr(resp, "usage", None):
        in_tok = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(resp.usage, "completion_tokens", 0) or 0)
    # ... existing JSON-parse logic unchanged ...
except Exception as e:
    _status = "err"; _err = str(e)[:500]
    raise
finally:
    _finished = _t.time()
    db.finalize_agent_run(
        _rid, finished_at=_finished,
        duration_ms=int((_finished - _started) * 1000),
        status=_status, error=_err,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=pricing.compute_cost(model, in_tok, out_tok),
    )
```

(`providers.current_kind()` may need verification — substitute with whichever helper returns "openai" / "anthropic" / "lmstudio" / etc. Check `providers.py` for the actual name.)

- [ ] **Step 4: Write the test**

Append to `tests/test_integration.py`:

```python
def test_synthesis_call_writes_agent_runs_row(qwe_temp_data_dir, mock_llm, monkeypatch):
    import synthesis, db, memory
    # Seed a single pending chunk so synthesis has work to do
    monkeypatch.setattr(memory, "get_pending_synthesis",
                        lambda limit: {"grp1": [{"id": "c1", "text": "hello world",
                                                 "thread_id": "t-syn", "source": "test"}]})
    monkeypatch.setattr(memory, "mark_synthesized", lambda ids: None)
    synthesis.run_synthesis()
    rows = [r for r in db.get_runs_for_thread("t-syn") if r["source"] == "synthesis"]
    assert len(rows) >= 1
```

- [ ] **Step 5: Run + Commit**

```bash
pytest tests/test_integration.py::test_synthesis_call_writes_agent_runs_row -v
git add synthesis.py tests/test_integration.py
git commit -m "feat(synthesis): instrument _extract_entities with agent_runs"
```

---

### Task 13: Instrument `skill_creator` pipeline

**Files:**
- Modify: `skills/skill_creator.py`

- [ ] **Step 1: Wrap `_run_pipeline()`**

Find `def _run_pipeline(` in `skills/skill_creator.py`. Wrap the whole body (after we know the model) with the same bracket pattern:

```python
import db, pricing, time as _t
_started = _t.time()
_rid = db.insert_agent_run(thread_id=thread_id, source="skill_creator",
                            started_at=_started, status="running",
                            model=model, provider=provider)
agg_in = agg_out = 0
status = "ok"; err = None; preview = None
try:
    # for each step in the existing pipeline, capture step_in / step_out from usage
    # accumulate into agg_in / agg_out
    # ... existing body unchanged otherwise ...
    preview = f"created skill: {skill_name}"
except Exception as e:
    status = "err"; err = str(e)[:500]
    raise
finally:
    _finished = _t.time()
    db.finalize_agent_run(_rid, finished_at=_finished,
        duration_ms=int((_finished - _started) * 1000),
        status=status, error=err, result_preview=preview,
        input_tokens=agg_in, output_tokens=agg_out,
        cost_usd=pricing.compute_cost(model, agg_in, agg_out))
```

- [ ] **Step 2: Test**

Run: `pytest tests/test_skill_creator_pipeline.py -v`
Expected: existing tests still pass (no behavior changes other than insert).

Append a focused test:

```python
def test_skill_creator_pipeline_writes_agent_run(qwe_temp_data_dir, mock_llm):
    import db
    from skills.skill_creator import create_skill
    create_skill("test_skill", "no-op skill")
    rows = [r for r in db.get_runs_for_thread("default") if r["source"] == "skill_creator"]
    assert len(rows) == 1
```

- [ ] **Step 3: Commit**

```bash
git add skills/skill_creator.py tests/test_skill_creator_pipeline.py
git commit -m "feat(skill_creator): instrument pipeline with agent_runs"
```

---

### Task 14: Switch `scheduler.py` from `routine_runs` to `agent_runs`

**Files:**
- Modify: `scheduler.py`
- Test: `tests/test_scheduler.py` (existing — adjust failing cases)

**Call sites to refactor** (verified line ranges from current `scheduler.py`):

| Line | What to do |
|---|---|
| ~389 | `firing: true iff agent.run is currently executing` — docstring only, leave |
| ~576 | `_fire()`: passes ctx into `agent.run`; **add `ctx.cron_id = cron_id` before the call** so agent_loop's instrumentation links the row |
| ~634 | "Routines run through agent.run" — comment, leave |
| ~752 | `_append_run(...)`: full body rewrite — see Step 2 below |
| ~777 | `list_runs(cron_id, limit)`: replace body with `return db.get_runs_for_routine(cron_id, limit=limit)` |
| ~807 | `count_recent_runs_by_status(cron_id, limit)`: change SQL to `agent_runs` table |
| ~821 | `detect_missed_runs()`: where it currently inserts a `missed` row into `routine_runs`, switch to `db.insert_skipped_run(cron_id, thread_id, scheduled_at, reason='missed')` |
| ~894 | `_fire` body: **remove** the `_append_run` call for ok/err completions — agent_loop now writes that row. Keep the `_append_run` call ONLY for the per-thread-lock-held `skipped` case |

- [ ] **Step 1: Find current call sites**

Run: `grep -n "_append_run\|list_runs\|count_recent_runs\|detect_missed_runs\|routine_runs" scheduler.py`
Expected: matches the line ranges in the table above. (If the file has drifted since this plan was written, use the grep output to anchor edits.)

- [ ] **Step 2: Replace `_append_run` body**

Change `_append_run(...)` to call `db.insert_skipped_run` for `status in ('missed', 'skipped')`, and for `ok`/`err` to call `db.insert_agent_run` + `db.finalize_agent_run` (or only `insert_skipped_run`-style row when called after-the-fact). Simpler approach: keep the function signature but write to `agent_runs`:

```python
def _append_run(cron_id, scheduled_at, started_at, finished_at, duration_ms,
                status, error, result_preview, thread_id):
    try:
        rid = db.insert_agent_run(
            thread_id=thread_id or "",
            cron_id=cron_id,
            source="routine",
            scheduled_at=scheduled_at,
            started_at=started_at if started_at is not None else scheduled_at,
            status="running",
        )
        db.finalize_agent_run(
            rid,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=status,
            error=error,
            result_preview=result_preview,
            input_tokens=0, output_tokens=0,
            cost_usd=None,
        )
        return rid
    except Exception as e:
        _log.debug(f"agent_runs insert failed for #{cron_id}: {e}")
        return None
```

For runs that actually executed (`ok`/`err`), the main `agent_loop.run_loop` will be writing its own row with tokens populated, and `scheduler._fire` should now NOT write a second row. Refactor `_fire`:

- Pass `ctx.cron_id = cron_id` in the `TurnContext` it constructs.
- Remove the `_append_run` call for `ok`/`err` cases — `agent_loop` does it.
- Keep `_append_run` calls ONLY for `skipped` cases (the per-thread lock was held).

For `missed` runs: `detect_missed_runs()` calls `db.insert_skipped_run` instead of constructing a row with `_append_run`.

- [ ] **Step 3: Update `list_runs` and `count_recent_runs_by_status`**

```python
def list_runs(cron_id, limit=20):
    return db.get_runs_for_routine(cron_id, limit=limit)

def count_recent_runs_by_status(cron_id, limit=20):
    rows = db._get_conn().execute(
        "SELECT status FROM agent_runs WHERE cron_id=? ORDER BY id DESC LIMIT ?",
        (cron_id, limit),
    ).fetchall()
    counts = {"ok": 0, "err": 0, "missed": 0, "skipped": 0, "running": 0, "aborted": 0}
    series = []
    for (status,) in rows:
        counts[status] = counts.get(status, 0) + 1
        series.append(status)
    return {"counts": counts, "series": list(reversed(series))}
```

- [ ] **Step 4: Tests**

Run: `pytest tests/test_scheduler.py tests/test_agent_runs.py -v`
Expected: PASS (existing scheduler tests should pass; failing ones likely reference old `routine_runs` SQL and need updating).

- [ ] **Step 5: Commit**

```bash
git add scheduler.py tests/
git commit -m "refactor(scheduler): point routine bookkeeping at agent_runs"
```

---

## Phase 4 — API Endpoints

### Task 15: Extend `GET /api/threads`

**Files:**
- Modify: `server.py` (handler at line 2984)
- Test: `tests/test_analytics_api.py` (new file)

**Naming note:** qwe-qwe's data layer uses "threads" everywhere. The UI's "Sessions list" is a thread list, and the endpoint is `GET /api/threads`. The spec uses "session" in user-facing copy but every code path is "thread".

- [ ] **Step 1: Find existing endpoint**

Run: `grep -n '@app.get."/api/threads"' server.py`
Expected: hits at line 2984. Read ~30 lines below to see the current response shape (the `est_tokens` / `user_messages` fields already aggregated there are computed differently — we add the new fields alongside, don't replace).

- [ ] **Step 2: Add `db.get_thread_totals` calls**

In the existing `GET /api/threads` handler, after constructing each session dict, merge the totals:

```python
sessions = []
for thread_id, ... in rows:
    totals = db.get_thread_totals(thread_id)
    sessions.append({
        "thread_id": thread_id,
        # ... existing fields ...
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cost_usd": (totals["cost_usd"] if totals["run_count"] else None),
        "run_count": totals["run_count"],
    })
```

- [ ] **Step 3: Write the test**

Create `tests/test_analytics_api.py`:

```python
"""Unit tests for analytics-related HTTP endpoints."""
import time, pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import server
    return TestClient(server.app)


def test_sessions_endpoint_includes_token_fields(client, qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                          status="ok", input_tokens=100, output_tokens=50,
                          cost_usd=0.001)
    r = client.get("/api/threads")
    assert r.status_code == 200
    sess = [s for s in r.json() if s["thread_id"] == "t1"]
    assert sess and sess[0]["input_tokens"] == 100
    assert sess[0]["cost_usd"] == 0.001
    assert sess[0]["run_count"] == 1
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_analytics_api.py::test_sessions_endpoint_includes_token_fields -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_analytics_api.py
git commit -m "feat(api): extend /api/threads with tokens/cost/run_count"
```

---

### Task 16: New `GET /api/threads/{thread_id}/runs`

**Files:**
- Modify: `server.py`
- Test: `tests/test_analytics_api.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_session_runs_endpoint(client, qwe_temp_data_dir):
    import db, time
    for tok in (100, 200, 300):
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok,
                              cost_usd=tok * 1e-6)
    r = client.get("/api/threads/t1/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 3
    assert runs[0]["input_tokens"] == 300  # newest first
    assert runs[2]["input_tokens"] == 100


def test_session_runs_empty_thread_returns_empty_list(client, qwe_temp_data_dir):
    r = client.get("/api/threads/never-existed/runs")
    assert r.status_code == 200
    assert r.json() == []
```

- [ ] **Step 2: Add the endpoint**

In `server.py`:

```python
@app.get("/api/threads/{thread_id}/runs")
async def get_session_runs(thread_id: str, limit: int = 50, offset: int = 0):
    import db
    return db.get_runs_for_thread(thread_id, limit=limit, offset=offset)
```

- [ ] **Step 3: Run + Commit**

```bash
pytest tests/test_analytics_api.py -v -k "session_runs"
git add server.py tests/test_analytics_api.py
git commit -m "feat(api): GET /api/threads/{thread_id}/runs"
```

---

### Task 17: New `GET /api/analytics/period`

**Files:**
- Modify: `server.py`
- Test: `tests/test_analytics_api.py`

- [ ] **Step 1: Write failing tests**

```python
def test_analytics_period_aggregates(client, qwe_temp_data_dir):
    import db, time
    for src, tok in [("web", 100), ("routine", 200), ("synthesis", 50)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok)
    r = client.get("/api/analytics/period?days=30")
    j = r.json()
    assert j["total_input_tokens"] == 350
    assert "by_source" in j and "synthesis" in j["by_source"]


def test_analytics_period_source_filter(client, qwe_temp_data_dir):
    import db, time
    for src, tok in [("web", 100), ("routine", 200)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok)
    r = client.get("/api/analytics/period?days=30&source=routine")
    assert r.json()["total_input_tokens"] == 200
```

- [ ] **Step 2: Add the endpoint**

```python
@app.get("/api/analytics/period")
async def get_analytics_period(days: int = 30, source: str | None = None):
    import db, time
    end_ts = time.time()
    start_ts = end_ts - max(1, int(days)) * 86400
    src = None if not source or source == "all" else source
    return db.get_period_totals(start_ts, end_ts, source=src)
```

- [ ] **Step 3: Run + Commit**

```bash
pytest tests/test_analytics_api.py -v -k "period"
git add server.py tests/test_analytics_api.py
git commit -m "feat(api): GET /api/analytics/period with source filter"
```

---

### Task 18: Pricing API — status + refresh

**Files:**
- Modify: `server.py`
- Test: `tests/test_analytics_api.py`

- [ ] **Step 1: Failing tests**

```python
def test_pricing_status(client, qwe_temp_data_dir):
    r = client.get("/api/pricing/status")
    j = r.json()
    assert "model_count" in j
    assert "source_url" in j
    assert "auto_update" in j


def test_pricing_refresh_success(client, qwe_temp_data_dir, monkeypatch):
    import pricing
    monkeypatch.setattr(pricing, "refresh_pricing", lambda force=False: True)
    monkeypatch.setattr(pricing, "all_known_models", lambda: ["x", "y"])
    r = client.post("/api/pricing/refresh")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_pricing_refresh_failure(client, qwe_temp_data_dir, monkeypatch):
    import pricing
    monkeypatch.setattr(pricing, "refresh_pricing", lambda force=False: False)
    r = client.post("/api/pricing/refresh")
    assert r.status_code == 502
    assert r.json()["ok"] is False
```

- [ ] **Step 2: Add endpoints**

```python
@app.get("/api/pricing/status")
async def pricing_status():
    import pricing, config, time
    fetched = pricing.last_updated()
    return {
        "last_updated": fetched,
        "model_count": len(pricing.all_known_models()),
        "source_url": config.get("pricing_url"),
        "auto_update": config.get("pricing_auto_update"),
        "cache_age_sec": (time.time() - fetched) if fetched else None,
    }


@app.post("/api/pricing/refresh")
async def pricing_refresh():
    import pricing
    ok = pricing.refresh_pricing(force=True)
    if not ok:
        return JSONResponse({"ok": False, "error": "refresh failed"}, status_code=502)
    return {"ok": True, "model_count": len(pricing.all_known_models()),
            "fetched_at": pricing.last_updated()}
```

(Ensure `JSONResponse` is imported from `fastapi.responses`.)

- [ ] **Step 3: Run + Commit**

```bash
pytest tests/test_analytics_api.py -v -k "pricing"
git add server.py tests/test_analytics_api.py
git commit -m "feat(api): GET /api/pricing/status + POST /api/pricing/refresh"
```

---

### Task 19: Re-wire `GET /api/routines/{cron_id}/runs`

**Files:**
- Modify: `server.py`
- Test: `tests/test_analytics_api.py`

- [ ] **Step 1: Failing test**

```python
def test_routine_runs_endpoint(client, qwe_temp_data_dir):
    import db, time
    rid = db.insert_agent_run(thread_id="t1", cron_id=42, source="routine",
                              started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                          status="ok", input_tokens=300, output_tokens=80,
                          cost_usd=0.005)
    r = client.get("/api/routines/42/runs")
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["cost_usd"] == 0.005
    assert runs[0]["input_tokens"] == 300
```

- [ ] **Step 2: Update handler**

Replace the body of the existing `/api/routines/{cron_id}/runs` handler to call `db.get_runs_for_routine(cron_id, limit=...)`.

- [ ] **Step 3: Run + Commit**

```bash
pytest tests/test_analytics_api.py::test_routine_runs_endpoint -v
git add server.py tests/test_analytics_api.py
git commit -m "refactor(api): /api/routines/{id}/runs reads agent_runs"
```

---

## Phase 5 — UI

### Task 20: Sessions list — Tokens + Cost columns

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Locate the sessions render function**

Search `static/index.html` for `renderSessionsList` or the closest equivalent (likely `renderSessions` / `wireSessions`). Identify the table-row template literal.

- [ ] **Step 2: Add columns**

In the `<thead>` template, add two `<th>` cells: "Tokens (in/out)" and "Cost".
In the row template, add two `<td>` cells reading from `session.input_tokens`, `session.output_tokens`, `session.cost_usd`. Use existing formatting helpers (`formatNumber`, `formatCurrency`); if they don't exist, define inline at the top:

```js
function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'k';
  return String(n);
}
function fmtCost(c) {
  if (c == null) return '—';
  if (c < 0.01) return '$' + c.toFixed(4);
  return '$' + c.toFixed(2);
}
```

- [ ] **Step 3: JS lint**

Run: `python scripts/check_js.py`
Expected: clean (no `node --check` errors).

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): tokens + cost columns in Sessions list"
```

---

### Task 21: `SessionRunsModal` component

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add the modal HTML scaffold**

Append a `<div id="sessionRunsModal" class="hidden">...</div>` near the other modals in the template (search for `class="modal"` to find the convention used by skill_creator / settings modals).

- [ ] **Step 2: Add the JS**

```js
async function openSessionRunsModal(threadId, title) {
  const r = await api(`/api/threads/${encodeURIComponent(threadId)}/runs`);
  const totals = await api(`/api/threads`); // already cached on page, pick this thread
  const row = totals.find(s => s.thread_id === threadId);
  state.sessionRunsModal = {
    threadId, title, runs: r,
    totals: row ? {in: row.input_tokens, out: row.output_tokens, cost: row.cost_usd, count: row.run_count} : null,
  };
  document.getElementById('sessionRunsModal').classList.remove('hidden');
  renderSessionRunsModal();
}
function renderSessionRunsModal() {
  const m = state.sessionRunsModal;
  if (!m) return;
  // build table rows; color-code status (.ok = green, .err = red, .aborted = gray)
  // include result_preview on click expand
  // pagination button "Load more" only if runs.length === 50
  // ...
}
```

(Implementation detail — full template literal goes here, ~80 lines.)

- [ ] **Step 3: Wire row clicks**

In Sessions list row template, add `onclick="openSessionRunsModal('${session.thread_id}', '${session.title}')"`.

- [ ] **Step 4: JS lint**

Run: `python scripts/check_js.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): SessionRunsModal drilldown with per-run timeline"
```

---

### Task 22: Topline analytics widget

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add widget HTML above Sessions table**

```html
<div class="topline-stats">
  <span id="topline-summary">Loading…</span>
  <select id="topline-source">
    <option value="all">All sources</option>
    <option value="web">Web</option>
    <option value="cli">CLI</option>
    <option value="telegram">Telegram</option>
    <option value="routine">Routines</option>
    <option value="synthesis">Synthesis</option>
    <option value="skill_creator">Skill creator</option>
  </select>
  <button id="refresh-pricing">Refresh pricing</button>
</div>
```

- [ ] **Step 2: Add JS to populate**

```js
async function loadTopline() {
  const src = document.getElementById('topline-source').value;
  const q = src === 'all' ? '' : `&source=${src}`;
  const j = await api(`/api/analytics/period?days=30${q}`);
  document.getElementById('topline-summary').textContent =
    `Past 30d: ${fmtTokens(j.total_input_tokens)} in · ` +
    `${fmtTokens(j.total_output_tokens)} out · ` +
    `${fmtCost(j.total_cost_usd)} · ${j.run_count} runs`;
}
// wire onchange + on initial load
```

- [ ] **Step 3: Wire refresh button**

```js
document.getElementById('refresh-pricing').onclick = async () => {
  const j = await api('/api/pricing/refresh', {method: 'POST'});
  showToast(j.ok ? `Pricing updated (${j.model_count} models)` : 'Refresh failed');
};
```

- [ ] **Step 4: Lint + Commit**

```bash
python scripts/check_js.py
git add static/index.html
git commit -m "feat(ui): topline 30-day analytics widget on Sessions page"
```

---

### Task 23: Settings → Cost tracking

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Find Settings render**

Search for `renderSettings` or the section that wires existing `EDITABLE_SETTINGS`.

- [ ] **Step 2: Add Cost Tracking sub-section**

Render a new card titled "Cost tracking" with:
- Text input bound to `pricing_url` setting + "Restore default" button (resets to the bundled default URL).
- Checkbox bound to `pricing_auto_update` setting.
- Read-only "Last updated" row showing `pricing.last_updated()` formatted as `YYYY-MM-DD HH:MM` + model count.
- Button "Refresh now" wired to `POST /api/pricing/refresh`.
- "Per-model overrides" sub-list (read from `kv_get_prefix('pricing_override_')`; UI for add/remove writes via existing settings KV API).

- [ ] **Step 3: Lint + Commit**

```bash
python scripts/check_js.py
git add static/index.html
git commit -m "feat(ui): Settings → Cost tracking section"
```

---

### Task 24: Routines page — Cost (30d) column

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Locate routines render**

Search for `renderRoutines` or the cron table render.

- [ ] **Step 2: Fetch cost per routine**

After fetching routines from the existing endpoint, for each row call:

```js
const since = (Date.now()/1000) - 30*86400;
// Single batched call would be nicer, but for MVP one /api/routines/{id}/runs?limit=500 per row,
// summing client-side, is acceptable.
const runs = await api(`/api/routines/${id}/runs?limit=500`);
const cost30d = runs
  .filter(r => r.started_at >= since)
  .reduce((s, r) => s + (r.cost_usd || 0), 0);
```

(If perf is a concern with many routines, optimize via a new `/api/routines/cost-summary?days=30` endpoint — but defer to a follow-up if needed.)

- [ ] **Step 3: Add column to render**

In the routines table thead/tbody, render `Cost (30d)` column with `fmtCost(cost30d)`.

- [ ] **Step 4: Lint + Commit**

```bash
python scripts/check_js.py
git add static/index.html
git commit -m "feat(ui): Routines page — Cost (30d) column"
```

---

## Phase 6 — Polish

### Task 25: Telemetry feature_first_use trigger + consent bump

**Files:**
- Modify: `telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Add the FEATURES enum entry**

In `telemetry.py`, locate `FEATURES = frozenset({...})` at line 205. Frozensets are immutable — you cannot `.append()`. Edit the literal directly: add `"cost_tracking",` as a new element inside the `frozenset({...})` braces.

- [ ] **Step 2: Bump consent version**

Same file: find `_CURRENT_CONSENT_VERSION` constant. Increment by 1.

- [ ] **Step 3: Wire the trigger**

In `server.py` (or a more appropriate spot), the first time `SessionRunsModal` opens we want to fire `telemetry.track_event("feature_first_use", {"feature": "cost_tracking"})`. Easiest: do it on the server-side handler the first time `GET /api/threads/{id}/runs` is called per process — track a module-level `_first_use_seen = False` flag and call the telemetry helper once.

- [ ] **Step 4: Test**

Append to `tests/test_telemetry.py`:

```python
def test_cost_tracking_feature_in_enum():
    import telemetry
    assert "cost_tracking" in telemetry.FEATURES

def test_consent_version_bumped():
    import telemetry
    assert telemetry._CURRENT_CONSENT_VERSION >= 2  # whatever the new floor is
```

- [ ] **Step 5: Run + Commit**

```bash
pytest tests/test_telemetry.py -v
git add telemetry.py server.py tests/test_telemetry.py
git commit -m "feat(telemetry): cost_tracking feature_first_use + consent bump"
```

---

### Task 26: Integration tests — end-to-end agent_runs population

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Add focused end-to-end tests**

Append:

```python
def test_full_turn_creates_one_agent_run(qwe_temp_data_dir, mock_llm):
    import agent, db
    agent.run("hi", thread_id="full", source="cli")
    rows = db.get_runs_for_thread("full")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["input_tokens"] > 0 or rows[0]["output_tokens"] > 0

def test_compaction_folds_into_parent_run(qwe_temp_data_dir, mock_llm):
    # Force compaction by setting a tiny context_budget; verify only 1 row appears
    import agent, config, db
    config.set("context_budget", 100)
    for _ in range(5):
        agent.run("hi", thread_id="compact", source="cli")
    rows = db.get_runs_for_thread("compact")
    # 5 turns → 5 rows; compaction internal LLM calls don't add new rows
    assert len(rows) == 5
```

- [ ] **Step 2: Run + Commit**

```bash
pytest tests/test_integration.py -v -k "agent_run or compaction_folds"
git add tests/test_integration.py
git commit -m "test(integration): agent_runs end-to-end + compaction fold-in"
```

---

### Task 27: Public-facing docs

**Files:**
- Create: `docs/COST_TRACKING.md`
- Modify: `CLAUDE.md`
- Modify: `RELEASE_NOTES.md`
- Modify: `config.py` (version bump)
- Modify: `pyproject.toml` (version bump)

- [ ] **Step 1: Write `docs/COST_TRACKING.md`**

Sections:
- What gets tracked (all LLM call sites)
- Where the data lives (`agent_runs` table)
- Where to view it (Sessions list, SessionRunsModal, Routines page)
- How pricing is sourced (LiteLLM + cache + bundled fallback)
- How to set up an air-gapped mirror
- How to add a per-model override
- Privacy: no data leaves the machine (except pricing GET)

- [ ] **Step 2: Update `CLAUDE.md`**

Add a "Cost tracking" sub-section under Architecture, paragraph-style, mirroring the existing Telemetry / SQLite migrations sub-sections:

- One paragraph: what `agent_runs` table is
- One paragraph: `pricing.py` module + LiteLLM source + fallback chain
- One paragraph: instrumentation points (where rows are written)
- Pointer to `docs/COST_TRACKING.md`

- [ ] **Step 3: Update `RELEASE_NOTES.md`**

Add at the top:

```markdown
## v0.19.0 — Cost tracking & per-session analytics

- New `agent_runs` table replaces `routine_runs`: one row per LLM call site
  (main loop, synthesis, skill creator, routine fire) with full token + cost
  capture.
- Online pricing from the LiteLLM community JSON, cached locally, with a
  bundled top-10 fallback for offline / air-gapped operation.
- Sessions list now shows Tokens + Cost per thread; click a row for a
  per-run drilldown with model, source, status, duration, tokens, and cost.
- Routines page shows Cost (30d) so you can spot expensive scheduled jobs.
- New Settings → Cost tracking section: pricing URL, auto-update toggle,
  manual refresh button, per-model overrides.
- API: `GET /api/threads` extended with `input_tokens / output_tokens /
  cost_usd / run_count`; new `GET /api/threads/{id}/runs`,
  `GET /api/analytics/period`, `GET /api/pricing/status`,
  `POST /api/pricing/refresh`.
- Migration 008 atomically copies legacy `routine_runs` into the new table
  and drops the old one.
```

- [ ] **Step 4: Version bumps**

In `config.py`: `VERSION = "0.19.0"`.
In `pyproject.toml`: `version = "0.19.0"`.
README badge if it references the version.

- [ ] **Step 5: Verify**

Run: `ruff check . && python scripts/check_js.py && pytest tests/ -q`
Expected: all clean.

- [ ] **Step 6: Final commit**

```bash
git add docs/ CLAUDE.md RELEASE_NOTES.md config.py pyproject.toml README.md
git commit -m "docs: cost tracking guide + CLAUDE/release notes for v0.19.0"
```

---

## Phase 7 — Final verification

### Task 28: Full-suite lint + test pass

- [ ] **Step 1: Run all gates**

```bash
ruff check .
python scripts/check_js.py
python -c "import ast, pathlib
for p in pathlib.Path('.').glob('*.py'):
    ast.parse(p.read_text(encoding='utf-8'), filename=str(p), feature_version=(3,11))"
pytest tests/ --cov --cov-report=term
```

Expected:
- `ruff`: clean
- `check_js.py`: clean
- AST parse: clean (no PEP 701 leaks)
- pytest: ~565 tests passing (was 495), coverage ≥ 24%.

- [ ] **Step 2: Smoke-test server boot**

```bash
python -c "import server; print('imports clean')"
# Optionally start the server briefly:
python cli.py --web --port 7861 &
sleep 3
curl -s http://localhost:7861/api/pricing/status | head
kill %1
```

Expected: `model_count >= 10` (bundled fallback present even before first network fetch).

- [ ] **Step 3: Final review commit (if anything needed touching)**

Nothing should need fixing if Tasks 1-27 were followed carefully.

---

## Risks & mitigations recap

| Risk | Mitigation (already in plan) |
|---|---|
| Migration 008 corrupts data | Atomic transaction; CI test loads pre-7 fixture, applies, verifies row count |
| LiteLLM JSON schema drift | `_normalize_litellm` skips entries with missing fields, falls back to bundled |
| Provider/model name mismatch (e.g. `groq/llama-...` vs `llama-...`) | Document in spec §14 #2; if seen in practice, add a small alias map in pricing.py |
| Streaming usage missing on aborted turns | Spec §14 #1: surface "tokens may be incomplete" hint in UI for `status='aborted'` |
| Background refresher crashes loop | Daemon thread with broad `except Exception` + `_log.warning` — never re-raises |
| Per-model override invalid JSON | `get_price` catches and logs; falls through to bundled/None |

---

## Definition of done (mirrors spec §15)

- [ ] `pricing.py` module implemented, ≥30 unit tests passing.
- [ ] Migration `008_agent_runs.sql` written, atomic on rollout.
- [ ] All five instrumentation points wired (`agent_loop`, `synthesis`, `skill_creator`, `scheduler`, db helpers).
- [ ] New / modified API endpoints return documented shapes; ≥15 API tests passing.
- [ ] Sessions list shows Tokens + Cost columns; SessionRunsModal opens on row click; topline widget on top of page.
- [ ] Settings → Cost tracking section functional.
- [ ] Routines page shows Cost (30d) column.
- [ ] `docs/COST_TRACKING.md` written; `CLAUDE.md` updated.
- [ ] `RELEASE_NOTES.md` entry for v0.19.0; version bumped in `config.py` + `pyproject.toml`.
- [ ] `ruff check .` clean; `python scripts/check_js.py` clean; `pytest tests/` all green; coverage ≥ 24%.
