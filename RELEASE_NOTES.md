# v0.18.0 — Business-ready repositioning

Minor bump because the project's positioning changed: qwe-qwe is now framed as a **self-hosted AI agent for business automation**, not just "an AI agent for small local models". No breaking API changes; the small-model heritage stays intact (it's why the system around the LLM works hard so the model doesn't have to). What changed is the framing of *who* it's built for.

## 🎯 Repositioning

- **Tagline**: `AI agent optimized for small local models` → `Self-hosted AI agent for business automation`
- **README opener**: leads with self-hosted, bring-your-own-LLM, deploys on your infra. Mentions Azure OpenAI, AWS Bedrock, Groq, OpenRouter alongside local LM Studio / Ollama.
- **soul.py identity**: agent introduces itself as `qwe-qwe — a self-hosted AI agent for business automation` instead of `personal AI assistant... lightweight offline`.
- **Default user name**: `Boss` → `User` (neutral for team deployments).
- **pyproject description**: matches the new tagline.

What's deliberately **not** changed: no fake enterprise claims (no multi-tenant, no RBAC, no SOC2 promises). The agent stays single-process, hackable, and laptop-friendly. The "Why Small Models" section in the README is preserved — small/local is still a real differentiator for self-hosted deployments where data must not leave the building.

## 🧠 Memory page unified (file/url/fact in one list)

Previously the Memory page had two parallel sections — "Sources" (indexed files/URLs) and "Saved memories" (atomic facts). Same Qdrant collection underneath, just two views. This was confusing: users would save a chat message via the 📖 button and ask why it didn't show in Sources.

Now: one list with top-level chips `all / file / url / fact`. Each row shows a category badge. Fact rows expand inline to show the full text. The eyebrow counts both kinds together: `MEMORY · N chunks · size · N sources · N facts`.

## 🌱 UI saves now feed synthesis

Direct saves from the chat's 📖 button were marked `synthesis_status="skip"` and never picked up by the night entity/wiki extraction pipeline. The chunked path (for files >1000 chars) marks chunks as `pending`, but single-atom saves were skipped permanently.

Added a `synth: bool = False` kwarg to `memory.save()` — opt-in flag that promotes single-atom saves to `pending` so they participate in night synthesis. `/api/memory/save` now passes `synth=True` so the UI button feeds the knowledge graph.

Default behaviour unchanged for the agent's own `memory_save` tool path (kwarg defaults to `False`).

## 📜 `/api/memory/list` returns full text

Was returning `preview = text[:240]`. UI showed a truncated preview even when expanded, because there was no full text to expand to. Now returns `text` (capped at 20K per atom) plus a `truncated: bool` flag, and the UI uses the full content. The 20K cap prevents a runaway 1MB payload if someone saved a giant document as a single atom.

## 🤐 Soul rule 16 — end turn for external-wait flows

When a step needs the user to do something **outside the chat** (browser OAuth, 2FA code, hardware-key touch, email confirmation, manual upload, paste-back of a code) — the shell tool's 120s timeout breaks the flow: commands like `gcloud auth login` start a localhost callback server that dies the moment shell times out, and the OAuth dance never completes.

The new rule: launch the operation in non-blocking mode (`--no-launch-browser`, `--device-code`, `--manual`), surface the URL via `open_url`, tell the user the exact next step, and **end the turn**. This is the explicit exception to rule 3 (NEVER STOP EARLY). The agent picks up on the user's next message.

This is the same pattern Claude Code uses (no built-in pause primitive — session resumes on next user message). A proper architectural primitive (`await_user` tool with explicit pause-marker for the UI) is deferred to a future release.

## 🧪 Test pollution fix — production logs no longer get test errors

`logger.py` bound file handlers to `config.LOGS_DIR` once at import. The `qwe_temp_data_dir` fixture repointed `QWE_DATA_DIR` to a tempdir but didn't reload `logger`, so test-simulated failures (scheduler "network is on fire", preset unsafe-archive, migration syntax errors) kept landing in the user's real `~/.qwe-qwe/logs/errors.log`.

7,971 lines accumulated in errors.log over the test history — none of them real production failures, all test fixtures.

Fix: detect `"pytest" in sys.modules` at import time and skip the `RotatingFileHandler` setup. Console handler stays for CRITICAL only. pytest captures stderr/stdout, so nothing's lost for debugging.

After the fix: 7,971 → 7,971 lines after a 63-test run across `test_scheduler_cron`, `test_presets`, `test_migrations`. Full suite still 349/349 green.

## 🔄 Upgrading

```bash
git pull && pip install -e .
```

No data migration needed. Existing memory entries, threads, and presets keep working. The two memory storage paths (Qdrant + markdown atoms in `~/.qwe-qwe/memories/atoms/`) are unchanged.
