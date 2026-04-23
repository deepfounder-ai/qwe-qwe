# v0.17.28 — auto-recall actually filters by relevance now

Bugfix release. One behavioral fix in the memory pipeline, two UI cleanups in the inspector, plus +20 tests backfilling coverage for surfaces that shipped untested.

## 🧠 Auto-recall: dense-only with a real semantic threshold

**Symptom**: asking the agent a programming question would inject memories about evolution / travel notes / anything-in-the-same-language into the system prompt. Nonsense relevance, wasted context.

**Root cause**: `_auto_context()` in `agent.py` was using the hybrid (dense + sparse SPLADE++ + BM25 → Qdrant RRF fusion) path with a `score_threshold=0.45`. Qdrant's RRF-fused scores are **rank-normalized** — the top result is always ≈1.0 regardless of absolute relevance. A 0.45 threshold on those scores meant "top 55% by rank", not "≥0.45 semantically similar". Garbage sailed through.

**Fix** (`agent.py:_auto_context`):

- Switched every auto-recall search to **dense-only** by dropping `query_text=` from the `memory.search_by_vector` calls (the function routes to hybrid iff `query_text` is passed). Dense-only returns raw cosine similarity on the 0..1 range.
- Raised `MEMORY_SCORE_MIN` 0.45 → **0.6** (the cosine bar where semantic closeness starts to mean something) and `EXPERIENCE_SCORE_MIN` 0.5 → **0.65**.
- Unified the entity-lookup threshold with `MEMORY_SCORE_MIN` (was hardcoded 0.5).
- Long comment in the source explaining why mixing RRF scores with absolute thresholds is a trap — so the next person doesn't "fix" it by switching back to hybrid.

**Hybrid RRF is unchanged** for the explicit `memory_search` tool, where the agent explicitly wants keyword + semantic and isn't trying to apply an absolute threshold.

Verified live: programming question → empty recall. Question about evolution → evolution memories, high cosine. Exactly what you want.

## 🎨 Inspector cleanup

- **Recalled memories**: removed the knowledge-base "preview" mode. When the thread has no live recall yet, the section now shows a clean empty state ("Send a message — the agent's live recall will stream here") instead of hitting `/api/knowledge/search` on the last user message and displaying unrelated KB hits with a "preview" badge. The preview was confusing users — making them think the agent had recalled things it hadn't. Real recalls still stream in via the WS `recall` event with the green **live** badge.
- **Context window header**: removed the duplicate `X%` tag from the section header. The same percentage was already rendered by the `.pct` span inside the gauge right below.

## 🧪 +20 tests — backfill for UI↔server contract + freshly-fixed bugs

Every one of these guards a surface that recently shipped without coverage.

- `tests/test_endpoint_consistency.py` (3) — walks every `/api/...` call site in `static/index.html` and asserts a matching FastAPI route is mounted. Segment-wise matcher handles both literal fills (`/api/kv/spicy_duck ↔ /api/kv/{key}`) and concat prefixes (`/api/threads/' + id + '/switch`). Fault-injected: flipping one path to `/api/threds` fails the test as intended.
- `tests/test_providers_list.py` (11) — locks in `list_all()` shape, **parallel ping behavior** (timing-based guard: serial 2×0.7s = 1.4s would trip the 1.2s ceiling, parallel ~0.75s passes — fault-injection confirmed), ping cache hit + invalidation, embedding-model block in `set_model`, and `switch()` guards (unknown / keyless cloud / keyless local).
- `tests/test_ws_attachments.py` (6) — end-to-end WebSocket `document` + `image_b64` round-trip: bytes land in `UPLOADS_DIR`, `[File attached: …]` reference injected into `user_input`, filename sanitizer blocks `../../etc/passwd\x00.txt`, image + document coexist in one turn.

Suite: 186 → **206 passing** (23s local, single pass).

## Upgrade

```bash
pip install --upgrade qwe-qwe        # or re-run ./setup.sh
```

No migrations. No config changes. Ship it.
