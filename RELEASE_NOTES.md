# v0.17.4 — Preset routes fix

Quick patch addressing a route-ordering bug in `server.py`.

## 🐛 Fix

- **`GET /api/presets/onboarding` → 404** and **`POST /api/presets/deactivate` → 405** — these literal-path routes were declared *after* the parameterised catch-all `GET /api/presets/{preset_id}`, so FastAPI swallowed requests to `/api/presets/onboarding` into the `{preset_id}` handler which returned 404 (no preset with id `"onboarding"` exists). Similarly for `deactivate`.

  Fixed by reordering: literal routes now declared **before** `{preset_id}`, matching FastAPI's declaration-order resolution.

  Visible symptoms this fixes:
  - Boot-time console noise: `GET /api/presets/onboarding 404`
  - Preset deactivation via UI silently failing
  - `POST /api/secrets` 405 reports — if the user sees these, it's from a stale server binary; restart the process after `git pull` and the 405 vanishes (the endpoint has existed since v0.17.0).

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Then restart the server (Ctrl+C then relaunch qwe-qwe --web)
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
