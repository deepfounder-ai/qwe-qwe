## v0.22.1 — Migration reliability fix

- **fix(db)**: SQLite migration runner now executes statements one-by-one instead of via `executescript()`. This makes `ALTER TABLE ADD COLUMN` migrations idempotent: if `scheduler._ensure_table()` or any other helper pre-creates a column before a migration runs, the "duplicate column name" error is silently skipped rather than aborting the entire migration. Eliminates a test-ordering flakiness introduced after the v0.20 / v0.21 merge.
- Internal: `_iter_sql_statements()` strips `--` line comments *before* splitting on `;`, so in-comment semicolons (e.g. `-- doesn't rewrite rows; each …`) no longer produce spurious SQL fragments.
- No schema changes, no API changes, no migration files modified.

---

## v0.22.0 — Auto-resume after interrupt

- Every abort (WS disconnect, Stop button, server crash) is now recoverable.
- Web UI shows a banner on reconnect: "Previous turn was interrupted — Resume / Dismiss". The agent picks up from where it left off, not from scratch.
- Telegram exposes `/resume` for the same flow in chat.
- Routines auto-resume if the abort was within 5 minutes (configurable).
- CLI Ctrl+C remains an intentional stop — no resume.
- New per-source TTL settings in Settings → Cost → Auto-resume: Web (7 days), Telegram (24h), Routines (5 min).
- Migration 009 adds `resumed_from_run_id` + `dismissed_at` to `agent_runs`.
- Analytics chain resume runs back to their originals.

---

## v0.21.0 — Per-routine budget caps

- Set a USD spending cap per routine, rolling over a configurable window.
- When the cap is reached, the next scheduled fire is SKIPPED with
  `status='skipped'`, `error='budget_exceeded'` in agent_runs — history
  shows what happened. The routine resumes once spend drops below the cap.
- UI: Routines page shows a budget chip per routine (green / orange /
  red based on % of cap). Click to set/clear/edit cap + period.
- API: `GET /api/routines/{id}/budget` and `POST /api/routines/{id}/budget`.
- Migration 010 adds `budget_usd_cap` + `budget_period_sec` to
  `scheduled_tasks`. Pre-existing routines have no cap (default).

---

## v0.19.0 — Cost tracking & per-session analytics

- New `agent_runs` table replaces `routine_runs`: one row per LLM call site
  (main loop, synthesis, skill creator, routine fire) with full token + cost
  capture.
- Online pricing from the LiteLLM community JSON, cached locally, with a
  bundled top-10 fallback for offline / air-gapped operation.
- Sessions list now shows Tokens + Cost per thread; click a row for a
  per-run drilldown with model, source, status, duration, tokens, and cost.
- Routines page shows Cost (30d) so you can spot expensive scheduled jobs.
- New Settings → Cost tracking section: pricing URL, auto-update toggle,
  manual refresh button.
- API: `GET /api/threads` extended with `input_tokens / output_tokens /
  cost_usd / run_count`; new `GET /api/threads/{id}/runs`,
  `GET /api/analytics/period`, `GET /api/pricing/status`,
  `POST /api/pricing/refresh`.
- Migration 008 atomically replaces legacy `routine_runs` with the new
  `agent_runs` table.

---

# v0.18.7 — Canvas (sandboxed HTML side panel) + Skill import (skills.sh / Anthropic SKILL.md spec)

Two big features land together because they're the same idea from opposite directions: **richer output → user** (Canvas), and **more capabilities ← community** (Skill import). Plus a Tools & skills tab rebuild so the growing skill list stays usable.

---

## 🎨 Canvas — sandboxed HTML in a side panel

The agent can now ship arbitrary HTML to a 480px right-side panel. Three concrete things this unlocks:

### 1. Interactive forms — the agent asks back, structured

```
You:    Сделай форму записи нового клиента: ФИО, телефон, источник.
Agent:  [canvas_prompt html="<form>…</form>" title="New client"]
        → panel slides in on the right
You:    *fills the form, hits Submit*
Agent:  → receives {name:"...", phone:"...", source:"..."} as the tool result
        [memory_save "Новый клиент: ..."]
        Saved. Записал.
```

`canvas_prompt` **blocks** until the user submits, exactly like `camera_capture` blocks until a frame is grabbed. The agent gets the form data back as JSON in the same turn — no manual "type each field into chat" step.

### 2. Dashboards & status views — pin them, come back next week

