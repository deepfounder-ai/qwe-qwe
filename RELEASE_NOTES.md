# v0.17.5 — Secrets list not updating

Quick patch: the Secrets sub-tab didn't refresh after a secret was saved, so users saw a "saved" toast but the list stayed empty.

## 🐛 Fix

- **`state.secrets` was being assigned a function** — the loader had:
  ```js
  state.secrets = r.keys || r || [];
  ```
  `/api/secrets` returns a bare array of keys. `r.keys` on an array resolves to `Array.prototype.keys` (a truthy method reference), so the fallback chain leaked the method into state instead of the array. `state.secrets.map(...)` then failed silently.

  Now uses an explicit guard: `Array.isArray(r) ? r : (r.secrets || r.list || [])`.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
