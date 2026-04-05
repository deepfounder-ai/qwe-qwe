# Knowledge Graph + Night Synthesis — Design Doc (v0.9.0)

## Concept

Inspired by Karpathy's LLM Wiki pattern. Raw data collected during the day, LLM synthesizes at night. All three layers (raw chunks, entities, wiki) stored in a single Qdrant collection for unified vector search.

## Architecture

```
DAY (fast, no LLM):
  memory_save("article text...")
    -> chunk into ~800 char pieces
    -> store in Qdrant: tag="knowledge", synthesis_status="pending"

  memory_save("Kir likes Python")
    -> short fact, no chunking
    -> store in Qdrant: tag="fact", synthesis_status="skip"

NIGHT (cron 03:00, LLM processes queue):
  For each pending group:
    1. LLM extracts entities + relations
    2. Create/update entity nodes (tag="entity") in Qdrant
    3. Link raw chunks to entities via payload
    4. Generate wiki text -> store as wiki chunks (tag="wiki") in Qdrant
    5. Also write wiki/fastapi.md to disk (human-readable backup)
    6. Mark originals: synthesis_status="done"

SEARCH (three-layer):
  "how does API validation work?"
    -> Vector search across ALL tags in one query:
      1. wiki chunk: "FastAPI uses Pydantic for automatic validation..." (tag=wiki, pre-synthesized, best match)
      2. raw chunk: "Pydantic BaseModel validates request body..." (tag=knowledge, original)
      3. entity: "Pydantic" (tag=entity, relations -> FastAPI, Python)
    -> Result: synthesized + raw + graph context in one search
```

## Three Layers, One Collection

All data lives in a single Qdrant collection (`qwe_qwe`), differentiated by `tag`:

| Layer | tag | Created | Purpose |
|-------|-----|---------|---------|
| **Raw** | `knowledge` | Day (immediate) | Original chunks from articles, code, conversations |
| **Facts** | `fact`, `user`, `project`, `task`, `decision` | Day (immediate) | Short facts, as today |
| **Entities** | `entity` | Night (synthesis) | Graph nodes with relations |
| **Wiki** | `wiki` | Night (synthesis) | Synthesized knowledge, best for search |
| **Experience** | `experience` | After tool tasks | Past task outcomes |

### Why one collection?

- Single vector search finds results across all layers
- Wiki chunks rank higher because they're synthesized (cleaner text = better embedding)
- No extra infrastructure (no Neo4j, no second collection)
- Payload filters narrow by tag when needed

## Qdrant Payload Schemas

### Raw chunk (tag="knowledge")
```json
{
  "tag": "knowledge",
  "text": "Pydantic BaseModel validates request body and returns 422...",
  "source": "article_fastapi",
  "source_type": "article|code|conversation|file",
  "chunk_index": 2,
  "chunk_total": 6,
  "synthesis_status": "pending|done",
  "synthesis_group": "article_fastapi_20260405",
  "entities": ["Pydantic", "FastAPI"],
  "ts": 1712345678
}
```

### Entity node (tag="entity")
```json
{
  "tag": "entity",
  "text": "FastAPI",
  "entity_type": "technology|person|project|concept|place|event",
  "description": "Modern Python web framework for building APIs with automatic docs",
  "relations": [
    {"to": "Starlette", "rel": "built_on", "weight": 0.9},
    {"to": "Pydantic", "rel": "uses", "weight": 0.8},
    {"to": "Python", "rel": "language", "weight": 1.0},
    {"to": "qwe-qwe", "rel": "used_in", "weight": 0.7}
  ],
  "observation_count": 15,
  "wiki_ref": "wiki/fastapi.md",
  "last_synthesized": 1712345678,
  "ts": 1712345678
}
```

### Wiki chunk (tag="wiki")
```json
{
  "tag": "wiki",
  "text": "FastAPI is a modern Python web framework built on Starlette and Pydantic. It provides automatic request validation, OpenAPI documentation, and async support. Used in the qwe-qwe project for the web server.",
  "wiki_page": "fastapi",
  "wiki_section": "summary|key_facts|relations",
  "synthesis_sources": ["article_fastapi_20260405", "conv_20260403"],
  "entities": ["FastAPI", "Starlette", "Pydantic", "Python"],
  "ts": 1712345678
}
```

### Short fact (tag="fact", unchanged from current)
```json
{
  "tag": "fact",
  "text": "Kir prefers FastAPI over Flask",
  "synthesis_status": "skip",
  "ts": 1712345678
}
```

## Search: How Three Layers Work Together

### Query: "how does validation work in my API?"

**Step 1: Vector search** (single query, all tags)
```
Results ranked by cosine similarity:
  0.92  [wiki]      "FastAPI uses Pydantic for automatic validation of request/response models..."
  0.87  [knowledge]  "from pydantic import BaseModel; class Item(BaseModel): name: str..."
  0.85  [entity]     "Pydantic" → relations: [{to:"FastAPI", rel:"used_by"}]
  0.71  [fact]        "Kir prefers FastAPI over Flask"
```

**Step 2: Relation expansion** (optional, for entities in results)
```
Entity "Pydantic" has relation to "FastAPI"
  -> pull FastAPI entity -> wiki_ref: "wiki/fastapi.md"
  -> inject 1-2 related wiki chunks into context
```

**Step 3: Context assembly**
```
Auto-context for LLM:
  [wiki] FastAPI uses Pydantic for automatic validation...  (synthesized, high quality)
  [knowledge] from pydantic import BaseModel...              (raw code, specific)
  [fact] Kir prefers FastAPI over Flask                      (user preference)
```

