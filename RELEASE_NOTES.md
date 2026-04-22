# v0.17.27 — JS lint, coverage floor, release automation

Three more tech-debt items closed. These are the last "nearly-free wins" from the audit.

## 🟨 D1. JS syntax check for `static/index.html`

5500-line inline vanilla JS + zero build step = bugs caught only by manual review. Now automated.

- **`scripts/check_js.py`** — pure Python helper that extracts every `<script>` block, writes to temp `.js`, runs `node --check`, remaps stderr line numbers back onto the HTML. No npm, no node_modules.
- **`.pre-commit-config.yaml`** — two hooks (ruff + check_js.py). `pip install pre-commit && pre-commit install` to opt in.
- **CI step** — runs `python scripts/check_js.py` between the AST syntax check and the import-time smoke. Node is preinstalled on GitHub runners; local dev without Node gets a friendly "skipped" (not a hard fail).

Verified: injecting `stae.x = 1 let bad;` produces `static/index.html:1396: SyntaxError: Unexpected identifier 'let'`.

eslint skipped — `npx eslint` would add an npm download for zero additional bug-catching beyond `node --check` on our codebase.

## 📊 D2. pytest-cov + 24% coverage floor

Measure + hold-the-line. Not chasing 90% — just ensuring regressions get caught.

- `pytest-cov` added to `[dev]` extras.
- `[tool.coverage.run]` + `[tool.coverage.report]` in `pyproject.toml` — source=`.`, omit tests/venv/build/static/setup, exclude `pragma: no cover` + `if __name__ == "__main__":` + stubs. `fail_under = 24` (2pp below the measured 25.95% baseline).
- CI replaces the `pytest -v` step with `pytest -v --cov --cov-report=term --cov-report=xml`. XML goes to the job summary for visibility.
- `CONTRIBUTING.md` documents `pytest --cov` as the canonical local run and states the floor policy.

**Baseline: 25.95% total**. 186/186 tests pass with or without `--cov`.

### Top 3 by coverage
1. `agent_budget.py` — 81%
2. `threads.py` — 80%
3. `logger.py` — 80%

### At 0% (useful data, not lies)
1. `cli.py` (1476 stmts) — entry point, not exercised by unit tests
2. `inference_setup.py` (159 stmts)
3. `synthesis.py` (173 stmts) — the night-synthesis job
4. `skills/browser.py` (300 stmts) — Playwright, env-dependent
5. `skills/mcp_manager.py`, `skills/spicy_duck.py`

These are candidates for future integration tests (the `skills/` ones are easy — just exercise `execute()` with mocked I/O). Not today.

## 🤖 D3. Release automation workflow

I hand-released 14 times today. Every one: bump VERSION in 3 files, write RELEASE_NOTES, commit, tag, push, `gh release create`. Error-prone — I hit merge conflicts on version bumps in parallel worktrees.

New `.github/workflows/release.yml` — triggers via `workflow_run: Tests completed success`. If `config.py` VERSION changed and the tag doesn't exist yet, auto-creates tag + GitHub release from `RELEASE_NOTES.md`.

### Gating chain
1. Push to main → `Tests` workflow runs (ruff → AST 3.11 check → JS check → import smoke → pytest --cov).
2. On green, `Release` workflow fires.
3. If `config.py` VERSION == `pyproject.toml` version ≠ any existing tag, tag + release.
4. If VERSION unchanged OR tag exists, no-op cleanly.
5. If `RELEASE_NOTES.md` is missing/empty when a new version is being released, fail loudly with a clear message.

### Idempotency (3 layers)
1. `git rev-parse --verify refs/tags/v$VERSION` — skip if tag already local
2. `gh api /repos/.../git/refs/tags/v$VERSION` — skip if tag on remote
3. `gh release view v$VERSION` — skip if release already exists (handles "tag pushed but release create failed" retry)

### This release is the first test

v0.17.27 is the first release where the workflow will fire. I'm still doing the manual bump + push (because the workflow doesn't exist yet on `main`); next release should be auto-cut by the workflow itself.

## 📊 Totals

```
ruff check .        — 0 errors
JS syntax           — 1 script, parses clean
pytest --cov        — 186 passed, 25.93% coverage (floor 24.0% ✓)
Python 3.11 AST     — 0 findings
import smoke        — all modules + FastAPI app
```

CI pipeline final form:

```yaml
1. Lint with ruff
2. Syntax check against Python 3.11 grammar
3. JS syntax check for static/index.html
4. Import-time smoke
5. Run tests with coverage (fail_under=24)
→ release.yml fires on green + VERSION change
```

## 📦 Upgrade

```bash
git pull && pip install -e .[dev] --upgrade  # pytest-cov lands here
pre-commit install                            # optional local hooks
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
