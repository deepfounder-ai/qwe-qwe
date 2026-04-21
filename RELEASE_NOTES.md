# v0.17.3 — Advanced Settings rendering fix

Quick patch addressing a UI regression in **Settings → Advanced → Settings**.

## 🐛 Fixes

- **All 30+ tunables rendered as `[object Object]`** — `/api/settings` returns each key as `{value, default, description, type, min, max}` but the UI was reading the nested object as a flat value. Every input / toggle in the Advanced Settings tab now:
  - Pulls the real value from `.value`
  - Uses `.type` (`int` / `float` / `str` / `bool`) to pick the right input element + cast on save
  - Shows `.description` as the row sub-text (was empty before)
  - Applies `.min` / `.max` / `step` attributes for numeric fields
  - Password inputs for `*_key` / `*_token` / `api_key` fields

- **TTS reference audio / transcript fields** on the Voice tab had the same bug — fixed in the same pass.

- **Secret fields saving** — inputs that used to send `"[object Object]"` as the value back to `/api/settings` now send the correct typed value. API keys persist properly.

- **Toggle semantics** — boolean settings now flip `true`/`false` (proper `bool` type) instead of `0`/`1` int-casting, matching what `config.set` expects.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
