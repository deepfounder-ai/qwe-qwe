# Auto-resume after interrupt — design

**Date**: 2026-05-12
**Status**: Draft (pending review)
**Author**: deepfounder-ai + Claude
**Related issues** (competitor pain points): NousResearch/hermes-agent#23522

---

## 1. Problem statement

qwe-qwe today handles aborts cleanly — `ctx.abort_event.set()` fires from the WS disconnect handler (server.py:3525), the `/api/abort` Stop button, or a server shutdown, and `agent_loop` checks the event per chunk so the loop exits without thrashing. After spec #1 (cost tracking) the aborted run gets recorded in `agent_runs` with `status='aborted'`.

But the user-visible behaviour ends there. The partial assistant reply is lost (it lived only in the streaming buffer), the conversation history shows the user's question with no answer, and there is no mechanism to pick up where the agent left off. For long agentic tasks ("research X, write a doc, deploy it") this means an interrupted laptop battery, a network blip, or a server restart silently throws away minutes of work — sometimes including expensive tool calls (Brave Search, browser sessions, LLM-driven extraction) whose output the user can no longer see.

User pain (competitor issue #23522, quoting):

> "feature request: auto-resume prior task after handling an interrupt — agent yields cleanly but doesn't consistently pick up where it left off"

This spec is **#3 of 3** in the competitor-pain-point series. Spec #1 (cost tracking — PR #26, the `agent_runs` table) provides the foundation; spec #2 (budget cap on routines) is independent. Spec #3 builds on spec #1's `agent_runs` schema to add resume semantics across all sources where it makes sense.

---

## 2. Goals

1. **Persist enough state at abort time** that a resumed run can pick up from where the previous turn left off without re-doing tool side effects.
2. **Detect interrupted turns** on the relevant return path per source: Web (WS reconnect), Telegram (`/resume` command), Routine (server startup within 5 min), CLI (no resume — Ctrl+C means user said stop).
3. **Recover from server crashes** — any `agent_runs` row in `running` state at startup is marked `aborted` so it becomes a resume candidate.
4. **Continue, don't replay** — resume executes a new agent.run that sees the partial assistant content in conversation history and is instructed to continue, not restart from scratch.
5. **Surface resume in the UI** without being intrusive: a banner on WS connect, a small `⏸ interrupted` marker inline on the affected assistant message, and Settings knobs for TTLs.
6. **Link resume runs to their originals** via `resumed_from_run_id` so analytics can chain the timeline.

**Non-goals (deferred):**

- Auto-resume on next user message (decided against — too surprising; banner asks first).
- CLI resume (Ctrl+C is intentional stop).
- Replay semantics (would re-execute tools with side effects).
- Cross-thread resume (each thread's interrupts are independent).
- Manual "undo dismiss" — once dismissed, you can't un-dismiss. (Trivial follow-up if requested.)

---

## 3. Architecture overview

Changes touch five modules; no new modules. The crash-recovery hook + the resume helper live in existing files.

```
┌──────────────────────────────────────────────────────────────────┐
│  ABORT PATH (extend existing finally: from spec #1 Task 11)      │
│                                                                  │
│  WS disconnect / Stop button / server SIGTERM                    │
│         │                                                        │
│         ▼                                                        │
│  ctx.abort_event.set()                                           │
│         │                                                        │
│         ▼                                                        │
│  agent_loop.run_loop finally block                               │
│    1. (existing) finalize_agent_run(status='aborted')            │
│    2. NEW: if final_content non-empty, flush as message row      │
│       with meta.interrupted=true + run_id                        │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  SERVER CRASH RECOVERY (server.py startup, new hook)             │
│                                                                  │
│  _recover_interrupted_runs_on_startup()                          │
│    SELECT id, thread_id FROM agent_runs WHERE status='running'   │
│    For each: synthesize abort marker + finalize as aborted       │
│  Logged as "recovered N interrupted runs"                        │
│                                                                  │
│  scheduler.detect_missed_runs (extended)                         │
│    NEW: auto-fire aborted routine runs within                    │
│         resume_ttl_routine_sec (default 5 min)                   │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  RESUME TRIGGER (per-source)                                     │
│                                                                  │
│  Web: WS connect handler runs _check_for_resumable_interrupt     │
│       → if hit, emits {event:'interrupted_turn', ...} to client  │
│       UI shows banner with [Resume] / [Dismiss]                  │
│                                                                  │
│  Telegram: /resume command — look up last interrupted run        │
│            in chat-scoped thread, fire resume                    │
│                                                                  │
│  Routine: scheduler.detect_missed_runs auto-fires (see above)    │
│                                                                  │
│  CLI: no resume path (Ctrl+C = explicit stop)                    │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  RESUME EXECUTION (universal across sources)                     │
│                                                                  │
│  agent.resume_interrupted_run(run_id, ctx=None)                  │
│    1. Look up original run; validate not dismissed, not already  │
│       resumed                                                    │
│    2. Build/reuse TurnContext with source + cron_id from original│
│    3. Set ctx.resumed_from_run_id = original                     │
│    4. Call agent.run with a small "[system] continue" prompt;    │
│       conversation history already contains the partial reply    │
└──────────────────────────────────────────────────────────────────┘
```

**Files touched:**

- `agent_loop.py` — extend abort `finally:` to flush partial content
- `agent.py` — new `resume_interrupted_run` function
- `server.py` — `_recover_interrupted_runs_on_startup`, `_check_for_resumable_interrupt`, `POST /api/resume/{run_id}`, `POST /api/resume/{run_id}/dismiss`
- `scheduler.py` — extend `detect_missed_runs` with auto-resume window
- `telegram_bot.py` — `/resume` command handler
- `static/index.html` — banner UI, inline interrupted marker, Settings sub-section
- `turn_context.py` — add `resumed_from_run_id` field
- `db.py` — `get_interrupted_for_thread`, helpers for dismiss + auto-resume queries
- `config.py` — add 3 TTL settings + 1 toggle
- `migrations/009_interrupted_runs.sql` — add `resumed_from_run_id` + `dismissed_at` columns
- `telemetry.py` — add `auto_resume` to `FEATURES`, bump consent version
- New tests in `tests/test_resume.py`, `tests/test_resume_api.py`, extensions to `test_integration.py`

---

## 4. Data schema

### 4.1 Migration `009_interrupted_runs.sql` (new)

```sql
-- v0.20.0: support auto-resume for interrupted turns.
--
-- Two narrow extensions on agent_runs:
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

Atomic transaction. Both columns nullable. Pre-existing rows behave as "not resumable" by default — no breaking change.

### 4.2 `messages.meta` JSON contract

The `messages.meta` column is an existing JSON blob used by other features (image_path, file_meta). Cost tracking spec already touches it. Add one convention:

```jsonc
{
  // Set when agent_loop's abort finally: flushes a partial assistant row:
  "interrupted": true,
  "run_id": 42,             // FK to agent_runs.id
  "partial_tokens": {       // optional, copied from agent_runs at flush time
    "input": 320, "output": 184
  },
  "crash_recovery": true    // optional, set ONLY when synthesized at
                            // server startup (no streaming content was
                            // ever flushed by agent_loop itself)
}
```

When the UI sees `meta.interrupted === true`, it renders the inline `⏸ interrupted` marker next to the message and dims the message slightly.

### 4.3 `agent_runs.status` value reference

| status | semantics | resumable? |
|---|---|---|
| `running` | row inserted at run start, not yet finalized | orphan if seen on server start; promoted to `aborted` |
| `ok` | finished cleanly | no |
| `err` | exception raised | no |
| `aborted` | abort_event fired mid-run | **yes** — primary resume target |
| `missed` | routine slot lapsed offline | no (different mechanic) |
| `skipped` | per-thread fire lock held | no |
| `dismissed_at IS NOT NULL` | user dismissed / TTL expired | filtered out of "resume?" lookups |

### 4.4 TTL settings (`config.py::EDITABLE_SETTINGS`)

| key | type | default | unit | meaning |
|---|---|---|---|---|
| `resume_ttl_web_sec` | int | 604800 | 7 days | how long a Web abort stays resumable |
| `resume_ttl_telegram_sec` | int | 86400 | 24 hours | how long a Telegram abort stays resumable |
| `resume_ttl_routine_sec` | int | 300 | 5 minutes | window for auto-firing aborted routines on startup |
| `resume_routine_auto` | bool | True | — | enable/disable routine auto-resume entirely |

End-user UI surfaces these as form inputs in Settings → Cost (extends the existing tab from spec #1) — not env vars.

---

## 5. Abort persistence path

### 5.1 Extension of agent_loop.run_loop finally

After spec #1 Task 11, run_loop already finalizes the agent_runs row in a `finally:` block. We extend the same block — no new flush path, no new abort callback.

Inside the `finally:`, immediately before the existing `db.finalize_agent_run(...)` call:

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

# (existing finalize_agent_run call below — unchanged)
```

### 5.2 Tool call durability

Tool call messages (the `tool_calls` assistant turn and each tool result) are already persisted via `db.save_message` inside the loop body, immediately after each tool completes — not at finalize time. So an abort after `browser_open` returns will leave that tool's result in `messages` permanently. Resume reads the full history; the model sees the completed tool calls and skips them. **No new code needed for tool durability.**

### 5.3 Empty `final_content` case

If abort fires before any token streams, `final_content == ""`. Skip the `save_message` call (`if _is_aborted and final_content:`) — no empty assistant row. The `agent_runs` row alone marks the abort, and the Web banner's preview shows `"(no output captured)"`.

### 5.4 Server crash recovery hook

New function in `server.py`:

```python
def _recover_interrupted_runs_on_startup() -> None:
    """Mark orphaned 'running' agent_runs as 'aborted' at server start.

    Synthesizes an abort marker in messages so the run is visible as
    interrupted in the UI. Does NOT auto-resume — the detect-and-ask
    flow (Section 6) handles user-facing recovery. Exception: routine
    runs within resume_ttl_routine_sec are auto-fired by
    scheduler.detect_missed_runs (Section 6.3).
    """
    import db
    rows = db._get_conn().execute(
        "SELECT id, thread_id FROM agent_runs WHERE status='running'"
    ).fetchall()
    for (rid, thread_id) in rows:
        # Synthesize a sentinel so the run appears interrupted in history.
        # content="" signals "no streaming buffer was ever flushed".
        try:
            db.save_message(
                role="assistant", content="",
                thread_id=thread_id,
                meta={"interrupted": True, "run_id": rid,
                      "crash_recovery": True},
            )
        except Exception as e:
            _log.debug(f"crash-recovery save_message failed for #{rid}: {e}")
        db.finalize_agent_run(
            rid, finished_at=None, duration_ms=None,
            status="aborted", error="server restart",
        )
    if rows:
        _log.info(
            f"recovered {len(rows)} interrupted runs from previous session"
        )
```

Wired into the FastAPI startup handler **before** `pricing.start_background_refresher()` (already there from spec #1). Order matters: scheduler's `detect_missed_runs` reads `aborted` rows for the auto-fire window, so recovery must run first.

---

## 6. Resume trigger detection

### 6.1 Web — `interrupted_turn` WS event + banner

After WS auth + thread_id setup, before the main message loop:

```python
async def _check_for_resumable_interrupt(ws, thread_id: str) -> None:
    """Probe for one resumable aborted run in this thread; emit event."""
    import db, config, time
    ttl = float(config.get("resume_ttl_web_sec") or 604800)
    cutoff = time.time() - ttl
    # Two related-but-different guards:
    #   resumed_from_run_id IS NULL  → this row is not itself a resume run
    #                                  (we want the ORIGINAL aborted run)
    #   id NOT IN (...)              → no later row has already resumed
    #                                  from this one (no double-fire)
    # Both clauses are intentional; do not collapse them.
    row = db._get_conn().execute(
        "SELECT id, started_at, result_preview, model, source FROM agent_runs "
        "WHERE thread_id=? AND status='aborted' AND dismissed_at IS NULL "
        "  AND started_at >= ? "
        "  AND resumed_from_run_id IS NULL "
        "  AND source != 'cli' "
        "  AND id NOT IN (SELECT resumed_from_run_id FROM agent_runs "
        "                 WHERE resumed_from_run_id IS NOT NULL) "
        "ORDER BY id DESC LIMIT 1",
        (thread_id, cutoff),
    ).fetchone()
    if not row:
        return
    rid, started_at, preview, model, source = row
    await ws.send_json({
        "event": "interrupted_turn",
        "run_id": rid,
        "started_at": started_at,
        "preview": preview or "",
        "model": model,
        "source": source,
        "thread_id": thread_id,
    })
```

Client handles the event **before** the `state.streaming` creation gate (CLAUDE.md v0.18.3 lesson: non-chat WS events must short-circuit at the top of `handleWsMessage`):

```js
if (data.event === 'interrupted_turn') {
  showInterruptBanner(data);
  return;  // short-circuit
}
```

`showInterruptBanner` renders the banner over the input composer. The two buttons hit:

- **POST /api/resume/{run_id}** — server kicks off `agent.resume_interrupted_run(run_id)` in a background task; output streams through the normal WS protocol. Returns 200 immediately on accepted, or 400 if the run is dismissed / already resumed / not aborted.
- **POST /api/resume/{run_id}/dismiss** — sets `dismissed_at = time.time()`. Banner hides. Returns 200.

### 6.2 Telegram — `/resume` command

In `telegram_bot.py`, a new command handler:

```python
@bot.command("resume")
async def handle_resume(update):
    chat_id = update.message.chat.id
    thread_id = _telegram_chat_to_thread_id(chat_id)
    import db, config, time, asyncio
    ttl = float(config.get("resume_ttl_telegram_sec") or 86400)
    cutoff = time.time() - ttl
    row = db._get_conn().execute(
        "SELECT id FROM agent_runs WHERE thread_id=? AND status='aborted' "
        "  AND dismissed_at IS NULL AND started_at >= ? "
        "  AND resumed_from_run_id IS NULL AND source='telegram' "
        "  AND id NOT IN (SELECT resumed_from_run_id FROM agent_runs "
        "                 WHERE resumed_from_run_id IS NOT NULL) "
        "ORDER BY id DESC LIMIT 1",
        (thread_id, cutoff),
    ).fetchone()
    if not row:
        await update.message.reply_text("No interrupted task to resume.")
        return
    await update.message.reply_text("▶ Resuming previous task...")
    asyncio.create_task(agent.resume_interrupted_run(row[0]))
```

Telegram does **not** auto-resume on any incoming message — that's too surprising. The user opts in via `/resume` only. Sending a regular message while there's an interrupted task simply starts a new turn; the old aborted run stays available (until TTL) for explicit `/resume`.

### 6.3 Routine — auto-fire within short window

Extend `scheduler.detect_missed_runs()` (existing function that runs at scheduler startup):

```python
def detect_missed_runs():
    # ... existing missed-slot logic unchanged ...

    # NEW: short-window auto-resume for routine runs
    import config, time
    if not config.get("resume_routine_auto"):
        return
    ttl = float(config.get("resume_ttl_routine_sec") or 300)
    cutoff = time.time() - ttl
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT id, cron_id FROM agent_runs "
        "WHERE status='aborted' AND cron_id IS NOT NULL "
        "  AND started_at >= ? AND dismissed_at IS NULL "
        "  AND resumed_from_run_id IS NULL",
        (cutoff,),
    ).fetchall()
    for (rid, cron_id) in rows:
        _log.info(f"auto-resuming routine run #{rid} (cron {cron_id})")
        try:
            agent.resume_interrupted_run(rid)
        except Exception as e:
            _log.warning(f"routine auto-resume failed for #{rid}: {e}")
```

Older than 5 minutes → skipped; the row stays `aborted` (the cron's next slot has already moved on). The crash-recovery hook in Section 5.4 already promoted these from `running` to `aborted`; this just decides whether to auto-fire.

### 6.4 CLI — explicit no-op

The CLI source is filtered out of Web banner queries (`AND source != 'cli'` in 6.1) and out of Telegram `/resume` (it scopes by `source='telegram'`). Aborted CLI runs are still recorded in `agent_runs` — visible in the Web UI as analytics — but no resume button appears for them. Ctrl+C means "I changed my mind."

---

## 7. Resume execution

One universal helper in `agent.py`:

```python
def resume_interrupted_run(
    run_id: int, ctx: "TurnContext | None" = None
) -> dict:
    """Resume a previously interrupted agent run.

    Loads the full thread message history (which now includes the
    partial assistant message flushed at abort time) and re-enters
    agent.run with a small system note that signals 'continue from
    where you left off'. Tools, recall, and budget all behave as in a
    normal turn; the only difference is that conversation already
    contains the in-progress assistant turn.
    """
    import db, turn_context

    # 1. Load and validate original run
    conn = db._get_conn()
    row = conn.execute(
        "SELECT thread_id, source, cron_id, dismissed_at, "
        "       resumed_from_run_id FROM agent_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"run #{run_id} not found")
    thread_id, source, cron_id, dismissed_at, already_resumed = row
    if dismissed_at is not None:
        raise ValueError(f"run #{run_id} was dismissed")
    if already_resumed is not None:
        raise ValueError(f"run #{run_id} already resumed by run #{already_resumed}")

    # Also: was this run *referenced* by some later run's
    # resumed_from_run_id? (i.e., already resumed forward)
    already_used = conn.execute(
        "SELECT id FROM agent_runs WHERE resumed_from_run_id=?", (run_id,),
    ).fetchone()
    if already_used:
        raise ValueError(f"run #{run_id} already resumed by run #{already_used[0]}")

    # NOTE: `resume_interrupted_run` does NOT block CLI source explicitly.
    # CLI filtering happens at the *trigger* layer (Web banner SQL and
    # Telegram /resume scope by source). Direct executor calls are rare
    # — primarily tests and tooling — and accepting any source there
    # keeps the helper general.

    # 2. Build or reuse context
    if ctx is None:
        ctx = turn_context.TurnContext(
            source=source,
            cron_id=cron_id,
            session_id=f"resume-{run_id}",
        )

    # 3. Link new run to original for analytics
    ctx.resumed_from_run_id = run_id  # NEW field on TurnContext

    # 4. Fire a normal agent.run with a system_note. CLAUDE.md's OpenCode
    #    lesson explicitly warns against injecting "[system]" prefixes as
    #    user-role messages — it breaks model flow. Instead, we add a new
    #    optional `system_note=` parameter to agent.run; agent_loop reads
    #    it and prepends one true {role: "system"} message to the chat
    #    completion request for the next LLM call (only — the note is
    #    one-shot, not persisted).
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

### 7.0a agent.run signature extension

`agent.run` currently takes `user_input: str` as the next conversation turn. Add one optional parameter:

```python
def run(
    user_input: str | None,
    thread_id: str | None = None,
    source: str = "cli",
    ctx: "TurnContext | None" = None,
    system_note: str | None = None,        # NEW — one-shot system message
    # ... existing params unchanged ...
) -> dict:
```

When `system_note` is set, `agent.run` passes it through to `agent_loop.run_loop`. `run_loop` adds one `{role: "system", content: system_note}` message at the head of the chat completion request for the next LLM call only — it's **not** persisted to `messages` (one-shot per resume) and it's **not** carried into subsequent turns (the next user message goes through the normal path).

When `user_input is None and system_note is not None`, the call is a "system-only nudge" — used by resume. The agent emits its next response in that conversational shape, which is consistent with the conversation history (the history already ends with the partial assistant message from the abort).

### 7.0b TurnContext extension

Add one field to `TurnContext` (in `turn_context.py`):

```python
@dataclass
class TurnContext:
    # ... existing fields ...
    # Set when this turn is a resume of an aborted run. Read by
    # agent_loop.run_loop and stored in agent_runs.resumed_from_run_id
    # so the analytics page can chain runs.
    resumed_from_run_id: Optional[int] = None
```

### 7.2 agent_loop wiring

In `agent_loop.run_loop`, where `db.insert_agent_run(...)` is called (added by spec #1 Task 11), pass through:

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

Update `db.insert_agent_run` signature in `db.py` to accept the optional `resumed_from_run_id=None` parameter and INSERT it into the new column.

### 7.3 Linking semantics

The new resume run gets its own `agent_runs` row with `resumed_from_run_id` set to the original. Analytics page (from spec #1) displays a chain:

```
Run #142  (resumed #138)   web   gpt-4o-mini   4.2k/1.1k   $0.005   ok
└── Run #138                web   gpt-4o-mini   1.8k/0.4k   $0.002   ⏸ aborted
```

Drilldown — click `#138` — opens the SessionRunsModal scrolled to that entry. Useful for "I clicked Resume but it produced different output" debugging.

### 7.4 Cascade resume

A resume run can itself be aborted. That's fine: it becomes another `aborted` row with `resumed_from_run_id=138`. A fresh banner will offer to resume **#142** next (the most recent aborted). The chain grows by one row per attempt; we never traverse backwards or coalesce. Resume crash-loop is therefore impossible — each click consumes a new row.

### 7.5 Input-token cost of resume

The partial assistant content lives in conversation history, so resume's first LLM call eats input tokens proportional to "everything that already happened in this thread + the partial reply." Spec #1's instrumentation captures this naturally — the resume run's `input_tokens` is what it is, and shows up in the Sessions list / Routines `Cost (30d)` column as a real cost.

---

## 8. UI changes (static/index.html)

### 8.1 Interrupted-turn banner

Renders above the input composer. Non-blocking — user can ignore it and start typing a new message; the banner just sits there until they click Resume or Dismiss or until the TTL window passes (no auto-hide on TTL boundary; client just refuses to show it on next WS connect after expiry).

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

JS handler (in `handleWsMessage`, **before** the streaming-message creation gate):

```js
if (data.event === 'interrupted_turn') {
  showInterruptBanner({
    runId: data.run_id,
    startedAt: data.started_at,
    preview: data.preview,
    model: data.model,
  });
  return;  // short-circuit
}

function showInterruptBanner({runId, startedAt, preview, model}) {
  const banner = document.getElementById('interruptBanner');
  document.getElementById('ib-preview').textContent =
    (preview || '').slice(0, 200) || '(no output captured)';
  const ago = formatTimeAgo((Date.now() / 1000) - startedAt);
  banner.querySelector('.ib-time').textContent = ago;
  banner.classList.remove('hidden');
  state.interruptedRunId = runId;

  document.getElementById('ib-resume').onclick = async () => {
    banner.classList.add('hidden');
    const r = await fetch(`/api/resume/${runId}`, {method: 'POST'});
    if (!r.ok) {
      showToast('Resume failed');
      banner.classList.remove('hidden');
    }
    // Output streams via WS as a normal turn — UI renders it like any
    // other agent reply.
  };
  document.getElementById('ib-dismiss').onclick = async () => {
    banner.classList.add('hidden');
    await fetch(`/api/resume/${runId}/dismiss`, {method: 'POST'});
    state.interruptedRunId = null;
  };
}
```

### 8.2 Inline interrupted marker

In the existing assistant-message renderer, branch on `msg.meta?.interrupted`:

```js
function renderAssistantMessage(msg) {
  const interrupted = msg.meta && msg.meta.interrupted;
  return `
    <div class="msg msg-assistant ${interrupted ? 'msg-interrupted' : ''}">
      ${renderMarkdown(msg.content || '')}
      ${interrupted ? `
        <div class="interrupted-marker">
          <span class="im-icon">⏸</span>
          <span class="im-text">interrupted</span>
          ${msg.meta.run_id ? `
            <a class="im-link"
               onclick="openSessionRunsModal('${msg.thread_id}', null, ${msg.meta.run_id})">
              run #${msg.meta.run_id}
            </a>` : ''}
        </div>
      ` : ''}
    </div>
  `;
}
```

`openSessionRunsModal` was added in spec #1 Task 21. Extend its signature with an optional `scrollToRunId` parameter so clicking the inline link anchors the drilldown to the relevant run.

### 8.3 CSS additions

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
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 100%;
}
.ib-actions { display: flex; gap: 6px; }

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

### 8.4 Settings → Cost tab → "Auto-resume" sub-section

Extension of the existing Cost tab from spec #1. New card titled "Auto-resume":

```
Auto-resume
─────────────
  Resume window — Web:       [  7 ] days
  Resume window — Telegram:  [ 24 ] hours
  Resume window — Routines:  [  5 ] minutes

  Auto-resume routines on server start:  [✓]   (default on)
```

Inputs bound to KV via the existing settings save API. Validation: positive integers only; unreasonably large values (years) clamped to 365 days.

### 8.5 Sessions list — interrupt indicator chip

In the sidebar thread list, if a thread has at least one un-dismissed aborted run within its source's TTL, render a small `⏸ 1` (or `⏸ N`) chip next to the existing tokens/cost chips. Click → `openSessionRunsModal` filtered to interrupted runs only. Cheap query (we already have `idx_agent_runs_thread_id`).

---

## 9. API endpoints (server.py)

### 9.1 `POST /api/resume/{run_id}` (new)

```json
// 200 — accepted
{"ok": true, "new_run_id": 142}

// 400 — original was dismissed / already resumed / not aborted
{"ok": false, "error": "run #138 was dismissed"}

// 404 — no such run
{"ok": false, "error": "run #138 not found"}
```

Body of handler runs `agent.resume_interrupted_run(run_id)` in a background task (FastAPI `BackgroundTasks` or `asyncio.create_task`). The streaming output reaches the active WS session through the normal event protocol.

### 9.2 `POST /api/resume/{run_id}/dismiss` (new)

```json
{"ok": true}
```

Body: `UPDATE agent_runs SET dismissed_at = ? WHERE id = ?`. Idempotent — dismissing an already-dismissed row is a no-op (returns 200).

### 9.3 WS `interrupted_turn` event (new)

Emitted by server during WS connect handler **after** auth + thread_id setup, **before** the main message loop. Payload documented in §6.1.

Not a request/response — just a server-initiated notification. Client must short-circuit at the top of `handleWsMessage` per the v0.18.3 lesson.

---

## 10. Privacy + security

- **No new outbound network traffic.** All resume detection / execution is local.
- **No new payloads to telemetry.** Spec #1's privacy contract holds; we only add one event-feature value (`auto_resume`) inside the existing `feature_first_use` event shape.
- **Authorization:** `POST /api/resume/{run_id}` accepts any authenticated session that owns the thread. We reuse the existing thread-ownership check (the same one that protects `GET /api/threads/{id}/runs`).
- **Cross-thread leakage:** the SQL in §6.1 always filters by `thread_id=?`. Resume cannot be triggered for a run that belongs to a different thread.
- **`result_preview` was already 200 chars (spec #1 §11)** to prevent inadvertent secret capture; the WS `interrupted_turn` event reuses that field, so no new leak surface.

---

## 11. Telemetry (opt-in only)

Add one value to the existing `FEATURES` frozenset in `telemetry.py`:

```python
FEATURES = frozenset({
    # ... existing values from spec #1 + earlier ...
    "auto_resume",
})
```

Fired once per anonymous_id, the first time the user clicks `[Resume]` in the banner (Web) or sends `/resume` (Telegram). Helps measure adoption without leaking any content.

Bump `_CURRENT_CONSENT_VERSION` 3 → 4 so opted-in users get a re-consent banner reflecting the addition.

Audit: no new event types, no new payload fields. Privacy contract unchanged.

---

## 12. Testing

### 12.1 `tests/test_resume.py` (new, ~25 cases)

- Crash recovery: at startup, `running` rows → marked `aborted`, synthetic message row written with `meta.crash_recovery=true`
- `resume_interrupted_run` happy path: passes mocked LLM, new run row written with `resumed_from_run_id` set
- Dismissed run → `ValueError` ("was dismissed")
- Already-resumed run → `ValueError` ("already resumed by run #N")
- Backwards check: cannot resume a run that some later run already cites as `resumed_from_run_id`
- TTL filtering — web 7d, telegram 24h, routine 5min — runs older than TTL not visible
- Empty `final_content` abort → no `messages` row written, but `agent_runs` still finalized
- Partial content abort → `messages` row with `meta.interrupted=true` + correct `run_id`
- Tool call durability — tool result rows from a now-aborted turn are intact (regression test against future refactors)
- `cron_id` of original carries to resume's `TurnContext`
- Routine auto-resume in scheduler: aborted routine run within 5 min → fired; older → skipped
- `resume_routine_auto = False` → auto-fire suppressed even within window
- Resume of CLI run not allowed (filtered out of Web detection)
- Migration 009 applies cleanly on fresh DB and on a DB with existing `agent_runs` rows

### 12.2 `tests/test_resume_api.py` (new, ~10 cases)

- `POST /api/resume/{run_id}` for valid aborted run → 200, returns `new_run_id`
- `POST /api/resume/{run_id}` for `ok` run → 400 ("not aborted")
- `POST /api/resume/{run_id}` for dismissed run → 400 ("was dismissed")
- `POST /api/resume/{run_id}` for unknown run → 404
- `POST /api/resume/{run_id}/dismiss` → 200, row gets `dismissed_at` set
- Dismissing an already-dismissed row → 200 (idempotent)
- WS `connect` for thread with eligible interrupt → emits `interrupted_turn` event with correct fields
- WS `connect` for clean thread → no event
- WS `connect` for thread with CLI-only aborts → no event (filtered by `source != 'cli'`)
- WS `connect` for thread with expired TTL aborts → no event

### 12.3 `tests/test_telegram_bot.py` (extend if exists, else new, ~3 cases)

- `/resume` with no interrupted run → "No interrupted task to resume."
- `/resume` with eligible interrupt → fires resume + "Resuming previous task..."
- `/resume` with `source='web'` runs only (not telegram-scoped) → "No interrupted task" (telegram queries scope to `source='telegram'`)

### 12.4 Integration tests (extend `tests/test_integration.py`)

- Full abort + resume cycle: agent.run, force abort mid-stream, call `resume_interrupted_run`, assert two linked rows in `agent_runs` AND messages history shows `interrupted=true` row + new clean reply
- Token continuity: `resume.input_tokens >= original.input_tokens` (resume sees the prior conversation plus the partial reply → strictly more context)
- Resume's `provider` matches original (model can change if user switched provider, but provider key on the new row is what's actually active when resume fires — not the original)

### 12.5 Coverage

~38 new tests; existing coverage floor of 24% holds comfortably.

---

## 13. Rollout

### 13.1 Migration 009

`ALTER TABLE ADD COLUMN` on SQLite is O(1) — no row rewrite. Both columns nullable; old rows behave correctly without any data backfill. Atomic transaction. Idempotent (running it twice is fine since the schema_version gate prevents the second application).

### 13.2 Crash recovery first deploy

On the first restart after deploying this version, no `running` rows exist (a `running` row only exists between insert + finalize within a single agent_loop invocation). So the recovery hook is a no-op on first deploy. Going forward, any crash leaves orphans that the next startup cleans up.

### 13.3 Backward compatibility

- Pre-v0.20 abort flushes (which didn't write the partial-content row) leave the message history truncated at the user's question. The Web banner can still offer Resume if the `agent_runs` row exists and is `aborted` (it will, post-spec-#1). The model just sees less context on resume — works but with mildly worse continuity. No regression vs current behaviour.
- Pre-v0.20 `agent_runs` rows have `resumed_from_run_id IS NULL` and `dismissed_at IS NULL` — they remain resumable until TTL. Acceptable; the worst case is the user sees an old "Resume?" banner once. Dismiss is right there.

### 13.4 Performance

- `_check_for_resumable_interrupt`: one indexed query (`thread_id`), `LIMIT 1`. Sub-millisecond at 100k rows.
- `_recover_interrupted_runs_on_startup`: a full table scan for `status='running'`. Indexed on neither (it's a rare state). On a hot agent doing 1k runs/day, the `running` set at crash time is at most ~10 (each turn finalizes in seconds). Negligible cost on startup.
- Resume execution itself: just `agent.run` with a different opening prompt. No new hot path.

### 13.5 Release plan

- Version bump: v0.19.x → **v0.20.0** (minor — schema change + new behaviour)
- `RELEASE_NOTES.md` entry covering all four sources + the routine auto-resume window
- `docs/AUTO_RESUME.md` (new) — user-facing guide
- `CLAUDE.md` — Architecture sub-section under existing "Cost tracking" (3-4 paragraphs)

---

## 14. Open questions / known limitations

1. **Resume of a run whose model is no longer available**: e.g., user resumes a run that used `claude-3-haiku-20240307` after retiring that model from their provider. agent.run picks up whatever the current active model is — output may differ stylistically. The original run's model is still recorded in `agent_runs.model` for the audit trail; the new run's row records the new model. Acceptable; logged as expected behaviour in `AUTO_RESUME.md`.

2. **Streaming usage on aborted turns**: some providers send `usage` only in the final chunk. If abort fires before that chunk, `input_tokens` / `output_tokens` on the aborted row may be 0 even though the request was billable. Same trade-off as spec #1 §14.1 — surface as a "tokens may be incomplete" hint in the modal for `status='aborted'` rows.

3. **Resume vs Brave Search re-entry**: if the partial reply was mid-Brave-Search call when aborted, the tool result is NOT in the conversation. Resume's "continue" instruction asks the model not to repeat tool calls, but the model has no record of the search ever running. The model will likely re-fire the search. We don't have a way to avoid this without persisting in-flight tool state (out of scope; spec #2 territory).

4. **Concurrent resumes**: user opens two tabs to the same thread, sees the banner in both, clicks Resume in both at roughly the same time. The first call wins (creates `resumed_from_run_id=N`); the second call detects `already_resumed` via the `IN (SELECT resumed_from_run_id ...)` clause and returns 400 to that tab. No data corruption; user sees "Resume failed" toast in tab 2.

5. **Time-formatting nits**: `formatTimeAgo` may not exist in the current SPA. If it doesn't, write a 5-line helper as part of Task 9 (UI banner) — not a blocker.

---

## 15. Definition of done

- [ ] Migration `009_interrupted_runs.sql` applies atomically on fresh + existing DBs.
- [ ] `agent_loop.run_loop`'s `finally:` flushes partial content into `messages` with `meta.interrupted=true`.
- [ ] `_recover_interrupted_runs_on_startup` runs on server boot before scheduler.
- [ ] `agent.resume_interrupted_run` works for all four sources where applicable.
- [ ] `scheduler.detect_missed_runs` auto-fires aborted routine runs within `resume_ttl_routine_sec`.
- [ ] WS `interrupted_turn` event emitted on connect; banner UI renders and wires Resume / Dismiss.
- [ ] `POST /api/resume/{run_id}` + `POST /api/resume/{run_id}/dismiss` work + tested.
- [ ] Telegram `/resume` command works for telegram-scoped threads.
- [ ] Inline `⏸ interrupted` marker renders on the affected message.
- [ ] Sessions list shows `⏸ N` chip for threads with un-dismissed interrupted runs.
- [ ] Settings → Cost → Auto-resume sub-section saves all four settings (web TTL, telegram TTL, routine TTL, auto-routine toggle).
- [ ] `auto_resume` added to `FEATURES`; consent version bumped.
- [ ] `docs/AUTO_RESUME.md` written; `CLAUDE.md` extended; `RELEASE_NOTES.md` entry.
- [ ] `ruff check .` clean; `python scripts/check_js.py` clean; `pytest tests/` no new regressions; coverage ≥ 24%.
- [ ] Version bumped to v0.20.0 in `config.py`, `pyproject.toml`, README (if it carries the version).
