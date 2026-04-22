# v0.17.9 — Recalled memories panel now shows the real recall

Before: the Inspector's "Recalled memories" panel claimed to show what the agent remembered to answer you. In practice it ran a **separate** query against the RAG knowledge base (uploaded files) using the last user message as a string — a speculative preview, not what the agent actually saw. The WebSocket handler had a `recall` event wired, but no code on the server ever emitted it.

Now the panel shows the exact items the agent's `_build_context()` injected into the system prompt this turn.

## 🔧 What changed

### Server side

- `agent._recall_callback` + `_emit_recall(list)` helper — same pattern as `_content_callback` / `_thinking_callback`.
- `agent._auto_context()` rewritten to collect a structured list alongside the text lines it builds for the prompt. Items carry `{tag, text, score, source}` where `source` ∈ `thread / wiki / entity / cross_thread / experience`. Emitted via `_emit_recall()` right before the text is returned, so the UI sees the same slice the model is about to see.
- Dedup is shared between text and structured list — one can't drift from the other.
- `server.py` WS handler wires `_queue_recall` that pushes `{type: "recall", memories: [...]}` onto the stream queue.

### UI side

- WS `recall` handler now normalizes `score → relevance` + preserves `source` + sets `state.memoryPillsReal = true` so the panel knows the data is authoritative.
- `fetchRecalledMemories()` (the knowledge-base fallback) only runs when `memoryPillsReal` is false or the user explicitly clicks refresh. No more clobbering live data with a speculative KB search.
- Panel badge:
  - **`live`** (green) when the data came from the agent's actual recall.
  - **`preview`** (muted) when it's a knowledge-base preview from the fallback.
  - Tooltip on both explains what it means.
- Each memory pill shows the source (`thread` / `wiki` / `entity` / `cross_thread` / `experience`) so you can see *where* the agent found it.
- Empty state text rewritten to explain what the panel actually does.
- `memoryPillsReal` is reset on thread switch / new chat so a stale "live" badge doesn't carry over.

## What the sources mean

| Source | Where it comes from | Scope |
|---|---|---|
| `thread` | Qdrant hybrid search scoped to **this** thread | Highest priority — local context |
| `wiki` | Synthesized markdown pages from night synthesis | High quality — cross-thread |
| `entity` | Graph nodes with typed relations (`rel→target`) | Structured knowledge |
| `cross_thread` | Synthesized tags (`fact`, `knowledge`, `user`, `project`, `decision`, `idea`) | Never raw messages from other threads |
| `experience` | Proven task patterns (success-weighted) | Only when `experience_learning` is on |

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

Send a message — the panel should tag **live** and show items as the agent's `_build_context()` picked them. Hover any pill to see the source.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
