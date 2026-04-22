# v0.17.11 — Knowledge Graph is actually explorable

Before: the graph used a fixed 3-ring radial layout (7 + 12 + 24 slots). Once you pushed past ~30 entities everything overlapped into a labels-on-labels blob. No zoom, no pan, no way to untangle it. Screenshot showed 89 nodes stacked on top of each other.

Now the graph is a proper interactive canvas.

## 🔧 What changed

### Force-directed layout

Replaced radial with a physics simulation that runs once on load:

- **Repulsion** — every pair of nodes pushes each other apart (O(n²), fine to ~500 nodes).
- **Spring attraction** — edges pull connected nodes to a rest length. Length scales with average degree so dense graphs don't squish.
- **Gravity** — pulls everything softly toward centre so isolated subgraphs don't drift off screen.
- **Cooling** — later iterations make smaller adjustments, so the graph settles.
- Parameters auto-scale with node count. Tested: **38ms for 89 nodes / 167 edges**.

### Pan / zoom / drag

- **Scroll wheel** — zoom in/out anchored at the cursor.
- **Drag empty space** — pan.
- **Drag a node** — move it (and its attached edges update in real-time).
- **Click a node** — pick it (inspector section below shows details).
- **Double-click empty space** — reset view to default.

Zoom clamped to 0.25×–8×.

### Toolbar

New buttons in the panel header:

| Button | What it does |
|---|---|
| `−` | Zoom out |
| `+` | Zoom in |
| 👁 | Fit all nodes to the viewport |
| ↻ | Reset view (pan=0, zoom=1) |
| ⎊ | Re-run layout (shuffles positions, re-settles) |
| 🗑 | Clear graph (unchanged — still there) |

### Implementation notes

- SVG viewBox is `0 0 200 200` (was `0 0 100 100`) — 2× the coordinate space so nodes have room to breathe.
- All graph content lives in `<g id="graph-root" transform="translate(tx ty) scale(zoom)">` so pan/zoom is a single attribute update — no re-render.
- Node drag also updates DOM directly (circle + text + connected lines) instead of re-rendering the whole SVG, so dragging stays smooth at 60fps.
- `.graph-canvas` got `overflow: hidden` + `user-select: none` + cursor states.
- Panel height bumped from 520 → 640 px.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

Open **Memory → Knowledge graph**: scroll to zoom, drag nodes around, click **fit** (eye icon) to auto-frame them all.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
