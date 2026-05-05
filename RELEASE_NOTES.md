# v0.18.1 — Bug fixes from external reports

Patch release closing three bugs reported on the issue tracker by **@EugeneKorr** (VegaEx). Each issue came with a clean repro and a working patch — pure pleasure to merge. No new features.

## 🔁 #10 — `_extract_tool_from_text` missing pattern for `!<function_call:>` format

**Symptom users saw**: "infinite reply" — the model kept generating but the chat never advanced. Reported on Qwen 3.5 9B served via LM Studio, but the underlying mechanism applies to any model that emits this format.

**Mechanism**: some LM Studio / Ollama-served models (notably certain Qwen variants) emit tool calls wrapped like:

    !<function_call:{"call": "tool_name", "arguments": {...}}>

Note the key is `"call"`, not `"name"`, and the outer wrapper is different from the four existing patterns in `agent_loop._extract_tool_from_text()`. None of P1–P4 matched, so the call rendered as raw text in the assistant's reply. The model never observed a tool result, often retried the same call forever, and the user saw an endless wall of text that didn't act on anything.

**Fix**: added Pattern 5 with defensive handling (accepts `call` *or* `name` key, treats `arguments: null` and non-dict args as empty `{}`, unknown tool names return `None`). New test file `tests/test_text_to_tool_extraction.py` covers all 5 patterns and the edge cases — 17 tests, full suite now 368/368.

## 🐳 #9 — Dockerfile missing `COPY migrations/`

**Symptom**: every fresh Docker deploy crashed every scheduled task with `sqlite3.OperationalError: no such column: thread_id`.

**Root cause**: `Dockerfile` copied `*.py`, `skills/`, and `static/` but not `migrations/`. `db._apply_migrations()` found 0 files, `schema_version` stayed at 0, migration 004 (which adds `thread_id` to `scheduled_tasks`) never ran.

**Fix**: one line — `COPY migrations/ migrations/` alongside the other source copies. Layer-caching ordering preserved.

## 📱 #11 — Telegram final message clobbered skill `emit_content()`

**Symptom**: a skill that streams a result via `ctx.emit_content(text)` instead of relying on the LLM to echo — user briefly saw the skill output during streaming, then it vanished and was replaced with a short LLM-round-2 reply.

**Root cause**: the final `editMessageText` used `response` (= `result.reply` = LLM-only text), which doesn't include direct skill emissions. `_stream_buf` already had the full streamed content (LLM text + skill emits) but was only used for intermediate streaming updates.

**Fix**: at `telegram_bot.py:1843`, switch the final message body to `_stream_buf.strip()` when non-empty, fall back to `response` when no streaming happened. For normal tools (no `emit_content` calls), `_stream_buf` already equals `result.reply`, so existing behaviour is unchanged.

A regression-shield test (`tests/test_telegram_stream_buf_fence.py`) uses `inspect.getsource()` to assert the fix stays in place. `_handle_message` is too closure-heavy to unit-test cleanly without ~200 lines of mocking; the fence guards against accidental reverts at PR-review time.

## 🔄 Upgrading

```bash
git pull && pip install -e .
```

No data migration needed. Existing memory entries, threads, presets, skills, and Telegram setup keep working as-is.

## 🙏 Credits

All three issues filed by [**@EugeneKorr**](https://github.com/EugeneKorr) with full repros and ready-to-apply patches. Fastest end-to-end issue → fix → release flow we've had on this project.
