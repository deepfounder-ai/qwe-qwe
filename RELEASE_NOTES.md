# v0.17.18 — Security hardening + UX cleanup

Eleven fixes shipping together. Two batches landed in parallel — one focused on closing security/disk gaps an authed user or misbehaving tool could exploit, the other on UX papercuts and an actual handler leak.

## 🔐 Security & disk (6 fixes)

### A. `memory.save()` scrubs secrets before persistence

Common key shapes (OpenAI, Anthropic, Groq, GitHub, AWS, Slack, JWT) and dotenv-style `NAME_KEY=value` lines are redacted before text hits Qdrant.

```python
# memory.py
def _scrub_secrets(text: str) -> tuple[str, bool]:
    ...  # returns (scrubbed, was_scrubbed)

def save(text, tag="general", ...):
    text, scrubbed = _scrub_secrets(text)
    if scrubbed:
        _log.warning(f"scrubbed {text.count('[REDACTED')} secret-like pattern(s)")
    ...
```

The Anthropic pattern is ordered before the generic `sk-` pattern so `sk-ant-…` correctly labels as `anthropic_key`, not `openai_key`. Covered by `tests/test_secret_scrub.py` (11 cases).

### B. `/api/knowledge/url` refuses private / loopback / link-local targets

Before registering the background fetch, resolve the URL's hostname and reject if any resolved address is private, loopback, link-local, or unspecified. Escape hatch: `QWE_ALLOW_PRIVATE_URLS=1`.

```python
if os.environ.get("QWE_ALLOW_PRIVATE_URLS", "").strip() != "1":
    err = _url_resolves_to_private(url)
    if err:
        return JSONResponse({"error": err}, status_code=403)
```

`_url_resolves_to_private` uses `socket.getaddrinfo` + `ipaddress.ip_address` so DNS rebinding to a private IP is caught too, not just literal IPs.

### C. Cleared tool-result stubs no longer carry raw bytes

`_clear_old_tool_results` previously stored the first 120 chars of content as a summary — if a tool printed a key, the stub leaked it back to the model. Now the stub is length + tool name only.

```python
# agent_loop.py
# before:  f"[cleared — {first_line[:120]}]"
# after:   f"[cleared — {n} chars of {tool_name} output]"
```

Tool name is looked up via `tool_call_id` → preceding assistant's `tool_calls`.

### D. Startup sweeps stale uploads (> 14 days)

On FastAPI `lifespan` startup, delete files in `config.UPLOADS_DIR` older than 14 days. Bounded at 10 k inspected files. `uploads/kb/` is skipped — those back indexed knowledge sources and live until the user removes the KB entry.

### E. `providers.py` no longer nukes `_structured_output_failed`

Line 402 used to do `agent._structured_output_failed = False`, replacing the `set[str]` with a `bool`. The next `provider in agent._structured_output_failed` crashed with `TypeError`. Now `.clear()`.

### F. `/api/kv` POST rejects writes to reserved prefixes

Any authed user could previously overwrite `telegram:owner_id`, `version:latest`, `provider:config:<name>`, or any internal setting via a plain `{key, value}` POST. Now those prefixes 403 and key length is capped at 200 chars.

```python
_KV_WRITE_BLOCKLIST = (
    "telegram:owner_id", "version:", "setup_", "_migrated_",
    "provider:config:", "setting:", "soul:",
)
```

## 🎨 UX cleanup (5 fixes)

### G. Toast cap + dedup

Previously `toast()` just appended a new `<div>` per call — 50 rapid saves → 50 stacked toasts. Now capped at 5 concurrent (oldest evicted) and same `msg + kind` within a 500 ms window deduplicates (updates timestamp instead of stacking).

### H. Graph pan-handler leak fixed

`document.addEventListener('mousemove'/'mouseup'/'mouseleave', …)` inside the graph's wireEvents block was attached on **every** render — accumulating dead handlers with stale `panning` closures.

Now attached once (guarded with `state._graphGlobalHandlersAttached`). The handler dereferences `state._graphPan.svg` fresh each tick and bails out via `svg.isConnected` when the SVG was removed mid-drag by a re-render.

### I. Provider key modal: Enter-to-submit

`openProviderKeyModal` now wires `keydown` on both `#pk-url` and `#pk-key` so Enter triggers the primary "Save + switch" action (matches the pattern in `openLoginModal`).

### J. Dead code removed

`state.lastTurnId` was referenced but never assigned anywhere — `toolCount` in the Inspector was permanently 0. Deleted the dangling reference. Left a one-line comment so it doesn't get re-added from muscle memory.

### K. `_save_experience` filters cover Russian inflections + more meta tools

- `_MEMORY_KEYWORDS` now includes: `забыл`, `забыла`, `забудьте`, `забываешь`, `запомнил`, `запомните`, `вспомни`, `вспомнил`.
- `_META_TOOLS` now includes: `soul_editor`, `skill_creator`, `add_trait`, `remove_trait`, `list_traits`, `rag_index`, `user_profile_get`.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you've been collecting uploads for a while, the startup sweep will free space on the first restart — check the log line for bytes reclaimed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
