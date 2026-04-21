# v0.17.7 — `shell` tool broken on every call

Emergency hotfix. Every `shell` invocation returned:

```
Error: cannot access local variable 'subprocess' where it is not associated with a value
```

## 🐛 Root cause

A Python scoping footgun inside `tools.execute()`:

```python
elif name == "open_url":
    ...
    import subprocess, sys   # ← makes subprocess LOCAL to the whole function
    ...

elif name == "shell":
    ...
    result = subprocess.run(...)   # UnboundLocalError — local is unassigned
```

Python decides a name is local at **compile time**: any `import subprocess` anywhere in a function's body turns `subprocess` into a local for the entire function. The module-level `import subprocess` at the top of `tools.py` was shadowed. When the `shell` branch ran, the local `subprocess` was referenced before the `open_url` branch had a chance to assign it (which it never does in a shell call).

Introduced in v0.15.0 when `open_url` landed. Silent for months because the `UnboundLocalError` was caught upstream and rendered as just "Error: …" — users saw a one-liner and assumed a normal shell failure.

## 🔧 Fix

Removed the redundant local imports in `open_url` (and a matching `import shutil` in `send_file`). Both modules are already imported at the top of `tools.py`.

## ✅ Verified

```python
>>> tools.execute('shell', {'command': 'echo hello'})
'hello'
```

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
