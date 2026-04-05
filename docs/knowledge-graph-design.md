# Knowledge Graph + Night Synthesis — Design Doc (v0.9.0)

## Concept

Inspired by Karpathy's LLM Wiki pattern. Raw data collected during the day, LLM synthesizes at night.

## Architecture

```
DAY (fast, no LLM):
  memory_save("article text...")
    → chunk into ~800 char pieces
    → store in Qdrant with synthesis_status="pending"
    → add to synthesis queue

NIGHT (cron 03:00, LLM processes queue):
  For each pending item:
    1. LLM extracts entities + relations
    2. Create/update entity nodes in Qdrant
    3. Link chunks to entities via payload
    4. Build/update wiki page (markdown in workspace/wiki/)
    5. Set synthesis_status="done", synthesis_ref="wiki/fastapi.md"

SEARCH (enriched):
  "what do I know about FastAPI?"
    → Vector search finds entity "FastAPI"
    → payload.synthesis_ref → read wiki/fastapi.md (pre-synthesized)
    → Also return fresh pending chunks (not yet synthesized)
    → Result: synthesized knowledge + raw recent data
```

## Qdrant Payload Schema

```json
{
  "tag": "knowledge|entity|fact|experience",
  "text": "original text",
  "source": "article_name|user_input|file_path",
  "synthesis_status": "pending|processing|done|skip",
  "synthesis_ref": "wiki/fastapi.md",
  "synthesis_ts": 1712345678,
  "entities": ["FastAPI", "Python", "Starlette"],
  "relations": [
    {"to": "Starlette", "rel": "built_on", "weight": 0.9},
    {"to": "Pydantic", "rel": "uses", "weight": 0.8}
  ],
  "chunk_index": 0,
  "chunk_total": 6,
  "entity_type": null
}
```

Entity nodes (tag="entity"):
```json
{
  "tag": "entity",
  "text": "FastAPI",
  "entity_type": "technology|person|project|concept",
  "description": "Modern Python web framework for building APIs",
  "synthesis_ref": "wiki/fastapi.md",
  "relations": [
    {"to": "Starlette", "rel": "built_on"},
    {"to": "Pydantic", "rel": "uses"},
    {"to": "Python", "rel": "language"}
  ],
  "observation_count": 15,
  "last_updated": 1712345678
}
```

## Wiki Pages (workspace/wiki/)

```markdown
# FastAPI

**Type:** Technology (Python web framework)
**Relations:** built on Starlette, uses Pydantic, written in Python

## Summary
FastAPI is a modern, fast web framework for building APIs with Python 3.7+.
Based on Starlette for async and Pydantic for data validation.

## Key Facts
- Used by Kir in qwe-qwe project (server.py)
- Supports WebSocket, middleware, dependency injection
- Auto-generates OpenAPI docs

## Sources
- article_fastapi (6 chunks, synthesized 2026-04-05)
- user conversation 2026-04-03 (2 facts)

## Related
- [[Python]] — language
- [[Starlette]] — foundation
- [[Pydantic]] — validation
```

## Implementation Plan

### Phase 1: Pending Queue (minimal)
- Add `synthesis_status` to memory.save() payload
- Long texts auto-chunked (>1000 chars) with status="pending"
- Short facts saved with status="skip" (no synthesis needed)
- New payload index in Qdrant: `synthesis_status` (keyword)

### Phase 2: Night Synthesis Cron
- New cron task: `synthesis_worker` runs at 03:00
- Finds all pending items, groups by source
- For each group: LLM extracts entities + relations + summary
- Creates/updates entity nodes
- Writes wiki markdown to workspace/wiki/
- Updates payload: status="done", ref=wiki path

### Phase 3: Enriched Search
- memory_search enhanced: if entity found, follow relations
- Return: direct matches + wiki summary + related entities
- Auto-context injection uses wiki pages for richer context

### Phase 4: Graph Visualization (optional)
- Web UI: interactive graph view (D3.js or similar)
- Nodes = entities, edges = relations
- Click node → see wiki page + related memories

## Token Budget Impact

- Day: zero extra tokens (save only)
- Night: ~500-1000 tokens per synthesis (LLM call)
- Search: +200-500 tokens (wiki summary injected)
- Net: slightly more context but much higher quality

## Files to Create/Modify

| File | Change |
|------|--------|
| memory.py | Add synthesis_status to save(), chunking for long text |
| tools.py | Update memory_save to auto-chunk + pending status |
| scheduler.py | Add synthesis_worker cron task |
| soul.py | Reference wiki in search results |
| agent.py | Enhanced auto_context with wiki pages |
| workspace/wiki/ | Generated markdown pages |
| static/index.html | (Phase 4) Graph visualization |