```
You:    Покажи дашборд по продажам за последнюю неделю.
Agent:  [canvas_render html="<div style='…'>…<canvas id='chart'></canvas>…"]
        → renders a styled HTML page with a Chart.js bar chart
You:    Сохрани его как weekly-sales.
Agent:  [canvas_save slug="weekly-sales"]
        ✓
```

Saved artifacts show up in a new **Canvases** left-nav view (card grid alongside Memory / Scheduler / Presets). Click a card → panel reopens with the saved dashboard. Reload the chat → the message that opened it has a chip "📊 Canvas: weekly-sales" you can click to reopen.

### 3. Mockups & prototypes — visual iteration in chat

```
You:    Накидай мокап лендинга для приложения «Поход в горы».
Agent:  [canvas_render html="<header>…</header><section class='hero'>…"]
        → panel renders the layout
You:    Сделай hero на тёмном фоне и кнопку CTA крупнее.
Agent:  [canvas_render …]
        ✓
```

The agent iterates the HTML in chat, you see each version side-by-side with the conversation.

### Security model — iframe sandbox is load-bearing

`<iframe sandbox="allow-scripts allow-forms" srcdoc="...">`. Note what's **NOT** there:

- ❌ `allow-same-origin` — iframe origin is `"null"`, no parent cookies / localStorage / DOM
- ❌ `allow-top-navigation` — can't redirect the host page
- ❌ `allow-popups` — no `window.open`

The parent listens for `postMessage` from the iframe and filters by `event.source === iframe.contentWindow` (origin-string filtering is useless when the origin is `"null"`). The iframe CAN load public CDN scripts (Chart.js, D3) without cookies, documented as a privacy note in `docs/CANVAS.md`.

256 KB HTML cap enforced at both skill-side and the REST `POST /api/canvas/artifacts` endpoint. Charts with inlined SVG fit comfortably; LLMs can't reliably emit more anyway.

### Five tools, auto-active

`tool_search("dashboard")` / `"form"` / `"mockup"` / `"chart"` / `"widget"` → activates the canvas tools without manual setup:

- `canvas_render(html, title?, slug?)` — fire-and-forget, opens the panel
- `canvas_prompt(html, title?, timeout_s=300)` — blocks until submit / close / timeout, returns user data as JSON
- `canvas_save(slug, title?, html?)` — persist as artifact
- `canvas_load(slug)` — reopen a saved artifact
- `canvas_list(limit=20)` — markdown table of saved artifacts

Full postMessage protocol, sandbox limits, and a reference HTML template live in `docs/CANVAS.md`.

---

## 📦 Skill import — install community skills from skills.sh / GitHub

Anthropic's [agentskills.io SKILL.md spec](https://agentskills.io/specification) — the same format Claude Code / Claude.ai use — now works in qwe-qwe via a thin adapter layer. Browse [skills.sh](https://skills.sh) or any compatible GitHub repo, paste the URL into Settings → Tools & skills → **Import skill**, click Import.

### Recognised URL shapes

- `https://skills.sh/<owner>/<repo>/<skill-name>`
- `https://github.com/<owner>/<repo>/tree/<ref>/<path-to-skill>`
- `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path-to-skill>/SKILL.md`

### How the bridge works

skills.sh skills are **markdown instructions for an LLM** + optional executable scripts. qwe-qwe skills are **single Python modules with `TOOLS` + `execute()`**. The importer generates a thin adapter `.py` at `~/.qwe-qwe/skills/<name>.py` that exposes one tool — `<name>_help` — returning the full SKILL.md body. Scripts / references / assets land at `~/.qwe-qwe/skills_imported/<name>/`. The agent reads them via the regular `read_file` / `shell` tools.

Best for **knowledge-heavy procedures** (PDF manipulation patterns, document conversion recipes, etc.). Pure-code wrappers around a specific API are still better written natively via `create_skill`.

### Safety surface — none of this is optional