The wiki layer acts as a **pre-computed summary** — it answers the question directly. Raw chunks provide **specifics**. Entities provide **navigation** to related topics.

## Wiki on Disk (Human-Readable Backup)

Wiki pages also written to `~/.qwe-qwe/wiki/` as markdown:

```
wiki/
  index.md        — master index of all entities and pages
  fastapi.md      — synthesized knowledge about FastAPI
  python.md       — synthesized knowledge about Python
  kir.md          — enriched user profile
  qwe-qwe.md     — project knowledge
  log.md          — chronological synthesis log
```

These files serve as:
- Human-readable knowledge base (open in any editor)
- Backup (if Qdrant is reset, wiki can be re-indexed)
- Debug (see what the synthesis produced)

## Night Synthesis Pipeline

```
Cron: daily 03:00 (or configurable)

1. COLLECT
   Find all points with synthesis_status="pending"
   Group by synthesis_group (source + date)

2. EXTRACT (per group)
   LLM prompt: "Extract entities, relations, and key facts from these chunks:"
   -> entities: [{name, type, description}]
   -> relations: [{from, to, rel, weight}]
   -> summary: "2-3 paragraph synthesis"

3. ENTITIES
   For each entity:
     - If exists in Qdrant (tag="entity", text=name): update relations, bump observation_count
     - If new: create entity point with embedding

4. WIKI
   For each entity with changes:
     - Gather all related chunks + existing wiki
     - LLM: "Update this wiki page with new information:"
     - Store wiki chunks in Qdrant (tag="wiki")
     - Write markdown to disk (wiki/{name}.md)

5. LINK
   Update original chunks: synthesis_status="done", entities=[...]

6. LOG
   Append to wiki/log.md: "2026-04-05 03:00 — Processed 12 chunks,
   created 3 entities, updated 2 wiki pages"
```

## Implementation Plan

### Phase 1: Pending Queue + Auto-Chunking
- Add `synthesis_status` to memory.save() payload
- Auto-chunk long texts (>1000 chars) into ~800 char pieces
- Short facts saved with status="skip"
- New payload index: `synthesis_status` (keyword)
- Files: memory.py, tools.py

### Phase 2: Night Synthesis Worker
- Synthesis cron task (configurable time, default 03:00)
- LLM entity extraction prompt
- Entity CRUD in Qdrant (create/update/merge)
- Wiki chunk generation + disk backup
- Files: synthesis.py (new), scheduler.py, config.py

### Phase 3: Enriched Search
- memory_search: results from all three layers
- Relation expansion: follow entity links
- Auto-context uses wiki chunks (higher quality context)
- Files: memory.py, agent.py, soul.py

### Phase 4: Graph Visualization (optional)
- Web UI: interactive knowledge graph (D3.js force layout)
- Nodes = entities (sized by observation_count)
- Edges = relations (colored by type)
- Click node -> wiki page + related memories
- Files: static/index.html, server.py (API endpoint)

## Token Budget Impact

| Phase | Day Cost | Night Cost | Search Benefit |
|-------|----------|------------|----------------|
| Phase 1 | 0 extra tokens | 0 (no synthesis yet) | Same as today |
| Phase 2 | 0 extra tokens | ~500-1000 tok per group | Wiki available |
| Phase 3 | 0 extra tokens | Same | +200-500 tok (wiki in context) but MUCH higher quality |

## Relation Types

Standard relation vocabulary:

| Relation | Meaning | Example |
|----------|---------|---------|
| `uses` | A uses B | FastAPI uses Pydantic |
| `built_on` | A is built on B | FastAPI built_on Starlette |
| `language` | A is written in B | qwe-qwe language Python |
| `part_of` | A belongs to B | server.py part_of qwe-qwe |
| `works_on` | person works on project | Kir works_on qwe-qwe |
| `prefers` | person prefers X | Kir prefers FastAPI |
| `related` | generic relation | async related concurrency |
| `depends_on` | A depends on B | memory.py depends_on Qdrant |
| `alternative` | A is alternative to B | Flask alternative FastAPI |
| `instance_of` | A is instance of B | qwe-qwe instance_of AI_agent |

## Config Settings

```python
EDITABLE_SETTINGS += {
    "synthesis_time": ("setting:synthesis_time", str, "03:00", "Night synthesis time (HH:MM)"),
    "synthesis_enabled": ("setting:synthesis_enabled", int, 1, "Enable night synthesis (0=off, 1=on)"),
    "synthesis_chunk_size": ("setting:synthesis_chunk_size", int, 800, "Chunk size for long texts"),
    "synthesis_max_per_run": ("setting:synthesis_max_per_run", int, 50, "Max items per synthesis run"),
}
```

## Files to Create/Modify

| File | Change |
|------|--------|
| memory.py | synthesis_status in payload, auto-chunking, relation expansion in search |
| tools.py | memory_save handles long text, wiki search integration |
| synthesis.py | **NEW** — night synthesis worker (entity extraction, wiki generation) |
| scheduler.py | Register synthesis cron task |
| config.py | Add synthesis settings |
| soul.py | Mention wiki in system prompt, use wiki for auto-context |
| agent.py | Enhanced auto_context with wiki chunks |
| server.py | API endpoint for graph data (Phase 4) |
| static/index.html | Graph visualization (Phase 4) |
| ~/.qwe-qwe/wiki/ | Generated markdown pages |
