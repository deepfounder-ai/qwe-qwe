# v0.17.23 — Python 3.11 runtime SyntaxError in rag.py

User installed v0.17.21 on Python 3.11, doctor green, but hit this as soon as a request hit `/api/knowledge/list`:

```
File "C:\AI\qwe-qwe\rag.py", line 610
    )
    ^
SyntaxError: f-string expression part cannot include a backslash
```

## 🐛 Root cause

Line 608 of `rag.py` (in `_fetch_youtube_transcript`, added v0.17.13):

```python
f"{('## Description\n\n' + description + '\n\n') if description else ''}"
```

The `\n` escapes live **inside** the f-string expression braces. PEP 701 relaxed this restriction in Python 3.12+, but on 3.11 the parser rejects it at import time.

## 🔧 Fix

Extract the optional section into a precomputed variable so the f-string expression stays backslash-free:

```python
desc_section = f"## Description\n\n{description}\n\n" if description else ""
md = (
    ...
    f"{desc_section}"
    ...
)
```

## 🚨 Why CI didn't catch this earlier

CI runs `pytest tests/`. None of the tests import `rag.py` at module load (it's imported inside `server.knowledge_url()` and `server.knowledge_list()` on-demand). So `pytest` collection never triggered the compile and 3.11 CI stayed green with a broken runtime module. Classic lazy-import blind spot.

## 🛡️ Guardrails added

Two new CI steps in `.github/workflows/test.yml` that run BEFORE `pytest`:

### 1. `ast.parse(..., feature_version=(3, 11))` on every `.py`

Forces Python's own parser to reject post-3.11 grammar — catches PEP 701 leakage (f-string backslash, quote reuse, multi-line expressions) across the whole repo regardless of whether anything imports the module.

```yaml
- name: Syntax check against Python 3.11 grammar
  run: |
    python - <<'PY'
    import ast, pathlib, sys
    errors = []
    for glob in ("*.py", "tests/*.py", "skills/*.py"):
        for p in pathlib.Path(".").glob(glob):
            try:
                ast.parse(p.read_text(encoding="utf-8"),
                          filename=str(p), feature_version=(3, 11))
            except SyntaxError as e:
                errors.append(f"{p}:{e.lineno}: {e.msg}")
    if errors:
        print("\n".join(errors)); sys.exit(1)
    PY
```

### 2. Import-time smoke

Imports every runtime module + constructs the FastAPI app — surfaces any syntax error (or ImportError from a missing dep, or a module-level side effect that crashes in CI) that `pytest` wouldn't touch.

```yaml
- name: Import-time smoke
  run: |
    python -c "
    import agent, agent_loop, tools, server, memory, rag, providers
    import scheduler, tasks, soul, threads, vault, mcp_client, presets
    import config, cli, db, discovery, synthesis, telegram_bot, stt, tts
    import updater, utils, logger, inference_setup
    from server import app
    print('all modules import cleanly')
    "
```

Both run on both 3.11 and 3.12 matrix legs. Anything similar in the future fails CI loudly instead of shipping as a latent bug.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you're on 3.11 and saw the doctor green + server crash pattern, this is the fix.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