| Layer | What it does |
|---|---|
| **Domain allowlist** | Only `skills.sh` / `github.com` / `raw.githubusercontent.com` / `api.github.com`. Everything else → HTTP 403 `host_not_allowed`. |
| **SSRF guard** | Private / loopback / link-local IPs blocked via `socket.getaddrinfo` + `ipaddress.ip_address`. Plus a custom `HTTPRedirectHandler` re-validates every redirect hop — a public-host fetch can't 302 into 127.0.0.1 or cloud metadata IPs. |
| **Name validation** | `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤64 chars (the agentskills.io regex). |
| **Built-in collision** | `browser`, `canvas`, `skill_creator`, etc. **cannot be replaced** even with `overwrite: true`. Typosquatting defense. |
| **License surfacing** | Word-anchored SPDX-ish regex + denylist of non-OSS riders (Commons Clause / BUSL / SSPL / Elastic / "Complete terms in LICENSE.txt"). Non-OSS licenses return HTTP **451 `license_confirm_required`** — the UI shows a confirmation panel with the license text before installing. |
| **Size caps** | SKILL.md ≤100 KB, total fetch ≤1 MB, ≤50 files, binaries / images filtered out. |
| **Atomic write** | Adapter writes to a tempfile, runs `skills.validate_skill` on it, **then** `os.replace` into final position. A broken renderer can never leave a half-written `.py` in `~/.qwe-qwe/skills/`. |
| **Sentinel-protected delete** | `delete_import` checks for the auto-generated sentinel before unlinking. If you replaced an imported skill's `.py` with hand-written code, your file survives. |
| **Audit trail** | Every install recorded in the `skill_imports` table — source URL, SHA-256 hash, license, timestamp. Query via `GET /api/skills/imports`. |

### REST round-trip

```bash
curl -X POST http://localhost:7861/api/skills/import \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://skills.sh/anthropics/skills/pdf"}'
```

Returns HTTP 451 if the upstream license isn't OSS; re-POST with `"accept_license": true` to confirm.

Full pattern doc + reference implementations: `docs/SKILLS_IMPORT.md`.

---

## 🔍 Tools & skills tab — search + collapsible categories

The Tools tab in Settings used to be a flat list. As the skill ecosystem grows (built-ins + user-created + imported), that flat list becomes unscannable. New layout:

- **Search box** at the top — filters across tool name, description, and category
- **Collapsible category headers** — Memory / Files / Web / Browser / Hardware / Skills / Meta. Expand only what you need.
- **Import skill button** in the header — paste URL, install in one step.

The user-created and imported skills appear in their own categories so you can tell where each tool came from at a glance.

---

## 🐛 Notable fixes

- **`fix(agent)` — tool_call argument normalization.** Some models (notably Qwen 3 variants) emit `tool_calls` with already-stringified-but-invalid JSON in `arguments` (single quotes, trailing commas). Replay through `_history_with_tool_calls` would crash with `JSONDecodeError` and break the turn. Now normalized to valid JSON before replay.

- **`fix(canvas)` — cross-thread leak + tool confusion.** The model couldn't "read forms back" because `_pending_canvas_renders` was a module global keyed by request_id only — concurrent threads would step on each other. Now bucketed by thread_id.

- **`fix(canvas)` — stale server message.** If you reload qwe-qwe after upgrading the server, the JS knows about canvas tools but the server doesn't have the endpoint yet. We now show a clear "restart qwe-qwe" toast instead of a confusing 404.

---

## 📈 By the numbers

- **+725 tests passing** (was 545 at v0.18.6) — +180 new tests covering canvas + skill_import + their JS contracts
- **109 tests in `test_skill_import.py`** alone — including a "live integration" path gated by `RUN_LIVE_TESTS=1` that fetches a real skills.sh skill
- **Coverage floor unchanged** at 24% — actual 25.93%
- **Two new SQLite migrations** — `006_canvas_artifacts.sql`, `007_skill_imports.sql`
- **Two new pattern docs** — `docs/CANVAS.md`, `docs/SKILLS_IMPORT.md`

---

## ⬆️ Upgrade

```bash
git pull
pip install -e . --upgrade
python cli.py --web --ssl --port 7861
```

Two new migrations apply automatically on first boot. No config changes needed. Telemetry consent unchanged (no new event types).

---

## 🙏 Inspirations

Canvas takes obvious inspiration from Claude.ai's Artifacts — but the sandboxed-iframe-only approach matters more here, since qwe-qwe runs on your own machine and Anthropic doesn't sit between the LLM and your filesystem. Skill import works because Anthropic published the [agentskills.io spec](https://agentskills.io/specification) as a portable format — you can drop the same `SKILL.md` into Claude Code, Claude.ai, and qwe-qwe and it works in all three. The [skills.sh](https://skills.sh) catalog made discovery trivial.
