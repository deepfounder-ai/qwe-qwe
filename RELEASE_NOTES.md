# v0.17.6 — Soul save + ground-truth core tools list

Two fixes on the v2 web UI.

## 🐛 Fixes

### Soul settings silently wiped all traits (and UI didn't refresh)

Changing one trait in **Settings → Soul** used to POST:

```js
/api/soul  { traits: { humor: "high" } }
```

The server iterates `data.items()` and calls `soul.save(key, value)` for each top-level key — so it received `save("traits", {humor: "high"})`, which returns `"Unknown trait: traits. Use add_trait()…"` and saves nothing. HTTP 200, silent failure.

On top of that, `renderTabSoul()` was reading trait values from `state.soul.traits` (the *descriptions* object), so even a successful save wouldn't show up in the UI without a page reload.

Now:

- Trait save sends the flat payload the server expects: `{ [name]: value }`.
- Render reads `state.soulFull.values` for values and `state.soulFull.traits` for descriptions — matching the actual `/api/soul` response shape.
- Local state (`state.soulFull.values[name]`) is updated immediately after save so the selector reflects the new value without a refresh.

### `tools.TOOLS` vs. the UI drifted — inspector showed a fake list

The right-hand Inspector and **Settings → Tools** used to hardcode their own 7-item "core tools" list — which didn't match the real 26 tools in `tools.TOOLS` (so e.g. `browser_open` appeared when the actual core has no browser tool; browser lives in the skill).

Now:

- `GET /api/status` returns a `core_tools` field: `sorted(t["function"]["name"] for t in tools.TOOLS)` — ground truth from the runtime registry.
- Inspector and Tools tab render from that list with a local description map.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
