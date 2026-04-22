# v0.17.22 — CI green: 158/158 tests pass

`ci-monitor` fired on an old already-merged PR (#1 — the notification was delayed by 4 days). When I checked, the CI was red on *main* too — all recent releases (0.17.17–0.17.21) had failing test runs I hadn't noticed. Root cause: 68 failures from the same two problems. Fixing them.

## 🔍 Root cause 1: `sys.modules` pollution in `tests/test_tools.py`

`test_tools.py` injected mock `config`, `db`, `memory`, `logger` into `sys.modules` at **import time**:

```python
# old test_tools.py — BAD
sys.modules["memory"] = mock_memory
sys.modules["config"] = mock_config
...
import tools  # picks up the mocks
```

pytest collects every test file before running any test, so those mocks leaked to sibling files. Any subsequent `from memory import _scrub_secrets` in `test_secret_scrub.py` got the mock (which didn't have that name) → ImportError → 30+ cascading failures in `test_shell_safety`, `test_secret_scrub`, etc.

**Fix**: rewritten `test_tools.py` to use the REAL modules — project is `pip install -e .` editable so imports resolve fine. Tests now exercise `tools._check_shell_safety` directly as a pure function, no mocking needed.

## 🔍 Root cause 2: CI runs `pytest tests/` (one big process)

Even after rewriting `test_tools.py`, the OTHER legacy test files (`test_config`, `test_experience`, `test_presets`, `test_reliability`, `test_server_presets`) still pollute `sys.modules` at import time with their own mocks. Each of them works fine in isolation but crashes their siblings when collected together.

**Fix**: `.github/workflows/test.yml` now loops over `tests/test_*.py`, runs each in its own `pytest` process. Each file gets a fresh Python and fresh `sys.modules` — no cross-file pollution possible.

```yaml
- name: Run tests
  run: |
    fail=0
    for t in tests/test_*.py; do
      echo "::group::$t"
      pytest "$t" -v || fail=1
      echo "::endgroup::"
    done
    exit $fail
```

## 🔧 Additional fixes surfaced by re-running tests

### `tests/test_experience.py` — 5 tests updated for v0.17.12 filter semantics

v0.17.12 added filters to `_save_experience()` that skip trivial single-round turns (reply < 80 chars) and memory-topic user inputs. Tests written before that filter used `rounds=1` + short replies → now skipped.

Updated inputs to be substantive (rounds=2) and swapped `"запомни это"` + `tools=["memory_save"]` in test_1_8 for `"напиши конфиг в config.yml"` + `tools=["write_file"]` (test was checking save path mechanics, not the memory-meta keyword).

### `agent._repair_json` — two real bugs fixed

```python
>>> _repair_json('{"items": [1, 2, 3}')
# before: {}  (wrong — added `]` at end, still invalid)
# after:  {"items": [1, 2, 3]}  ✓

>>> _repair_json('{"command": "ls -la')
# before: {}  (wrong — closed brace before string, landed inside)
# after:  {"command": "ls -la"}  ✓
```

**Bug 1**: string was closed AFTER brackets — so `{"command": "ls -la` → `{"command": "ls -la}"` (curly inside string). Now closes string first, brackets second.

**Bug 2**: bracket repair used append-at-end counting — so `{"items": [1, 2, 3}` counted `{`=1 `}`=1 `[`=1 `]`=0 → added `]` at end → `{"items": [1, 2, 3}]` (still invalid). Now uses a **positional scan-and-insert**: when a premature `}` is seen with a pending `[`, it inserts the `]` BEFORE the `}`.

## 📊 Result

```
tests/test_config.py          4 passed
tests/test_experience.py     20 passed
tests/test_json_repair.py    16 passed
tests/test_presets.py        39 passed
tests/test_reliability.py     6 passed
tests/test_secret_scrub.py   11 passed
tests/test_server_presets.py 13 passed
tests/test_shell_safety.py   39 passed
tests/test_tools.py          10 passed
                            ─────────
                            158 passed
```

**Before this release**: 86 passed, 68 failed.
**Now**: 158 passed, 0 failed.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

No user-visible behavior changes — just CI health. `_repair_json` is slightly smarter now, so a malformed tool call that a small model emits with a missing bracket or unterminated string is more likely to be recovered instead of being dropped.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
