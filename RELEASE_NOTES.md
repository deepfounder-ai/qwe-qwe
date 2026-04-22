# v0.17.19 — UX polish: toast flood, graph leak, Enter-to-submit, memory-meta skips

Five targeted UX fixes. All changes localized, no public API shifts.

## Fixes

### 1. Toast flood prevention (static/index.html)

`toast()` used to append a fresh `<div>` per call. Fifty rapid calls = fifty overlapping toasts piling down the screen. Now capped at **5 concurrent** (oldest dropped via a `state._toasts` queue) with **500ms dedup** (same `text+kind` within the window just refreshes the timestamp instead of stacking).

**Before:**

```js
const toast = (text, kind) => {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; ... }, 2500);
  setTimeout(() => el.remove(), 2900);
};
```

**After:** tracks a `state._toasts` queue, dedups within 500ms, caps at 5 concurrent, still auto-removes at 2900ms.

### 2. Graph handler leak (static/index.html)

Every render of the knowledge-graph panel re-attached `mousemove` / `mouseup` / `mouseleave` listeners to `document` — and never removed them. After ten renders you'd have ten live `mousemove` handlers each carrying a stale `panning` closure, firing on every mouse tick.

**Before:**

```js
let panning = null;
graphSvg.addEventListener('mousedown', ...);
const endPan = () => { panning = null; ... };
document.addEventListener('mousemove', (ev) => { if (!panning) return; ... });
document.addEventListener('mouseup', endPan);
document.addEventListener('mouseleave', endPan);
```

**After:** document-level listeners attach **once** behind a `state._graphGlobalHandlersAttached` flag and look up the active pan state through `state._graphPan` (which also stores the current `svg` ref so it survives re-renders). Node-level `mousedown` handlers were already self-cleaning (onMove/onUp remove themselves on mouseup) — no change there; the SVG itself is gc'd on re-render along with its listeners.

### 3. Provider key modal — Enter-to-submit (static/index.html)

`openProviderKeyModal` had no keyboard submit. You had to mouse the "Save + switch" button. Now `Enter` on either `#pk-url` or `#pk-key` triggers the primary action — mirrors the existing pattern in `openLoginModal`.

### 4. Dead-code cleanup in Inspector (static/index.html)

```js
// line 3234 — removed
const toolCount = (lt && state.messages.find(m => m.id === 'stream-' + (state.lastTurnId || '')))?.tools?.length || 0;
```

`state.lastTurnId` is never assigned anywhere in the codebase, so the lookup always returned `undefined` and `toolCount` was permanently `0`. The variable wasn't displayed in the output HTML either. **Chose removal over wiring up**: wiring `lastTurnId` would mean threading a new piece of state through the WS message handlers with no visible consumer for it — pure dead code is simpler to delete than to resurrect a feature no one asked for.

### 5. `_save_experience` keyword/meta coverage (agent.py)

The experience-learning skip list missed common Russian inflections and a few self-config tools, so meta-turns like "забыл пароль" or `soul_editor`-only edits were still being saved as "experiences" and polluting recall.

**Added to `_MEMORY_KEYWORDS`:**
`забыл, забыла, забудьте, забываешь, запомнил, запомните, вспомни, вспомнил`

**Added to `_META_TOOLS`:**
`soul_editor, skill_creator, add_trait, remove_trait, list_traits, rag_index, user_profile_get`

(`list_notes` and `get_stats` were already present; not duplicated.)

## Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

No config changes needed. Web UI picks up all four front-end fixes on next reload.
