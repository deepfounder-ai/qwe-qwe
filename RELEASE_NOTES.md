# v0.17.18 — Security hardening pass

Six focused fixes across memory, server, and the agent loop. No new features;
the goal is to close gaps an authed user (or a mis-behaving tool) could use to
exfiltrate secrets, pivot to localhost, or corrupt internal state.

## A. memory.save() scrubs secrets before persistence

Common key shapes (OpenAI, Anthropic, Groq, GitHub, AWS, Slack, JWT) and
dotenv-style `NAME_KEY=value` lines are redacted before the text hits Qdrant.

```python
# memory.py
def _scrub_secrets(text: str) -> tuple[str, bool]:
    ...  # returns (scrubbed, was_scrubbed)

def save(text, tag="general", ...):
    text, scrubbed = _scrub_secrets(text)
    if scrubbed:
        hits = text.count("[REDACTED")
        _log.warning(f"scrubbed {hits} secret-like pattern(s) ...")
    ...
```

The anthropic pattern is ordered before the generic `sk-` pattern so
`sk-ant-…` correctly labels as `anthropic_key`, not `openai_key`. Covered by
`tests/test_secret_scrub.py` (11 cases).

## B. /api/knowledge/url refuses private/loopback targets

Before registering the background fetch, resolve the URL's hostname and reject
it if any resolved address is private, loopback, link-local, or unspecified.
Escape hatch: `QWE_ALLOW_PRIVATE_URLS=1`.

```python
# server.py
if os.environ.get("QWE_ALLOW_PRIVATE_URLS", "").strip() != "1":
    err = _url_resolves_to_private(url)
    if err:
        return JSONResponse({"error": err}, status_code=403)
```

`_url_resolves_to_private` uses `socket.getaddrinfo` + `ipaddress.ip_address`
so DNS rebinding to a private IP is caught too, not just literal IPs.

## C. Cleared tool-result stubs no longer carry raw bytes

`_clear_old_tool_results` previously stored the first 120 chars of content as a
summary — if a tool printed a key, the stub leaked it back to the model. Now
the stub is length + tool name only.

```python
# agent_loop.py — before
first_line = content.split("\n")[0][:120].strip()
m["content"] = f"[cleared — {first_line}]"

# after
n = len(content) if isinstance(content, str) else 0
m["content"] = f"[cleared — {n} chars of {tool_name} output]"
```

Tool name is looked up via `tool_call_id` → preceding assistant's `tool_calls`.

## D. Startup sweeps stale uploads (>14 days)

On FastAPI `lifespan` startup, delete files in `config.UPLOADS_DIR` older than
14 days. Bounded at 10k inspected files. `uploads/kb/` is skipped — those back
indexed knowledge sources and live until the user removes the KB entry.

```python
# server.py
def _sweep_uploads(max_age_days=14, max_files=10000) -> tuple[int, int]:
    ...
    kb_dir = (upl / "kb").resolve()
    for p in upl.rglob("*"):
        ...
        if kb_dir in rp.parents or rp == kb_dir:
            continue  # KB sources are user-managed
        if st.st_mtime >= cutoff:
            continue
        p.unlink(); deleted += 1; bytes_freed += size
```

## E. providers._reset_structured_output_cache no longer nukes the set

Line 402 used to do `agent._structured_output_failed = False`, replacing the
`set[str]` with a bool. The next `provider in agent._structured_output_failed`
then crashed with `TypeError: argument of type 'bool' is not iterable`.

```python
# providers.py — before
agent._structured_output_failed = False
# after
agent._structured_output_failed.clear()
```

Only one call site; nothing else does the same assignment.

## F. /api/kv POST rejects writes to reserved prefixes

Any authed user could previously overwrite `telegram:owner_id`,
`version:latest`, `provider:config:<name>`, or any internal setting via a plain
`{key, value}` POST. Now those prefixes 403 and the key length is capped at
200 chars.

```python
# server.py
_KV_WRITE_BLOCKLIST: tuple[str, ...] = (
    "telegram:owner_id", "version:", "setup_", "_migrated_",
    "provider:config:", "setting:", "soul:",
)

for prefix in _KV_WRITE_BLOCKLIST:
    if key.startswith(prefix):
        return JSONResponse({"error": ...}, status_code=403)
```

## Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```
