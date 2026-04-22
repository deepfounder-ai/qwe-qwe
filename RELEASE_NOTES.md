# v0.17.26 — Integration tests, slimmer install, schema migrations, dev docs

Four more tech-debt items off the priority list, landing in parallel. Pure structural work — no user-visible behavior changes, but the codebase is materially more resilient.

## ✅ C1. Integration tests (16 new)

`tests/test_integration.py` — the layer missing between unit tests and manual QA. Uses `TestClient(server.app)` + mocked LLM to exercise real endpoints end-to-end.

Covers:

- Server boots + serves SPA
- `/api/status`, `/api/soul`, `/api/settings`, `/api/threads`, `/api/knowledge/list` (the v0.17.23 regression guard)
- `/api/knowledge/url` — empty / non-http / private-IP SSRF / well-formed all behave correctly
- `/api/knowledge/search` on empty corpus doesn't crash
- `/api/knowledge/recent` round-trips synthetic history
- `/api/kv` allowlist / blocklist enforced
- WebSocket handshake
- **Agent turn smoke** — mocks `providers.get_client()` with a `FakeStreamingClient` that yields deterministic chunks. Runs `agent.run("hello", ctx=...)`, verifies `on_content` fires and messages land in SQLite.
- **Concurrent turn isolation** — two threads, two TurnContexts, separate replies, zero cross-contamination via the public HTTP surface.

If any of these had existed before, the v0.17.23 3.11 SyntaxError wouldn't have shipped.

## 📦 C3. `markitdown[all]` → `markitdown[docx,pptx,pdf,outlook]`

`[all]` was dragging ~115 MB of dead weight: `pandas` (xlsx — covered by our `openpyxl`), `speech_recognition` (we use faster-whisper), `azure-ai-documentintelligence` (Azure OCR — never called), `youtube-transcript-api` (broken by YT bot-detection — already replaced with `yt-dlp` in v0.17.13).

```toml
# before
"markitdown[all]>=0.1.0",
# after
"markitdown[docx,pptx,pdf,outlook]>=0.1.0",
```

**Measured install size**: 361 MB → **241 MB** (33% reduction). XLSX still works via our `openpyxl` fallback in `rag._read_xlsx`. Smoke-tested DOCX/PPTX/PDF/HTML/XLSX ingestion end-to-end in a fresh venv — all non-empty output.

## 🗃️ C4. SQLite migrations

`migrations/` directory + versioned SQL files. Replaces ad-hoc `CREATE TABLE IF NOT EXISTS` + `try/except ALTER TABLE` scattered across `db.py`.

- `migrations/001_initial.sql` — baseline: `messages`, `kv`, `presets`, `threads`, `scheduled_tasks`, `secrets`, FTS5 virtual tables, primary indexes.
- `migrations/002_message_thread_ts_index.sql` — first real migration (composite `(thread_id, ts)` index).
- `migrations/README.md` — convention: `NNN_snake_case.sql`, monotonically increasing. Runner applies in order, transactional per-file, tracks `schema_version` in kv.

**Back-compat heuristic** for existing installs: if `schema_version` is missing AND `messages` table exists, stamp `schema_version=1` without re-running baseline. Fresh installs apply every file.

5 new tests in `tests/test_migrations.py` covering fresh-apply, idempotency, back-compat stamping, rollback on invalid SQL.

## 📖 C5. `ARCHITECTURE.md` + `CONTRIBUTING.md`

- **`ARCHITECTURE.md`** (113 lines) — system diagram, core modules one-liner each, request lifecycle, memory 3-way hybrid (corrected vs CLAUDE.md's 2-way claim), state locations, extension points.
- **`CONTRIBUTING.md`** (93 lines) — setup, test commands, branching, commit style, CI guardrails, release flow, Dependabot.

Agent caught and corrected two outdated claims while grepping:
1. Memory search is **3-way hybrid** (dense + sparse + BM25 FTS5), not 2-way.
2. Core tools count is **28**, not ~18.

## 📊 Totals

```
ruff check .                  — 0 errors
pytest tests/                 — 186 passed (was 165 → +16 integration + 5 migrations)
import smoke                  — all modules load, TurnContext exported
Python 3.11 AST               — 0 findings
```

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

First upgrade: `_apply_migrations()` will stamp existing DBs at `schema_version=1` without re-running baseline. `pip install` downloads ~120 MB less. Your existing data is untouched.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
