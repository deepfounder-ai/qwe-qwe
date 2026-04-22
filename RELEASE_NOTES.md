# v0.17.25 — TurnContext: per-request state isolation

The big structural fix from the tech-debt plan. Eliminates cross-source contamination between concurrent turns (web + Telegram + CLI running in the same Python process).

## 🐛 What was broken (silent)

Several per-turn pieces of state lived as **module-level globals** in `agent.py`:

- `_pending_image_path`, `_pending_file` — the image/document attached to "the current turn"
- `_content_callback`, `_thinking_callback`, `_status_callback`, `_tool_call_callback`, `_recall_callback` — where to send streaming tokens / tool events

When a web turn and a Telegram turn ran concurrently, whichever one set these last won. Result: Telegram would sometimes get the web client's tool-call events, web would get Telegram's thinking, pending images from turn A leaked into turn B. Rarely caught because 99% of installs have one active user, but structurally wrong — and the pattern was going to bite harder as soon as any user ran web + Telegram simultaneously.

v0.17.19 already fixed `_abort_event` with per-request `threading.Event`. This release does the same treatment for everything else.

## 🔧 The fix

### New: `turn_context.py` + `TurnContext` dataclass

```python
@dataclass
class TurnContext:
    abort_event: threading.Event = field(default_factory=threading.Event)
    on_content: Callable[[str], None] | None = None
    on_thinking: Callable[[str], None] | None = None
    on_status: Callable[[str], None] | None = None
    on_tool_call: Callable[[str, str, str], None] | None = None
    on_recall: Callable[[list[dict]], None] | None = None
    image_path: str | None = None
    file_meta: dict | None = None
    source: str = "cli"          # "web" | "telegram" | "cli"
    session_id: str | None = None
```

### Context propagation via `ContextVar`

Rather than thread `ctx` through every helper in `agent.py`, the current context lives in a `contextvars.ContextVar`:

```python
_current_turn_ctx: ContextVar[TurnContext | None] = ContextVar("...", default=None)
```

`_run_inner` sets it at the top of each run. `_emit_content` / `_emit_thinking` / etc. read it. Each OS thread (or asyncio task) gets its own isolated copy — no leak between concurrent turns.

### Wire-up

- `agent.run(..., ctx=...)` accepts an optional `TurnContext`. If omitted, creates a fresh one (CLI compat).
- `agent_loop.run_loop()` extended to take `ctx` + reads callbacks from it.
- `server.py` WS handler builds `TurnContext` per connection, installs `on_content → WS queue`, passes to `agent.run(..., ctx=my_ctx)`. On `WebSocketDisconnect`, `my_ctx.abort_event.set()`.
- `telegram_bot.py` stashes ctx on `threading.local`; `server._telegram_handler` retrieves + passes to agent.

### Back-compat shim

Old code that sets `agent._content_callback = fn` still works. `_harvest_legacy_slots(ctx)` runs at the top of every `agent.run()`, copies any present legacy attributes onto the freshly built ctx, and emits a one-shot `DeprecationWarning` per slot. Explicit `ctx=...` callers keep their own values (the harvester only fills `None` fields).

## ✅ Verification

New test: `tests/test_turn_context.py`:

```
test_cross_source_isolation_two_threads — PASS
```

Two threads, each with its own `TurnContext` and `on_content` callback. Emit 100 labelled events per thread concurrently. Callback A sees only A-labelled events, callback B only B's. Zero cross-contamination. Six more tests cover the harvester / back-compat / ContextVar reset / deprecation warning.

## 📊 Totals

```
ruff check .         — 0 errors
pytest tests/        — 165 passed (was 158 — +7 new turn_context tests)
import smoke         — `from agent import TurnContext` exports visible
Python 3.11 AST      — 0 findings
```

## 🔍 Module globals deliberately kept

- `_structured_output_failed` — provider capability tracking, not per-turn
- `_compaction_lock` — cross-turn serialisation of DB writes
- `_compaction_callback` — background subsystem hook, not per-turn
- `agent._last_tools` — pre-existing stash read by Telegram bot; separate refactor

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

No user-visible change if you use only one client at a time. If you run Web + Telegram concurrently, you'll notice events stop getting crossed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
