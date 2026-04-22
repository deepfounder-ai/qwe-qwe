# v0.17.19 — Abort propagation, concurrency safety, DST

Five plumbing fixes landing from the code-review follow-up. Focus: the agent now actually stops when you press Stop (even mid-shell, mid-HTTP), web + telegram turns can't cancel each other, lazy singletons don't duplicate under concurrent access, and scheduled daily tasks stay on wall-clock time across DST.

## A. Stop actually stops blocking tools

Before: `tool_executor` calls `subprocess.run(..., timeout=300)` for `shell` and `urlopen(..., timeout=15)` for `http_request`. The agent loop's abort check runs only between tool calls, so pressing Stop while a `shell` was running left the thread blocked for up to 300 s.

After: `shell` uses `subprocess.Popen` + a 200 ms polling loop that checks the per-thread abort event; on abort it kills the whole process tree (`taskkill /T /F` on Windows, `os.killpg` POSIX — so `bash -c "sleep 10"` doesn't leak a grandchild `sleep`). `http_request` drops the hardcoded 15 s timeout to a user-overridable 5 s (cap 30 s) and re-checks the abort event before and after the blocking read.

```python
# tools.py (shell branch)
abort_evt = _get_abort_event()
popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, ...)
if sys.platform == "win32":
    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    popen_kwargs["start_new_session"] = True
proc = subprocess.Popen([_SHELL_EXE, "-c", cmd], **popen_kwargs)
while True:
    if proc.poll() is not None: break
    if abort_evt is not None and abort_evt.is_set():
        aborted = True; break
    if time.monotonic() >= deadline:
        timed_out = True; break
    time.sleep(0.2)
```

Smoke test: `tools.execute("shell", {"command": "sleep 10"})` in a worker thread, set the abort event after 1 s — the call returns `"⏹ Shell aborted (user pressed Stop)."` in ~1.5 s.

## B. Per-request abort events — no more cross-source cancellation

Before: `agent._abort_event` was a single module-level `threading.Event`. The web WebSocketDisconnect handler called `_abort_event.set()`, which also aborted any concurrent Telegram turn running in the same process.

After: `agent.run()` takes an optional `abort_event` kwarg. Each WS session creates its own event, registered in `server._ws_abort_events`; WS disconnect sets only that session's event; `/api/abort` iterates the registered set so the Stop button still works from any client; REST callers that pass no event fall back to the module-level shared event (back-compat).

```python
# agent.py
def run(user_input, thread_id=None, source="cli", image_b64=None,
        abort_event=None):
    ...
    return _run_inner(..., abort_event=abort_event)

def _run_inner(..., abort_event=None):
    if abort_event is None:
        abort_event = _abort_event   # legacy fallback
    ...
    loop_result = run_loop(..., abort_event=abort_event)
```

```python
# server.py  (WS handler)
my_abort_event = threading.Event()
...
with _ws_abort_lock:
    _ws_abort_events.add(my_abort_event)
...
except WebSocketDisconnect:
    my_abort_event.set()   # was: _abort_event.set()
```

The abort event reaches blocking tools via a `threading.local` slot in `tools.py` (`_set_abort_event` / `_get_abort_event`) — `agent_loop._run_tool` sets it around each `tool_executor(...)` call so `shell` and `http_request` can poll it without the executor signature changing.

## C. Lock around lazy Qdrant / FastEmbed / MarkItDown singletons

Before: `memory._get_qdrant()` and `rag._get_qdrant()` both did `if _qclient is None: _qclient = QdrantClient(...)`. Two threads racing on first access constructed two clients; disk-mode Qdrant then raises `Storage folder already accessed by another instance`. Same race applied to `_get_dense_model`, `_get_sparse_model`, and `_get_markitdown`.

After: double-checked locking on each. Fast path stays unlocked after the singleton is built.

```python
# memory.py
_qclient_lock = threading.Lock()
...
def _get_qdrant():
    global _qclient
    if _qclient is None:
        with _qclient_lock:
            if _qclient is None:
                _qclient = QdrantClient(path=config.QDRANT_PATH)
                _ensure_collection(_qclient, config.QDRANT_COLLECTION)
    return _qclient
```

`rag._get_markitdown` got the same treatment — the previous `try: return _markitdown_cache except NameError` trick was racy and replaced with an explicit `_MARKITDOWN_UNSET` sentinel under lock.

Smoke test: 10 threads (5 × `memory._get_qdrant`, 5 × `rag._get_qdrant`) in parallel produce one distinct client id and zero errors.

## D. Compaction callback is a single slot (was append-only list)

Before: `_compaction_callbacks: list = []` + `on_compaction(cb)` appended without de-dup. Only `server.py` ever registered — once, at module load — so the list usually held one entry, but any caller (hot reload, a second server start in-process) silently duplicated.

After: a single slot, matching how `_status_callback` / `_content_callback` / `_tool_call_callback` are already wired. Re-registering replaces; `on_compaction(None)` unregisters.

```python
# agent.py
_compaction_callback = None

def on_compaction(callback):
    global _compaction_callback
    _compaction_callback = callback

def _notify_compaction(event, data):
    cb = _compaction_callback
    if cb is None: return
    try: cb(event, data)
    except Exception as e: _log.warning(f"compaction callback error: {e}")
```

## E. Scheduler: optional IANA timezone for DST

Before: `scheduler._tz()` returned `timezone(timedelta(hours=config.TZ_OFFSET))`, a fixed offset. A `daily 09:00` task in `Europe/Moscow` drifts ±1 h across DST-observing zones.

After: `config.get("tz_name")` (new `EDITABLE_SETTINGS` entry, default `""`) — when set to an IANA zone like `Europe/Moscow` or `America/New_York`, `_tz()` returns `zoneinfo.ZoneInfo(tz_name)`. Empty string or an invalid name falls back to the legacy fixed offset.

```python
# scheduler.py
def _tz():
    tz_name = (config.get("tz_name") or "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception as e:
            _log.warning(f"invalid tz_name={tz_name!r} ({e}); falling back to fixed offset")
    return timezone(timedelta(hours=config.TZ_OFFSET))
```

No schema change — existing `"daily HH:MM"` schedules keep firing; if `tz_name` is set they now fire at the user's wall-clock time across DST transitions.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you live in a DST-observing zone and rely on daily scheduled tasks, set `tz_name` in settings (or `setting:tz_name` in `kv`) to your IANA zone name.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
