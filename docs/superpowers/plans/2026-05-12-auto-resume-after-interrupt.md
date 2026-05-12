# Auto-Resume After Interrupt Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every abort (WS disconnect, Stop button, server crash, network blip) recoverable — across Web, Telegram, and Routine sources — by persisting the partial assistant reply and offering a Resume action that continues from where the agent left off (not from scratch).

**Architecture:** Two new `agent_runs` columns (`resumed_from_run_id`, `dismissed_at`) chain resume runs to their originals; the existing agent_loop `finally:` block (from spec #1 Task 11) flushes partial content into `messages.meta.interrupted=true`; a server startup hook promotes orphaned `running` rows to `aborted`; a universal `agent.resume_interrupted_run` helper fires a new agent.run with a real system-role nudge (`system_note=` parameter, **not** a `[system]` user-message prefix — that's a CLAUDE.md anti-pattern); per-source trigger paths (Web banner / Telegram `/resume` / Routine auto-fire within 5 min) all converge on the same executor.

**Tech Stack:** Python 3.11+, SQLite, FastAPI WS, vanilla-JS SPA (`static/index.html`), pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-05-12-auto-resume-after-interrupt-design.md`](../specs/2026-05-12-auto-resume-after-interrupt-design.md)

**Depends on spec #1** (PR #26, merged or branch `feat/cost-tracking`): `agent_runs` table, `db.insert_agent_run` / `finalize_agent_run`, agent_loop `finally:` block, `TurnContext.cron_id`, analytics endpoints.

---

## File Structure

### New files

- `migrations/009_interrupted_runs.sql` — add `resumed_from_run_id` + `dismissed_at` columns to `agent_runs`
- `tests/test_resume.py` — ~25 unit tests for resume execution + crash recovery + scheduler auto-fire
- `tests/test_resume_api.py` — ~10 tests for HTTP/WS endpoints
- `docs/AUTO_RESUME.md` — user-facing guide

### Modified files

- `db.py` — extend `insert_agent_run` signature; add `dismiss_run`, `get_resumable_run_for_thread`
- `turn_context.py` — add `resumed_from_run_id: Optional[int] = None`
- `agent.py` — add `resume_interrupted_run` function; add `system_note=` parameter to `run`
- `agent_loop.py` — accept `system_note=`; extend `finally:` to flush partial content; pass `resumed_from_run_id` from ctx into `insert_agent_run`
- `server.py` — `_recover_interrupted_runs_on_startup`, `_check_for_resumable_interrupt`, two `/api/resume/*` endpoints, wire startup hook
- `scheduler.py` — extend `detect_missed_runs` with routine auto-resume window
- `telegram_bot.py` — `/resume` command handler
- `static/index.html` — banner HTML/JS/CSS, inline `⏸ interrupted` marker on assistant messages, sidebar chip, Settings → Cost → Auto-resume sub-section
- `config.py` — add 4 settings (`resume_ttl_web_sec`, `resume_ttl_telegram_sec`, `resume_ttl_routine_sec`, `resume_routine_auto`)
- `telemetry.py` — add `"auto_resume"` to `FEATURES`; bump `_CURRENT_CONSENT_VERSION`
- `tests/test_integration.py` — extend with full-cycle test
- `CLAUDE.md` — Cost-tracking section gets an "Auto-resume" sub-section
- `RELEASE_NOTES.md` — v0.20.0 entry
- `pyproject.toml`, `config.py` (VERSION), `README.md` (badge) — version bump

---

## Implementation Phases

21 tasks across 6 phases. Phase 1 lays foundation (schema + signature extensions); Phase 2 adds the abort + recovery side; Phase 3 adds the resume executor; Phase 4 wires per-source triggers; Phase 5 lights up the UI; Phase 6 wraps with telemetry + docs + verification.

---

## Phase 1 — Foundation

### Task 1: Migration 009 — `agent_runs` columns for resume

**Files:**
- Create: `migrations/009_interrupted_runs.sql`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: Write the migration**

Create `migrations/009_interrupted_runs.sql`:

```sql
-- v0.20.0: support auto-resume for interrupted turns.
--
-- Two narrow additions on agent_runs (created by migration 008):
--   1. resumed_from_run_id — chains resume runs back to their original
--      aborted run, so analytics can show "run #142 (resumed #138)".
--   2. dismissed_at — user clicked Dismiss on the banner, or TTL
--      expired. Filtered out of "resume?" prompts.
--
-- The 'interrupted' marker on individual partial messages lives in
-- messages.meta (existing JSON column) — no schema change for that.
BEGIN;

ALTER TABLE agent_runs ADD COLUMN resumed_from_run_id INTEGER;
ALTER TABLE agent_runs ADD COLUMN dismissed_at REAL;

CREATE INDEX IF NOT EXISTS idx_agent_runs_dismissed_at
    ON agent_runs(dismissed_at);

COMMIT;
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_migrations.py`:

```python
def test_migration_009_adds_resume_columns(qwe_temp_data_dir):
    import db
    db._migrated = False
    conn = db._get_conn()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    assert "resumed_from_run_id" in cols
    assert "dismissed_at" in cols

def test_migration_009_adds_dismissed_at_index(qwe_temp_data_dir):
    import db
    db._migrated = False
    conn = db._get_conn()
    indexes = {r[1] for r in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='agent_runs'"
    ).fetchall()}
    assert "idx_agent_runs_dismissed_at" in indexes
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_migrations.py -v -k "009"
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add migrations/009_interrupted_runs.sql tests/test_migrations.py
git commit -m "feat(db): migration 009 — resumed_from_run_id + dismissed_at on agent_runs"
```

---

### Task 2: `db.py` — extend `insert_agent_run`, add resume helpers

**Files:**
- Modify: `db.py`
- Test: `tests/test_resume.py` (new file — start it here)

- [ ] **Step 1: Write failing tests**

Create `tests/test_resume.py`:

```python
"""Unit tests for auto-resume after interrupt (v0.20.0)."""
import time
import pytest


def test_insert_agent_run_accepts_resumed_from(qwe_temp_data_dir):
    import db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    resume = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running",
                                  resumed_from_run_id=original)
    row = db._get_conn().execute(
        "SELECT resumed_from_run_id FROM agent_runs WHERE id=?", (resume,)
    ).fetchone()
    assert row[0] == original


def test_dismiss_run_sets_dismissed_at(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    row = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] is not None and row[0] > 0


def test_dismiss_run_is_idempotent(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    first = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()[0]
    db.dismiss_run(rid)
    second = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()[0]
    assert first == second  # not overwritten on second call


def test_get_resumable_run_for_thread_happy(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted",
                           result_preview="partial reply preview")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is not None
    assert found["id"] == rid


def test_get_resumable_run_excludes_cli(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="cli",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_get_resumable_run_respects_ttl(qwe_temp_data_dir):
    import db
    long_ago = time.time() - 86400 - 1
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=long_ago, status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_get_resumable_run_excludes_dismissed(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_get_resumable_run_excludes_resume_runs(qwe_temp_data_dir):
    """A row that is itself a resume of something else is not offered for re-resume."""
    import db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    db.finalize_agent_run(original, finished_at=None, duration_ms=None, status="aborted")
    resume = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running",
                                  resumed_from_run_id=original)
    db.finalize_agent_run(resume, finished_at=None, duration_ms=None, status="aborted")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    # The MOST RECENT aborted run that ISN'T itself a resume is the original.
    # But the original was already resumed (by `resume`), so it's also filtered out.
    # Result: no resumable run.
    assert found is None


def test_get_resumable_run_excludes_already_resumed_original(qwe_temp_data_dir):
    """An original that's already been resumed-from cannot be resumed again."""
    import db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    db.finalize_agent_run(original, finished_at=None, duration_ms=None, status="aborted")
    db.insert_agent_run(thread_id="t1", source="web",
                         started_at=time.time(), status="running",
                         resumed_from_run_id=original)
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_resume.py -v
```
Expected: FAIL with `TypeError: unexpected keyword argument 'resumed_from_run_id'` or `AttributeError: module 'db' has no attribute 'dismiss_run'`.

- [ ] **Step 3: Extend `db.insert_agent_run`**

In `db.py`, modify the `insert_agent_run` signature:

```python
def insert_agent_run(
    thread_id: str,
    source: str,
    started_at: float,
    status: str = "running",
    cron_id: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    scheduled_at: float | None = None,
    resumed_from_run_id: int | None = None,   # NEW
) -> int:
    """Insert a new run row. Returns the new id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO agent_runs (thread_id, cron_id, source, scheduled_at, "
        " started_at, status, model, provider, resumed_from_run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (thread_id, cron_id, source, scheduled_at, started_at, status,
         model, provider, resumed_from_run_id),
    )
    conn.commit()
    return int(cur.lastrowid)
```

- [ ] **Step 4: Add `dismiss_run` and `get_resumable_run_for_thread`**

Append to `db.py`:

```python
def dismiss_run(run_id: int) -> None:
    """Mark a run dismissed (idempotent — does not overwrite existing dismissed_at)."""
    import time as _t
    conn = _get_conn()
    conn.execute(
        "UPDATE agent_runs SET dismissed_at = ? "
        "WHERE id = ? AND dismissed_at IS NULL",
        (_t.time(), int(run_id)),
    )
    conn.commit()


def get_resumable_run_for_thread(
    thread_id: str,
    source_filter: str | None = None,
    ttl_sec: float = 604800,
) -> dict | None:
    """Return the most recent aborted run on this thread that's still
    eligible for resume, or None.

    Filters out:
      - non-aborted statuses
      - dismissed runs
      - runs older than ttl_sec
      - runs that are themselves resume runs (resumed_from_run_id NOT NULL)
      - runs that have already been resumed-from (referenced by some later row)
      - source='cli' (Ctrl+C is intentional stop)
      - optionally: anything not matching source_filter
    """
    import time as _t
    cutoff = _t.time() - float(ttl_sec)
    params: list = [thread_id, cutoff]
    src_clause = ""
    if source_filter is not None:
        src_clause = " AND source = ?"
        params.append(source_filter)

    # Two related-but-different guards:
    #   resumed_from_run_id IS NULL  → row is not itself a resume run
    #   id NOT IN (...)              → no later row already resumed from it
    # Both intentional; do not collapse.
    sql = (
        "SELECT id, started_at, result_preview, model, source, cron_id "
        "FROM agent_runs "
        "WHERE thread_id = ? AND status = 'aborted' "
        "  AND dismissed_at IS NULL "
        "  AND started_at >= ? "
        "  AND resumed_from_run_id IS NULL "
        "  AND source != 'cli' "
        "  AND id NOT IN (SELECT resumed_from_run_id FROM agent_runs "
        "                 WHERE resumed_from_run_id IS NOT NULL) "
        f"{src_clause} "
        "ORDER BY id DESC LIMIT 1"
    )
    conn = _get_conn()
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "started_at": row[1],
        "result_preview": row[2],
        "model": row[3],
        "source": row[4],
        "cron_id": row[5],
    }
```

- [ ] **Step 5: Re-run tests**

```bash
pytest tests/test_resume.py -v
```
Expected: 8 PASS.

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_resume.py
git commit -m "feat(db): insert_agent_run accepts resumed_from_run_id; add dismiss_run + get_resumable_run_for_thread"
```

---

### Task 3: TurnContext — add `resumed_from_run_id`

**Files:**
- Modify: `turn_context.py`
- Test: `tests/test_turn_context.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_turn_context.py`:

```python
def test_turn_context_has_resumed_from_run_id_default_none():
    from turn_context import TurnContext
    ctx = TurnContext()
    assert ctx.resumed_from_run_id is None

def test_turn_context_accepts_resumed_from_run_id():
    from turn_context import TurnContext
    ctx = TurnContext(resumed_from_run_id=42)
    assert ctx.resumed_from_run_id == 42
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Add field**

In `turn_context.py`, after the `cron_id` field (added in spec #1 Task 1):

```python
    # Set when this turn resumes a previously aborted run. agent_loop reads
    # it and stores it on the new agent_runs row so analytics can chain
    # the resume back to its original.
    resumed_from_run_id: Optional[int] = None
```

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git add turn_context.py tests/test_turn_context.py
git commit -m "feat(turn_context): add resumed_from_run_id field for resume chaining"
```

---

### Task 4: `agent.run` — `system_note` parameter

**Files:**
- Modify: `agent.py`
- Modify: `agent_loop.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_resume.py`:

```python
def test_agent_run_accepts_system_note(qwe_temp_data_dir, mock_llm):
    """system_note=... should not crash; it gets injected as a system
    message in the next LLM call (not persisted)."""
    import agent
    out = agent.run(
        user_input=None,
        thread_id="t1",
        system_note="Continue from where you left off.",
        source="cli",
    )
    assert out is not None  # returns whatever run() returns; just smoke
```

- [ ] **Step 2: Find current `agent.run` signature**

```bash
grep -n "^def run(\|^def run\>" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/agent.py | head -5
```

Read ~20 lines below the match to understand the existing signature.

- [ ] **Step 3: Add `system_note` parameter to `agent.run`**

In `agent.py`, extend the `run` function signature:

```python
def run(
    user_input: str | None,
    thread_id: str | None = None,
    source: str = "cli",
    ctx: "TurnContext | None" = None,
    abort_event: "threading.Event | None" = None,
    system_note: str | None = None,   # NEW
    # ... existing params unchanged ...
) -> dict:
```

Update the docstring to describe `system_note`: "Optional one-shot system message prepended to the next LLM call only. Not persisted to messages. Used by resume_interrupted_run to nudge the model into 'continue' mode without injecting a [system] user-role message."

Pass it through to `agent_loop.run_loop`:

```python
return agent_loop.run_loop(
    # ... existing args ...
    system_note=system_note,
)
```

Also handle `user_input is None`: if both `user_input` and `system_note` are None, raise ValueError. If `user_input is None` and `system_note` is set, don't save a user message — just go straight to the LLM call with the system note prepended.

- [ ] **Step 4: Accept `system_note` in `agent_loop.run_loop`**

Add `system_note: str | None = None` to `run_loop`'s signature. Where the chat completion request is built, prepend the system note to the messages list:

```python
messages = _build_messages(...)
if system_note:
    messages = [{"role": "system", "content": system_note}] + messages
```

(Or insert it after existing system messages — find where `messages` is constructed and place appropriately.)

- [ ] **Step 5: Run + commit**

```bash
pytest tests/test_resume.py::test_agent_run_accepts_system_note -v
git add agent.py agent_loop.py tests/test_resume.py
git commit -m "feat(agent): system_note parameter on agent.run for one-shot system message"
```

---

### Task 5: 4 settings in `config.py`

**Files:**
- Modify: `config.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Add settings**

Find `EDITABLE_SETTINGS` in `config.py`. Append (match the existing 6-tuple shape — verify with `grep -n "EDITABLE_SETTINGS" config.py`):

```python
    ("resume_ttl_web_sec",       int,  604800, "How long (sec) a Web abort stays resumable. Default 7 days.", 60, 31536000),
    ("resume_ttl_telegram_sec",  int,  86400,  "How long (sec) a Telegram abort stays resumable. Default 24h.", 60, 31536000),
    ("resume_ttl_routine_sec",   int,  300,    "Window (sec) for auto-firing aborted routines on server start. Default 5 min.", 0, 86400),
    ("resume_routine_auto",      bool, True,   "Enable/disable routine auto-resume entirely.", None, None),
```

- [ ] **Step 2: Test**

Append:

```python
def test_resume_settings_have_defaults(qwe_temp_data_dir):
    import config
    assert config.get("resume_ttl_web_sec") == 604800
    assert config.get("resume_ttl_telegram_sec") == 86400
    assert config.get("resume_ttl_routine_sec") == 300
    assert config.get("resume_routine_auto") is True
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/test_resume.py::test_resume_settings_have_defaults -v
git add config.py tests/test_resume.py
git commit -m "feat(config): add resume_ttl_* + resume_routine_auto settings"
```

---

## Phase 2 — Abort persistence + server crash recovery

### Task 6: Extend agent_loop `finally:` — flush partial content

**Files:**
- Modify: `agent_loop.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Failing test**

Append:

```python
def test_abort_flushes_partial_content(qwe_temp_data_dir, mock_llm, monkeypatch):
    """When agent_loop aborts mid-stream, the partial assistant content
    should be saved as a messages row with meta.interrupted=true."""
    import agent, db
    # Configure the mock LLM to emit a few tokens then "abort" by setting
    # the abort_event mid-stream.
    # ... (depends on mock_llm fixture shape — adapt to existing pattern)
    # The simplest approach: fire abort_event from a side thread after
    # mock_llm has yielded ~3 chunks.
    import threading, time as _t
    
    def trigger_abort_after_short_delay(ctx):
        _t.sleep(0.05)
        ctx.abort_event.set()
    
    from turn_context import TurnContext
    ctx = TurnContext(source="web")
    threading.Thread(target=trigger_abort_after_short_delay, args=(ctx,), daemon=True).start()
    
    try:
        agent.run("hello world", thread_id="t-abort", source="web", ctx=ctx)
    except Exception:
        pass  # abort may or may not raise; either is fine
    
    rows = db._get_conn().execute(
        "SELECT role, content, meta FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT 3",
        ("t-abort",)
    ).fetchall()
    # Find any assistant row with meta.interrupted=true
    import json
    interrupted = [r for r in rows if r[0] == "assistant" and r[2] and 
                   json.loads(r[2]).get("interrupted") is True]
    assert len(interrupted) >= 1, f"no interrupted assistant message found: {rows}"
```

This test is flaky-prone (depends on timing of mock_llm chunks); if it can't be made reliable, replace with a unit test that directly invokes the `finally:` block logic by mocking what `run_loop` would have done. Acceptable fallback: assert that when abort is triggered before a normal turn completes, `agent_runs` row has `status='aborted'` AND a corresponding `messages` row with `meta.interrupted=true` exists.

- [ ] **Step 2: Find the existing `finally:` in `run_loop`**

```bash
grep -n "_final_status\|finalize_agent_run\|final_content" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/agent_loop.py | head -15
```

The finally block was added in spec #1 Task 11. Locate it (around line 815 in current `agent_loop.py`).

- [ ] **Step 3: Insert the flush logic**

In `agent_loop.py`, inside the existing `finally:` block, **immediately before** the existing `db.finalize_agent_run(...)` call, add:

```python
_is_aborted = (_final_status == "aborted")

# NEW: flush partial assistant content as a regular message row so
# resume sees it in conversation history. On clean exit, agent.py's
# reply-save path handles this — skip.
if _is_aborted and final_content:
    try:
        db.save_message(
            role="assistant",
            content=final_content,
            thread_id=thread_id,
            meta={
                "interrupted": True,
                "run_id": _run_id,
                "partial_tokens": {
                    "input": int(stats.input_tokens or 0),
                    "output": int(stats.output_tokens or 0),
                },
            },
        )
    except Exception as e:
        _log.debug(f"interrupt flush failed: {e}")
```

(`final_content` is the existing streaming buffer variable from spec #1 Task 11.)

- [ ] **Step 4: Verify save_message accepts `meta` as dict**

```bash
grep -n "def save_message" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/db.py
```

Read the function. If `meta` is already a dict parameter that gets JSON-serialized into the column, no work needed. If `meta` is a string parameter, wrap: `meta=json.dumps({...})`. Update the spec implementation accordingly.

- [ ] **Step 5: Run + commit**

```bash
pytest tests/test_resume.py::test_abort_flushes_partial_content -v
git add agent_loop.py tests/test_resume.py
git commit -m "feat(agent_loop): flush partial content on abort with meta.interrupted"
```

---

### Task 7: Server crash recovery hook

**Files:**
- Modify: `server.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Failing test**

```python
def test_crash_recovery_promotes_running_to_aborted(qwe_temp_data_dir):
    import db
    # Simulate a server crash by leaving a 'running' row
    rid = db.insert_agent_run(thread_id="t-crash", source="web",
                               started_at=time.time(), status="running")
    # Now call the recovery hook directly
    from server import _recover_interrupted_runs_on_startup
    _recover_interrupted_runs_on_startup()
    
    row = db._get_conn().execute(
        "SELECT status, error FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] == "aborted"
    assert "restart" in (row[1] or "").lower()
    
    # And the synthesized message marker should be present
    import json
    m = db._get_conn().execute(
        "SELECT meta FROM messages WHERE thread_id='t-crash' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert m is not None
    meta = json.loads(m[0])
    assert meta.get("interrupted") is True
    assert meta.get("crash_recovery") is True
    assert meta.get("run_id") == rid
```

- [ ] **Step 2: Implement the hook**

In `server.py`, near the existing startup logic (search for `pricing.start_background_refresher` from spec #1 Task 10), add ABOVE that call:

```python
def _recover_interrupted_runs_on_startup() -> None:
    """Mark orphaned 'running' agent_runs as 'aborted' at server start.
    
    Synthesizes an abort marker in messages so the run appears interrupted
    in the UI. Does NOT auto-resume — Web banner / Telegram /resume /
    scheduler auto-fire handle user-facing recovery. Must run BEFORE
    scheduler.start() so scheduler.detect_missed_runs sees the up-to-date
    'aborted' rows for its 5-min auto-fire window.
    """
    import db
    import logger as _logger
    _log = _logger.get("server")
    try:
        rows = db._get_conn().execute(
            "SELECT id, thread_id FROM agent_runs WHERE status='running'"
        ).fetchall()
    except Exception as e:
        _log.warning(f"crash-recovery query failed: {e}")
        return
    for (rid, thread_id) in rows:
        try:
            db.save_message(
                role="assistant", content="",
                thread_id=thread_id,
                meta={"interrupted": True, "run_id": rid,
                      "crash_recovery": True},
            )
        except Exception as e:
            _log.debug(f"crash-recovery save_message failed for #{rid}: {e}")
        try:
            db.finalize_agent_run(
                rid, finished_at=None, duration_ms=None,
                status="aborted", error="server restart",
            )
        except Exception as e:
            _log.debug(f"crash-recovery finalize failed for #{rid}: {e}")
    if rows:
        _log.info(f"recovered {len(rows)} interrupted runs from previous session")
```

Wire it into the startup handler. Find the existing startup (search `on_event("startup")` or the lifespan handler) and insert `_recover_interrupted_runs_on_startup()` BEFORE the call to `pricing.start_background_refresher()` and BEFORE `scheduler.start()`.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/test_resume.py::test_crash_recovery_promotes_running_to_aborted -v
git add server.py tests/test_resume.py
git commit -m "feat(server): startup hook recovers orphaned running rows as aborted"
```

---

## Phase 3 — Resume execution

### Task 8: `agent.resume_interrupted_run`

**Files:**
- Modify: `agent.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Failing tests**

```python
def test_resume_interrupted_run_happy(qwe_temp_data_dir, mock_llm):
    import agent, db
    # Set up an aborted run
    original = db.insert_agent_run(thread_id="t-resume", source="web",
                                    started_at=time.time(), status="running")
    db.finalize_agent_run(original, finished_at=None, duration_ms=None,
                           status="aborted",
                           result_preview="I'll start by searching...")
    # Resume
    agent.resume_interrupted_run(original)
    # A new run row exists with resumed_from_run_id = original
    new_run = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE resumed_from_run_id=?", (original,)
    ).fetchone()
    assert new_run is not None


def test_resume_dismissed_run_raises(qwe_temp_data_dir):
    import agent, db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    with pytest.raises(ValueError, match="dismissed"):
        agent.resume_interrupted_run(rid)


def test_resume_unknown_run_raises(qwe_temp_data_dir):
    import agent
    with pytest.raises(ValueError, match="not found"):
        agent.resume_interrupted_run(99999)


def test_resume_already_resumed_raises(qwe_temp_data_dir, mock_llm):
    import agent, db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    db.finalize_agent_run(original, finished_at=None, duration_ms=None, status="aborted")
    # First resume succeeds
    agent.resume_interrupted_run(original)
    # Second attempt fails
    with pytest.raises(ValueError, match="already resumed"):
        agent.resume_interrupted_run(original)
```

- [ ] **Step 2: Implement `resume_interrupted_run`**

Add to `agent.py`:

```python
def resume_interrupted_run(
    run_id: int, ctx: "TurnContext | None" = None
) -> dict:
    """Resume a previously interrupted agent run.

    Loads the run metadata, validates it's resumable, builds (or reuses)
    a TurnContext with the original source / cron_id, and fires a normal
    agent.run with a system_note nudging the model to continue.
    
    The conversation history (loaded inside agent.run via db.list_messages)
    already contains the partial assistant message flushed at abort time,
    so the model sees its own incomplete output and the system_note
    instruction.

    Raises ValueError if the run is unknown / dismissed / already resumed.
    """
    import db
    import turn_context as _tc

    conn = db._get_conn()
    row = conn.execute(
        "SELECT thread_id, source, cron_id, dismissed_at, resumed_from_run_id "
        "FROM agent_runs WHERE id=?",
        (int(run_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"run #{run_id} not found")
    thread_id, source, cron_id, dismissed_at, already_resumed = row
    if dismissed_at is not None:
        raise ValueError(f"run #{run_id} was dismissed")
    if already_resumed is not None:
        raise ValueError(f"run #{run_id} is itself a resume run")

    # Reverse-lookup: was this run already resumed from by something later?
    referenced_by = conn.execute(
        "SELECT id FROM agent_runs WHERE resumed_from_run_id = ?",
        (int(run_id),),
    ).fetchone()
    if referenced_by:
        raise ValueError(
            f"run #{run_id} already resumed by run #{referenced_by[0]}"
        )

    # NOTE: do NOT block CLI source here. Trigger layer (Web banner / Telegram
    # /resume) filters by source; direct executor calls (tests, tooling)
    # accept any source.

    if ctx is None:
        ctx = _tc.TurnContext(
            source=source,
            cron_id=cron_id,
            session_id=f"resume-{run_id}",
        )
    ctx.resumed_from_run_id = int(run_id)

    return run(
        user_input=None,
        system_note=(
            "The previous turn was interrupted before completing. "
            "Continue from where you left off — do not restart, do not "
            "repeat tool calls that already ran. If your prior partial "
            "reply was on the right track, pick up the thread."
        ),
        thread_id=thread_id,
        ctx=ctx,
        source=source,
    )
```

- [ ] **Step 3: Wire `resumed_from_run_id` through agent_loop**

In `agent_loop.run_loop`, where `db.insert_agent_run(...)` is called (added by spec #1 Task 11), pass:

```python
_run_id = db.insert_agent_run(
    thread_id=thread_id,
    source=(ctx.source if ctx else "cli"),
    started_at=_run_started,
    status="running",
    cron_id=(ctx.cron_id if ctx else None),
    model=model,
    provider=provider,
    resumed_from_run_id=(ctx.resumed_from_run_id if ctx else None),  # NEW
)
```

- [ ] **Step 4: Run + commit**

```bash
pytest tests/test_resume.py -v -k "resume_interrupted_run or resume_dismissed or resume_unknown or resume_already"
git add agent.py agent_loop.py tests/test_resume.py
git commit -m "feat(agent): resume_interrupted_run helper + agent_loop wires resumed_from_run_id"
```

---

## Phase 4 — Resume triggers

### Task 9: WS `interrupted_turn` event on connect

**Files:**
- Modify: `server.py`
- Test: `tests/test_resume_api.py` (new)

- [ ] **Step 1: Create the test file**

Create `tests/test_resume_api.py`:

```python
"""HTTP/WS tests for auto-resume endpoints."""
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import server
    return TestClient(server.app)


def test_ws_connect_emits_interrupted_turn(qwe_temp_data_dir, client):
    import db
    rid = db.insert_agent_run(thread_id="t-ws", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None,
                           status="aborted", result_preview="partial")
    # Use TestClient.websocket_connect to receive the event
    # NOTE: depends on the WS handler shape — adapt to existing pattern.
    # The handler accepts auth+thread_id setup before yielding control.
    with client.websocket_connect("/ws?thread_id=t-ws") as ws:
        # Drain initial events; one should be {"event": "interrupted_turn", ...}
        events = []
        try:
            for _ in range(5):
                events.append(ws.receive_json(timeout=1))
        except Exception:
            pass
    interrupted = [e for e in events if e.get("event") == "interrupted_turn"]
    assert len(interrupted) == 1
    assert interrupted[0]["run_id"] == rid
    assert interrupted[0]["thread_id"] == "t-ws"


def test_ws_connect_no_event_for_clean_thread(qwe_temp_data_dir, client):
    with client.websocket_connect("/ws?thread_id=t-clean") as ws:
        try:
            evt = ws.receive_json(timeout=0.5)
            assert evt.get("event") != "interrupted_turn"
        except Exception:
            pass  # timeout is fine — means no event
```

The TestClient WS protocol may vary; if the connect signature differs, adapt. Goal: assert that the `interrupted_turn` event lands on connect for a thread that has an eligible aborted run, and does NOT land for a clean thread.

- [ ] **Step 2: Find WS handler**

```bash
grep -n "@app.websocket\|async def ws_\|websocket_endpoint" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/server.py | head -5
```

Locate where the WS handler authenticates and binds the thread_id.

- [ ] **Step 3: Add `_check_for_resumable_interrupt` and call it**

In `server.py`:

```python
async def _check_for_resumable_interrupt(ws, thread_id: str) -> None:
    """Probe for one resumable aborted run in this thread; emit event."""
    import db, config
    ttl = float(config.get("resume_ttl_web_sec") or 604800)
    row = db.get_resumable_run_for_thread(thread_id, source_filter=None, ttl_sec=ttl)
    if not row:
        return
    # Filter to non-CLI in case source_filter was None; get_resumable already
    # filters source != 'cli' inside, so this is just for Web-banner semantics.
    if row.get("source") == "cli":
        return
    await ws.send_json({
        "event": "interrupted_turn",
        "run_id": row["id"],
        "started_at": row["started_at"],
        "preview": row.get("result_preview") or "",
        "model": row.get("model"),
        "source": row.get("source"),
        "thread_id": thread_id,
    })
```

In the WS handler, after thread_id resolution and BEFORE the main message loop, call:

```python
await _check_for_resumable_interrupt(ws, thread_id)
```

- [ ] **Step 4: Run + commit**

```bash
pytest tests/test_resume_api.py -v -k "ws_connect"
git add server.py tests/test_resume_api.py
git commit -m "feat(server): WS emits interrupted_turn event on connect when eligible"
```

---

### Task 10: `POST /api/resume/{run_id}` + dismiss endpoint

**Files:**
- Modify: `server.py`
- Test: `tests/test_resume_api.py`

- [ ] **Step 1: Failing tests**

```python
def test_resume_endpoint_happy(qwe_temp_data_dir, client, mock_llm):
    import db
    rid = db.insert_agent_run(thread_id="t-r", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    r = client.post(f"/api/resume/{rid}")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert "new_run_id" in j


def test_resume_endpoint_unknown_run_404(client, qwe_temp_data_dir):
    r = client.post("/api/resume/999999")
    assert r.status_code == 404


def test_resume_endpoint_dismissed_run_400(qwe_temp_data_dir, client):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    r = client.post(f"/api/resume/{rid}")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_dismiss_endpoint_sets_dismissed_at(qwe_temp_data_dir, client):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    r = client.post(f"/api/resume/{rid}/dismiss")
    assert r.status_code == 200 and r.json()["ok"] is True
    row = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] is not None


def test_dismiss_endpoint_idempotent(qwe_temp_data_dir, client):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    r1 = client.post(f"/api/resume/{rid}/dismiss")
    r2 = client.post(f"/api/resume/{rid}/dismiss")
    assert r1.status_code == 200 and r2.status_code == 200
```

- [ ] **Step 2: Add endpoints**

In `server.py`:

```python
@app.post("/api/resume/{run_id}")
async def resume_run(run_id: int, background_tasks: BackgroundTasks):
    import db
    from fastapi.responses import JSONResponse
    
    row = db._get_conn().execute(
        "SELECT status, dismissed_at, resumed_from_run_id FROM agent_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    status, dismissed_at, already_resume = row
    if status != "aborted":
        return JSONResponse({"ok": False, "error": f"not aborted (status={status})"},
                             status_code=400)
    if dismissed_at is not None:
        return JSONResponse({"ok": False, "error": "was dismissed"}, status_code=400)
    if already_resume is not None:
        return JSONResponse({"ok": False, "error": "is itself a resume run"},
                             status_code=400)
    # Forward-resume check
    fwd = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE resumed_from_run_id=?", (run_id,)
    ).fetchone()
    if fwd:
        return JSONResponse(
            {"ok": False, "error": f"already resumed by run #{fwd[0]}"},
            status_code=400,
        )
    
    import agent
    # Run in background so HTTP returns immediately; streaming flows via WS.
    background_tasks.add_task(agent.resume_interrupted_run, run_id)
    
    # Best-effort: tell the caller what new_run_id will be (predicted = max+1
    # is racy with concurrent writes; just return ok and let WS deliver)
    return {"ok": True}


@app.post("/api/resume/{run_id}/dismiss")
async def dismiss_run_endpoint(run_id: int):
    import db
    from fastapi.responses import JSONResponse
    row = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    db.dismiss_run(run_id)
    return {"ok": True}
```

Make sure `BackgroundTasks` is imported (`from fastapi import BackgroundTasks`).

- [ ] **Step 3: Run + commit**

```bash
pytest tests/test_resume_api.py -v -k "resume_endpoint or dismiss"
git add server.py tests/test_resume_api.py
git commit -m "feat(api): POST /api/resume/{id} + /api/resume/{id}/dismiss"
```

---

### Task 11: Telegram `/resume` command

**Files:**
- Modify: `telegram_bot.py`
- Test: optional (telegram_bot tests may not exist; skip if so)

- [ ] **Step 1: Locate command-handler pattern**

```bash
grep -n "@bot.command\|@dp.message\|handle_.*command" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/telegram_bot.py | head -10
```

Match the existing command-handler decorator pattern.

- [ ] **Step 2: Add `/resume` handler**

```python
# Match the existing command-handler pattern in telegram_bot.py
@bot.command("resume")  # or whatever decorator is in use
async def handle_resume_cmd(update):
    import db, config, asyncio
    chat_id = update.message.chat.id
    thread_id = _telegram_chat_to_thread_id(chat_id)  # use the existing helper
    ttl = float(config.get("resume_ttl_telegram_sec") or 86400)
    row = db.get_resumable_run_for_thread(thread_id, source_filter="telegram", ttl_sec=ttl)
    if not row:
        await update.message.reply_text("No interrupted task to resume.")
        return
    await update.message.reply_text("▶ Resuming previous task...")
    import agent
    asyncio.create_task(asyncio.to_thread(agent.resume_interrupted_run, row["id"]))
```

(`asyncio.to_thread` is used because `agent.resume_interrupted_run` is synchronous internally — wraps `agent.run` which is sync. Adjust if telegram_bot uses a different pattern.)

- [ ] **Step 3: Commit**

```bash
git add telegram_bot.py
git commit -m "feat(telegram): /resume command for interrupted-task resume"
```

---

### Task 12: Scheduler routine auto-resume (within 5-min window)

**Files:**
- Modify: `scheduler.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Failing test**

```python
def test_scheduler_auto_resumes_routine_within_window(qwe_temp_data_dir, mock_llm):
    import db, scheduler
    rid = db.insert_agent_run(thread_id="t-routine", cron_id=42, source="routine",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    scheduler.detect_missed_runs()
    fwd = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE resumed_from_run_id=?", (rid,)
    ).fetchone()
    assert fwd is not None  # auto-resume fired


def test_scheduler_skips_old_routine_runs(qwe_temp_data_dir, mock_llm):
    import db, scheduler
    long_ago = time.time() - 1000  # > 5 min
    rid = db.insert_agent_run(thread_id="t-routine", cron_id=42, source="routine",
                               started_at=long_ago, status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    scheduler.detect_missed_runs()
    fwd = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE resumed_from_run_id=?", (rid,)
    ).fetchone()
    assert fwd is None  # outside window, not resumed


def test_scheduler_respects_routine_auto_off(qwe_temp_data_dir, mock_llm):
    import db, scheduler, config
    config_orig = config.get("resume_routine_auto")
    try:
        # Toggle via KV directly (existing pattern)
        db.kv_set("setting:resume_routine_auto", "0")
        rid = db.insert_agent_run(thread_id="t-routine", cron_id=42, source="routine",
                                   started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
        scheduler.detect_missed_runs()
        fwd = db._get_conn().execute(
            "SELECT id FROM agent_runs WHERE resumed_from_run_id=?", (rid,)
        ).fetchone()
        assert fwd is None
    finally:
        db.kv_set("setting:resume_routine_auto", "1" if config_orig else "0")
```

- [ ] **Step 2: Find `detect_missed_runs`**

```bash
grep -n "def detect_missed_runs" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/scheduler.py
```

- [ ] **Step 3: Extend the function**

After the existing missed-slot logic, add:

```python
def detect_missed_runs():
    # ... existing missed-slot logic unchanged ...

    # NEW: short-window auto-resume for routine runs
    import config, time as _t
    if not config.get("resume_routine_auto"):
        return
    ttl = float(config.get("resume_ttl_routine_sec") or 300)
    cutoff = _t.time() - ttl
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT id, cron_id FROM agent_runs "
        "WHERE status='aborted' AND cron_id IS NOT NULL "
        "  AND started_at >= ? AND dismissed_at IS NULL "
        "  AND resumed_from_run_id IS NULL "
        "  AND id NOT IN (SELECT resumed_from_run_id FROM agent_runs "
        "                 WHERE resumed_from_run_id IS NOT NULL)",
        (cutoff,),
    ).fetchall()
    for (rid, cron_id) in rows:
        _log.info(f"auto-resuming routine run #{rid} (cron {cron_id})")
        try:
            import agent
            agent.resume_interrupted_run(rid)
        except Exception as e:
            _log.warning(f"routine auto-resume failed for #{rid}: {e}")
```

- [ ] **Step 4: Run + commit**

```bash
pytest tests/test_resume.py -v -k "scheduler"
git add scheduler.py tests/test_resume.py
git commit -m "feat(scheduler): auto-resume aborted routine runs within window"
```

---

## Phase 5 — UI

### Task 13: Interrupted-turn banner

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Find WS message handler**

```bash
grep -n "handleWsMessage\|handleWSMessage\|onmessage\|state.streaming" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/static/index.html | head -10
```

- [ ] **Step 2: Add HTML for banner**

Near other modals / top-bar UI in the DOM template, add:

```html
<div id="interruptBanner" class="interrupt-banner hidden">
  <div class="ib-icon">⚠</div>
  <div class="ib-body">
    <div class="ib-title">Previous turn was interrupted <span class="ib-time"></span></div>
    <div class="ib-preview" id="ib-preview"></div>
  </div>
  <div class="ib-actions">
    <button id="ib-resume" class="btn-primary">▶ Resume</button>
    <button id="ib-dismiss" class="btn-secondary">× Dismiss</button>
  </div>
</div>
```

- [ ] **Step 3: Add JS handler**

In `handleWsMessage` (or equivalent), at the TOP — **before** the `state.streaming` creation gate (CLAUDE.md v0.18.3 lesson):

```js
if (data.event === 'interrupted_turn') {
  showInterruptBanner(data);
  return;
}

function showInterruptBanner({run_id, started_at, preview, model}) {
  const banner = document.getElementById('interruptBanner');
  document.getElementById('ib-preview').textContent =
    (preview || '').slice(0, 200) || '(no output captured)';
  const ago = formatTimeAgo((Date.now()/1000) - started_at);
  banner.querySelector('.ib-time').textContent = ago;
  banner.classList.remove('hidden');
  state.interruptedRunId = run_id;

  document.getElementById('ib-resume').onclick = async () => {
    banner.classList.add('hidden');
    const r = await fetch(`/api/resume/${run_id}`, {method: 'POST'});
    if (!r.ok) {
      showToast('Resume failed');
      banner.classList.remove('hidden');
    }
  };
  document.getElementById('ib-dismiss').onclick = async () => {
    banner.classList.add('hidden');
    await fetch(`/api/resume/${run_id}/dismiss`, {method: 'POST'});
    state.interruptedRunId = null;
  };
}

function formatTimeAgo(secs) {
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs/3600)}h ago`;
  return `${Math.floor(secs/86400)}d ago`;
}
```

If `formatTimeAgo` already exists, reuse it; check via `grep`.

- [ ] **Step 4: Add CSS**

In the inline `<style>` block:

```css
.interrupt-banner {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; margin-bottom: 8px;
  background: var(--warning-bg, #fff3cd);
  border-left: 3px solid var(--warning, #f39c12);
  border-radius: 6px;
}
.interrupt-banner.hidden { display: none; }
.ib-body { flex: 1; }
.ib-title { font-weight: 600; }
.ib-time { font-weight: 400; opacity: 0.7; font-size: 0.9em; }
.ib-preview {
  font-size: 0.9em; opacity: 0.8; margin-top: 4px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%;
}
.ib-actions { display: flex; gap: 6px; }
```

- [ ] **Step 5: Lint + commit**

```bash
python scripts/check_js.py
git add static/index.html
git commit -m "feat(ui): interrupted-turn banner with Resume/Dismiss actions"
```

---

### Task 14: Inline interrupted marker on assistant messages

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Find assistant message render**

```bash
grep -n "msg-assistant\|renderAssistantMessage\|role.*assistant" /Users/kirleshkevich/Documents/GitHub/qwe-qwe/static/index.html | head -10
```

- [ ] **Step 2: Branch on `msg.meta.interrupted`**

In the assistant message render template, after the content, conditionally append:

```js
${msg.meta && msg.meta.interrupted ? `
  <div class="interrupted-marker">
    <span class="im-icon">⏸</span>
    <span class="im-text">interrupted</span>
    ${msg.meta.run_id ? `
      <a class="im-link" onclick="openSessionRunsModal('${msg.thread_id}', null, ${msg.meta.run_id})">
        run #${msg.meta.run_id}
      </a>` : ''}
  </div>
` : ''}
```

Also add `msg-interrupted` class to the outer `.msg.msg-assistant` div when `meta.interrupted` is true:

```js
<div class="msg msg-assistant ${msg.meta?.interrupted ? 'msg-interrupted' : ''}">
```

- [ ] **Step 3: Add CSS**

```css
.msg-interrupted { opacity: 0.85; }
.interrupted-marker {
  display: inline-flex; align-items: center; gap: 6px;
  margin-top: 6px; padding: 2px 8px;
  background: var(--bg-subtle, rgba(255,255,255,0.05));
  border-radius: 4px; font-size: 0.85em;
  color: var(--text-muted, #888);
}
.im-link { color: var(--accent); cursor: pointer; text-decoration: underline; }
```

- [ ] **Step 4: Verify `openSessionRunsModal` accepts a `scrollToRunId`**

Look up the existing function (added in spec #1 Task 21). If it doesn't accept a third arg, extend it to do nothing-or-anchor based on the optional `scrollToRunId`.

- [ ] **Step 5: Lint + commit**

```bash
python scripts/check_js.py
git add static/index.html
git commit -m "feat(ui): inline ⏸ marker on interrupted assistant messages"
```

---

### Task 15: Sessions list interrupt indicator chip

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add a query to per-thread metadata**

In the existing Sessions/Threads list render (from spec #1 Task 20), augment the data fetch (or use existing `cost_usd / run_count` query). The simplest path: extend `GET /api/threads` to include `interrupted_count` (cheap — same indexed scan). OR fetch per-thread via a follow-up call.

Easiest MVP: client-side derivation from `GET /api/threads/{id}/runs` is overkill. Add to the existing server-side `/api/threads` handler one extra subquery:

```sql
SELECT COUNT(*) FROM agent_runs
WHERE thread_id = ? AND status = 'aborted' AND dismissed_at IS NULL
  AND resumed_from_run_id IS NULL
  AND id NOT IN (SELECT resumed_from_run_id FROM agent_runs WHERE resumed_from_run_id IS NOT NULL)
```

Add `interrupted_count` to each thread dict.

- [ ] **Step 2: Render the chip in the sidebar**

In the existing thread-list row template, after the tokens/cost chips, add:

```js
${thread.interrupted_count > 0 ? `
  <span class="chip-interrupted" onclick="openSessionRunsModal('${thread.id}', null, null)">
    ⏸ ${thread.interrupted_count}
  </span>
` : ''}
```

- [ ] **Step 3: CSS**

```css
.chip-interrupted {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 6px; margin-left: 4px;
  background: var(--warning-bg, #fff3cd);
  border-radius: 8px; font-size: 0.8em;
  color: var(--warning, #b8860b);
  cursor: pointer;
}
```

- [ ] **Step 4: Lint + commit**

```bash
python scripts/check_js.py
git add server.py static/index.html
git commit -m "feat(ui): sessions list shows ⏸ chip for interrupted threads"
```

---

### Task 16: Settings → Cost → Auto-resume sub-section

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Find the Settings "Cost" tab**

This was added in spec #1 Task 23. Find `renderTabCost` or equivalent.

- [ ] **Step 2: Add Auto-resume card**

Append to the Cost tab render:

```js
const autoResumeCard = `
<div class="settings-card">
  <h3>Auto-resume</h3>
  <label>
    Resume window — Web (days):
    <input type="number" id="setting-resume_ttl_web_sec_days" min="1" max="365">
  </label>
  <label>
    Resume window — Telegram (hours):
    <input type="number" id="setting-resume_ttl_telegram_sec_hours" min="1" max="720">
  </label>
  <label>
    Resume window — Routines (minutes):
    <input type="number" id="setting-resume_ttl_routine_sec_min" min="0" max="1440">
  </label>
  <label>
    <input type="checkbox" id="setting-resume_routine_auto">
    Auto-resume routines on server start
  </label>
  <button id="save-auto-resume-settings">Save</button>
</div>
`;
```

In `wireTabCost`, load current values (convert sec → days/hours/min) and wire the Save button to write back (POST `/api/settings/{key}` or whatever existing pattern is used). Make sure to convert UNITS on save: days × 86400, hours × 3600, minutes × 60.

- [ ] **Step 3: Lint + commit**

```bash
python scripts/check_js.py
git add static/index.html
git commit -m "feat(ui): Settings → Cost → Auto-resume sub-section"
```

---

## Phase 6 — Polish

### Task 17: Telemetry — `auto_resume` first-use trigger + consent bump

**Files:**
- Modify: `telemetry.py`
- Modify: `server.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Add value + bump**

In `telemetry.py`:
- Locate `FEATURES = frozenset({...})`. Add `"auto_resume"` inside.
- Increment `_CURRENT_CONSENT_VERSION` by 1 (currently 3 after spec #1 Task 25 → becomes 4).

- [ ] **Step 2: Wire the trigger**

In `server.py`, at the top of `resume_run` handler (`POST /api/resume/{run_id}`), fire once per process:

```python
_auto_resume_first_use_seen = False  # module-level

@app.post("/api/resume/{run_id}")
async def resume_run(run_id: int, background_tasks: BackgroundTasks):
    global _auto_resume_first_use_seen
    if not _auto_resume_first_use_seen:
        _auto_resume_first_use_seen = True
        try:
            import telemetry
            telemetry.track_event("feature_first_use", {"feature": "auto_resume"})
        except Exception:
            pass
    # ... rest of handler ...
```

- [ ] **Step 3: Test**

```python
def test_auto_resume_feature_in_enum():
    import telemetry
    assert "auto_resume" in telemetry.FEATURES

def test_consent_version_bumped_for_auto_resume():
    import telemetry
    assert telemetry._CURRENT_CONSENT_VERSION >= 4
```

- [ ] **Step 4: Run + commit**

```bash
pytest tests/test_telemetry.py -v -k "auto_resume or consent_version_bumped_for_auto"
git add telemetry.py server.py tests/test_telemetry.py
git commit -m "feat(telemetry): auto_resume feature_first_use + consent v3→v4"
```

---

### Task 18: Integration tests — full cycle

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Add full-cycle test**

Append:

```python
def test_full_abort_resume_cycle(qwe_temp_data_dir, mock_llm):
    """Run, abort mid-stream, resume; assert linked rows and message history."""
    import agent, db, threading, time as _t
    from turn_context import TurnContext
    
    ctx = TurnContext(source="web")
    def trigger_abort():
        _t.sleep(0.05)
        ctx.abort_event.set()
    threading.Thread(target=trigger_abort, daemon=True).start()
    
    try:
        agent.run("hello world", thread_id="t-full", source="web", ctx=ctx)
    except Exception:
        pass
    
    # Find the aborted run
    aborted = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE thread_id='t-full' AND status='aborted' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert aborted is not None
    original_id = aborted[0]
    
    # Resume
    agent.resume_interrupted_run(original_id)
    
    # Two rows: original + resume
    rows = db._get_conn().execute(
        "SELECT id, resumed_from_run_id, status FROM agent_runs "
        "WHERE thread_id='t-full' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] is None  # original
    assert rows[1][1] == original_id  # resume links back
    
    # Token continuity: resume saw the partial reply
    assert rows[1][2] in ("ok", "err")  # depends on mock
```

- [ ] **Step 2: Run + commit**

```bash
pytest tests/test_integration.py -v -k "full_abort_resume_cycle"
git add tests/test_integration.py
git commit -m "test(integration): full abort + resume cycle linked via resumed_from_run_id"
```

---

### Task 19: Public docs — `AUTO_RESUME.md`

**Files:**
- Create: `docs/AUTO_RESUME.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write `docs/AUTO_RESUME.md`**

User-facing doc (~80 lines). Sections:
- **What's resumable** — Web tab close / network drop / `/api/abort` Stop click / server crash; aborts in last 7 days (Web), 24h (Telegram), 5min (Routines)
- **What's NOT resumable** — CLI Ctrl+C (intentional stop), `err` (raised) runs, missed routine slots (different mechanic)
- **How resume works** — "continue" not "replay"; the agent sees its partial reply + a one-shot system note telling it to keep going
- **UX per source** — Web banner on reconnect, Telegram `/resume`, Routine auto-fire if within window
- **TTL configuration** — Settings → Cost → Auto-resume
- **Privacy** — no data leaves the machine; partial reply content stored locally only
- **Troubleshooting** — "I see weird duplicates" (model re-fired a tool call; deferred to spec #2 territory), "Resume button doesn't appear" (TTL expired or source=cli)

- [ ] **Step 2: Update `CLAUDE.md`**

Add a new sub-section under the existing "Cost tracking" section in Architecture. ~30 lines covering:
- Migration 009 (resumed_from_run_id + dismissed_at)
- Crash-recovery hook in server startup
- agent.resume_interrupted_run helper
- per-source TTL + scheduler routine auto-resume
- pointer to `docs/AUTO_RESUME.md`

- [ ] **Step 3: Commit**

```bash
git add docs/AUTO_RESUME.md CLAUDE.md
git commit -m "docs: AUTO_RESUME.md + CLAUDE Architecture sub-section"
```

---

### Task 20: Release notes + version bump

**Files:**
- Modify: `RELEASE_NOTES.md`
- Modify: `config.py` (VERSION)
- Modify: `pyproject.toml` (version)
- Modify: `README.md` (badge if present)

- [ ] **Step 1: Add release notes**

Prepend to `RELEASE_NOTES.md`:

```markdown
## v0.20.0 — Auto-resume after interrupt

- Every abort (WS disconnect, Stop button, server crash) is now recoverable.
- Web UI shows a banner on reconnect: "Previous turn was interrupted — Resume / Dismiss". The agent picks up from where it left off, not from scratch.
- Telegram exposes `/resume` for the same flow in chat.
- Routines auto-resume if the abort was within 5 minutes (configurable).
- CLI Ctrl+C remains an intentional stop — no resume.
- New per-source TTL settings in Settings → Cost → Auto-resume: Web (7 days), Telegram (24h), Routines (5 min).
- Migration 009 adds `resumed_from_run_id` + `dismissed_at` to `agent_runs`.
- Analytics chain resume runs back to their originals (`run #142 (resumed #138)` in drilldown).
```

- [ ] **Step 2: Bump version**

- `config.py`: `VERSION = "0.20.0"`
- `pyproject.toml`: `version = "0.20.0"`
- `README.md`: update badge if present

- [ ] **Step 3: Commit**

```bash
git add RELEASE_NOTES.md config.py pyproject.toml README.md
git commit -m "docs: release notes + version bump v0.20.0"
```

---

### Task 21: Final verification

- [ ] **Step 1: Run all gates**

```bash
ruff check .
python scripts/check_js.py
python -c "import ast, pathlib
for p in pathlib.Path('.').glob('*.py'):
    ast.parse(p.read_text(encoding='utf-8'), filename=str(p), feature_version=(3,11))"
pytest tests/ -q
```

Expected:
- ruff clean
- check_js clean
- AST parse clean
- pytest: ALL new tests pass; pre-existing failures (presets table missing, pyserial, telegram integration env) remain — DO NOT count against this PR.

- [ ] **Step 2: Smoke-test server boot**

```bash
python -c "import server; print('imports clean')"
```

- [ ] **Step 3: If something needed fixing, commit it; otherwise the plan is done.**

---

## Risks & mitigations recap

| Risk | Mitigation (in plan) |
|---|---|
| Abort flush leaves empty content | `if _is_aborted and final_content:` guard skips empty rows |
| Resume re-runs a tool with side effects | Spec acknowledges: model sees prior tool calls in history; one-shot system_note says "do not repeat tool calls that already ran"; perfect resume guarantee deferred |
| `[system]` prefix anti-pattern | Use `system_note=` parameter on agent.run → real `role: system` message |
| Race on concurrent `POST /api/resume/{id}` | Forward-resume check (`SELECT id FROM agent_runs WHERE resumed_from_run_id=?`) makes the second call 400 |
| Routine auto-resume infinite loop on crash | The new resume run starts as `running`; if it also crashes, the next startup's recovery hook promotes it to `aborted`, but `resumed_from_run_id IS NOT NULL` filters it out of subsequent auto-resume sweeps. One re-fire max per cycle. |
| Migration 009 breaks existing data | Pure `ALTER TABLE ADD COLUMN`; nullable; pre-existing rows behave as "not resumable" |

---

## Definition of done (mirrors spec §15)

- [ ] Migration 009 applies atomically; tests cover fresh + existing DBs
- [ ] `agent_loop` `finally:` flushes partial content with `meta.interrupted=true`
- [ ] `_recover_interrupted_runs_on_startup` runs before scheduler.start in server boot
- [ ] `agent.resume_interrupted_run` covers all four sources
- [ ] Scheduler auto-fires aborted routine runs within `resume_ttl_routine_sec`
- [ ] WS `interrupted_turn` event emitted on connect; banner wires Resume/Dismiss
- [ ] `POST /api/resume/{id}` + `/dismiss` work + tested
- [ ] Telegram `/resume` command works for telegram-scoped threads
- [ ] Inline `⏸ interrupted` marker renders
- [ ] Sessions list shows `⏸ N` chip for threads with un-dismissed interrupted runs
- [ ] Settings → Cost → Auto-resume sub-section saves 4 settings
- [ ] `auto_resume` added to telemetry `FEATURES`; consent v3→v4
- [ ] `docs/AUTO_RESUME.md` + CLAUDE.md sub-section + RELEASE_NOTES.md v0.20.0 entry
- [ ] `ruff check .` clean; `python scripts/check_js.py` clean; new tests green; no new pre-existing regressions
- [ ] Version bumped to v0.20.0 in config.py, pyproject.toml, README (if applicable)
