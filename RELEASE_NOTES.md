# v0.17.24 — Tech-debt Phase A: lint, hoisted imports, clean tests, dependabot

Four parallel fixes landing together from the tech-debt audit. No user-visible features — the goal is structural reliability so future releases don't hide bugs behind blind spots (like v0.17.23's 3.11 SyntaxError that pytest never touched).

## 🧹 A. Ruff lint in CI + 7 real bugs caught

`ruff` was in `[dev]` deps but never run. First run surfaced **199 findings**, split into:

- **7 genuine bugs fixed**:
  1. `cli.py` — missing `import os` in `/preset list` branch → `NameError` on execution
  2. `server.py` `_broadcast()` — `_ws_clients -= dead` inside closure made `_ws_clients` local → `UnboundLocalError`. Swapped to `.difference_update()`
  3. `telegram_bot.py` — dropped `-> "Path"` annotation referencing unimported `Path`
  4. `cli.py` — removed dead `r = _req.get(...)` in TTS doctor check
  5. `memory.py` — removed unused `result = qc.delete(...)`
  6. `telegram_bot.py` — removed dead `loading_notified` flag + `thinking_text` buffer
  7. `server.py` — removed unused `import math` in camera scan

- **68 auto-fixed** (unused imports, comma-split imports — safe cosmetic).
- **Ignore rules** documented in `pyproject.toml` with justification (long prompt strings, E402 for circular-dep deferrals, f-strings as markers).

CI now runs `ruff check .` before the syntax check + import smoke + pytest.

## 🔌 B. Lazy imports hoisted

v0.17.7 (`subprocess` UnboundLocalError) and v0.17.23 (rag.py f-string SyntaxError) both hit because imports lived inside function bodies and didn't trigger on startup. Hoisting them to module top catches these at import time.

- **`tools.py`**: 35 local imports → 3 kept lazy (cv2, request_camera_frame_sync, tasks — all circular or heavy). 32 hoisted. Removed duplicate aliased re-imports (`_time`, `_b64`, `_uuid`, `_re`).
- **`server.py`**: 107 local imports → 20 kept lazy (cv2, pypdf, av, cryptography — all heavy optional deps, documented with `# lazy: ...` comments). 87 hoisted.
- **Shadowing risks flagged**: `knowledge_url` had `import tasks as t` alongside `[t.strip() for t in tags_raw]`. Comprehensions have their own scope so it didn't collide, but the aliasing was a tripwire. Rewrote to use `tasks.` directly.

Agent.py + agent_loop.py deferred to Phase B (TurnContext refactor touches them heavily).

## 🧪 C. Test `sys.modules` pollution eliminated

**Problem from v0.17.22**: 5 legacy test files (`test_config`, `test_experience`, `test_presets`, `test_reliability`, `test_server_presets`) mutated `sys.modules` at import time. pytest collects everything up front → mocks leaked → 68 cross-file failures. Workaround was running each file in its own pytest process.

**Fix**: refactored all 5 files to use pytest fixtures + `monkeypatch` (auto-reverts). Added `tests/conftest.py` with shared `qwe_temp_data_dir` + `mock_llm` fixtures. Zero `sys.modules[X] = ...` assignments remain.

**CI reverted**: single `pytest tests/ -v` again. Faster and saner.

Result: **158 tests pass in one process in 4.8 seconds** (was: file-by-file loop with ~15 s overhead).

## 📦 D. Dependabot enabled

`.github/dependabot.yml` added:
- Weekly pip scan of `pyproject.toml`, minor+patch grouped into one PR per week.
- Monthly github-actions scan.
- Majors ignored for `fastapi`, `openai`, `qdrant-client`, `pydantic` (need hands-on review).
- Security updates ALWAYS get their own PR (dependabot default).

## ✅ Result

```
ruff check .           — 0 errors
pytest tests/          — 158 passed in 4.8s
import smoke           — all modules load cleanly
Python 3.11 AST check  — 0 findings
```

CI now has 4 guardrails before pytest: **ruff → 3.11 syntax → import smoke → pytest**. The class of bugs that slipped through earlier this week (UnboundLocalError, 3.12-only f-strings, unused-import NameError) can no longer ship.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

No behavior change. Doctor output unchanged. Just a harder-to-break codebase.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
