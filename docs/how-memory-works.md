# How Memory Works in qwe-qwe

## Overview

qwe-qwe uses a three-layer knowledge system that combines fast vector search with a knowledge graph. All data lives in a single Qdrant vector database collection, differentiated by tags.

The core insight: **raw data is cheap to store but expensive to search well**. By synthesizing raw facts into structured entities and wiki summaries, we get dramatically better search quality with minimal runtime cost.

## The Three Layers

```
USER INPUT
    |
    v
+-------------------+     +-----------------+     +------------------+
| Layer 1: RAW      |     | Layer 2: ENTITY |     | Layer 3: WIKI    |
| tag = knowledge   | --> | tag = entity    | --> | tag = wiki       |
| tag = fact        |     |                 |     |                  |
|                   |     | Nodes in the    |     | Synthesized      |
| Original chunks,  |     | knowledge graph |     | summaries with   |
| facts, decisions  |     | with typed      |     | better embeddings|
|                   |     | relations       |     | than raw chunks  |
| Created: DAY      |     | Created: NIGHT  |     | Created: NIGHT   |
| Cost: 0 LLM calls|     | Cost: 1 LLM/grp|     | Cost: included   |
+-------------------+     +-----------------+     +------------------+
```

### Layer 1: Raw Data (immediate)

When the agent saves something via `memory_save`, it goes directly to Qdrant:

- **Short facts** (< 1000 chars): stored as-is with `synthesis_status = "skip"`
- **Long texts** (> 1000 chars): auto-chunked into ~800 char pieces with 100 char overlap
  - Each chunk gets `synthesis_status = "pending"`
  - Chunks are grouped by `synthesis_group` for batch processing

Chunking splits on sentence boundaries (`. `, `\n`, `! `, `? `) to preserve meaning.

**Example:**
```
memory_save("Kir likes Python", tag="fact")
  -> 1 point, synthesis_status="skip"

memory_save(article_text_5000_chars, tag="knowledge", source="fastapi_docs")
  -> 6 chunks, synthesis_status="pending", synthesis_group="fastapi_docs_1712345678"
```

### Layer 2: Entities (night synthesis)

A cron task runs at 03:00 (configurable) and processes all pending chunks:

1. Groups chunks by `synthesis_group`
2. Sends combined text to LLM for entity extraction
3. LLM returns structured JSON:
```json
{
  "entities": [
    {"name": "FastAPI", "type": "technology", "description": "Python web framework"},
    {"name": "Pydantic", "type": "technology", "description": "Data validation library"}
  ],
  "relations": [
    {"from": "FastAPI", "to": "Pydantic", "rel": "uses"},
    {"from": "FastAPI", "to": "Starlette", "rel": "built_on"}
  ],
  "summary": "FastAPI is a modern Python web framework..."
}
```
4. Creates/updates entity nodes in Qdrant with `tag = "entity"`
5. Each entity stores its relations in payload:
```json
{
  "tag": "entity",
  "text": "FastAPI",
  "entity_type": "technology",
  "description": "Modern Python web framework for building APIs",
  "relations": [
    {"to": "Pydantic", "rel": "uses"},
    {"to": "Starlette", "rel": "built_on"},
    {"to": "Python", "rel": "language"}
  ],
  "observation_count": 5,
  "last_synthesized": 1712345678
}
```

### Layer 3: Wiki (night synthesis)

During the same synthesis run, wiki summaries are created:

- Stored in Qdrant as `tag = "wiki"` (searchable via vector search)
- Also written to disk as markdown (`~/.qwe-qwe/wiki/fastapi.md`)

Wiki chunks have **better embeddings** than raw chunks because the LLM has already distilled the key information into clean, focused text.

## Search: How Three Layers Work Together

When the agent searches memory (or auto-context injects memories into a conversation), all three layers are queried simultaneously:

```
Query: "how does validation work in my API?"

Search results (ranked by relevance):
  0.92  [wiki]      "FastAPI uses Pydantic for automatic validation..."  <- BEST: synthesized
  0.87  [knowledge] "from pydantic import BaseModel; class Item..."      <- specific code
  0.85  [entity]    "Pydantic" -> relations: [{to: FastAPI, rel: uses}]  <- graph context
  0.71  [fact]      "Kir prefers FastAPI over Flask"                     <- personal fact
```

The auto-context injection prioritizes in this order:
1. **Wiki chunks** — synthesized knowledge, highest quality
2. **Entity relations** — graph navigation, structural context
3. **Thread memories** — local conversation context
4. **Global memories** — all facts and knowledge
5. **Experience** — past task outcomes

## Vector Architecture

All vectors use the same schema in one Qdrant collection (`qwe_qwe`):

| Component | Model | Dimensions | Purpose |
|-----------|-------|------------|---------|
| Dense | FastEmbed multilingual-MiniLM-L12-v2 | 384 (float16) | Semantic similarity |
| Sparse | SPLADE++ (prithivida/Splade_PP_en_v1) | Dynamic | Keyword matching |
| Fusion | Reciprocal Rank Fusion (RRF) | - | Merge dense + sparse + BM25 |

**Why hybrid search?**
- Dense vectors catch semantic meaning ("validation" matches "data checking")
- Sparse vectors catch specific terms ("Pydantic" matches exactly)
- BM25 (SQLite FTS5) catches rare words and exact phrases
- RRF fusion combines all three rankings into one

## Knowledge Graph Visualization

The Web UI has a Graph tab (Knowledge > Graph) that shows entities and their relations as an interactive force-directed graph:

- **Nodes** = entities, sized by observation count
- **Edges** = relations, labeled with type (uses, built_on, etc.)
- **Colors** by entity type: technology (orange), person (blue), project (green), concept (purple)
- **Interactive**: drag nodes, hover for details

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `synthesis_enabled` | 1 | Enable night synthesis (0=off) |
| `synthesis_time` | 03:00 | When to run synthesis (HH:MM) |
| `synthesis_max_per_run` | 50 | Max items per synthesis run |

Change via Web UI Settings or `self_config(action="set", key="synthesis_time", value="04:00")`.

## File Locations

```
~/.qwe-qwe/
  memory/           Qdrant data (vectors, payloads)
  wiki/             Synthesized markdown pages
    index.md        Master index of all entities
    log.md          Chronological synthesis log
    fastapi.md      Wiki page for FastAPI entity
    python.md       Wiki page for Python entity
  qwe_qwe.db       SQLite (FTS5 for BM25, settings, history)
```

## Token Budget

The knowledge graph is designed for small models with limited context:

| Phase | Token Cost | When |
|-------|-----------|------|
| Save (day) | 0 extra | Immediate, no LLM |
| Synthesis (night) | ~500-1000 per group | Background, user doesn't wait |
| Search (query) | +200-500 (wiki in context) | Per turn, but much higher quality |

**Net result**: slightly more context used per turn, but dramatically better answers because the context is pre-synthesized knowledge instead of raw chunks.

## Comparison with Traditional RAG

| | Traditional RAG | qwe-qwe Knowledge Graph |
|---|---|---|
| **Storage** | Chunks only | Chunks + entities + wiki |
| **Search** | Vector similarity | Hybrid (dense + sparse + BM25 + graph) |
| **Context quality** | Raw chunks (noisy) | Synthesized summaries (clean) |
| **Relations** | None | Typed entity relations |
| **Cost per query** | Same | Same (synthesis is offline) |
| **Visualization** | None | Interactive graph |
| **Compound knowledge** | No | Yes (entities grow over time) |
