"""Qdrant-backed semantic memory — hybrid search (dense + sparse), recommendations, grouping."""

import atexit, re, uuid, time, zlib
from collections import Counter
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter,
    FieldCondition, MatchValue, Range, PayloadSchemaType,
    SparseVectorParams, SparseVector, NamedVector, NamedSparseVector,
    Fusion, FusionQuery, Prefetch, Datatype, TextIndexParams,
    TokenizerType, RecommendInput, RecommendQuery,
)
import config
import logger

_log = logger.get("memory")

_qclient: QdrantClient | None = None
_embed_client: OpenAI | None = None

# Schema version — bump to force migration
_SCHEMA_VERSION = 2  # v1: unnamed vector, v2: named dense+sparse, float16, full-text


def _close_qdrant():
    global _qclient
    if _qclient:
        try:
            _qclient.close()
        except Exception:
            pass
        _qclient = None

atexit.register(_close_qdrant)


# ── Sparse vector (BM25-like via word hashing) ──

def sparse_embed(text: str) -> SparseVector:
    """Generate sparse vector using word-frequency hashing (BM25-like).

    Maps each unique token to a deterministic hash-based index (crc32,
    stable across Python restarts), with TF saturation scoring.
    Aggregates colliding indices via max to avoid Qdrant duplicate-index errors.
    No external model needed — runs instantly on CPU.
    """
    tokens = re.findall(r'\w{2,}', text.lower())
    if not tokens:
        return SparseVector(indices=[0], values=[1.0])
    freq = Counter(tokens)
    index_values: dict[int, float] = {}  # aggregate by index to handle collisions
    for token, count in freq.items():
        idx = abs(zlib.crc32(token.encode())) % 100_000  # deterministic, stable across restarts
        tf = count / (count + 1.0)  # TF saturation: diminishing returns
        if idx in index_values:
            index_values[idx] = max(index_values[idx], tf)
        else:
            index_values[idx] = tf
    return SparseVector(
        indices=list(index_values.keys()),
        values=list(index_values.values()),
    )


# Keep private alias for backwards compat (tests mock it)
_sparse_embed = sparse_embed


# ── Qdrant client + collection management ──

def _get_qdrant() -> QdrantClient:
    global _qclient
    if _qclient is None:
        if config.QDRANT_MODE == "memory":
            _qclient = QdrantClient(":memory:")
        elif config.QDRANT_MODE == "disk":
            _qclient = QdrantClient(path=config.QDRANT_PATH)
        else:
            _qclient = QdrantClient(url=config.QDRANT_URL)
        _ensure_collection(_qclient, config.QDRANT_COLLECTION)
    return _qclient


def _ensure_collection(qc: QdrantClient, collection: str):
    """Ensure collection exists with v2 schema. Migrate from v1 if needed."""
    cols = [c.name for c in qc.get_collections().collections]

    # Check for interrupted migration — resume from temp if found
    temp_name = f"{collection}_v2_migration"
    if temp_name in cols:
        _log.info("found interrupted migration, resuming from temp collection")
        _resume_migration(qc, collection, temp_name)
        return

    if collection not in cols:
        _create_collection_v2(qc, collection)
        return

    # Check if existing collection needs migration (v1 → v2)
    info = qc.get_collection(collection)
    vectors_cfg = info.config.params.vectors
    # v1 has unnamed vector (VectorParams directly), v2 has dict with "dense"
    if isinstance(vectors_cfg, dict) and "dense" in vectors_cfg:
        # Already v2, just ensure indexes
        _ensure_payload_indexes(qc, collection)
        return

    # Migration: v1 → v2
    _log.info("migrating memory collection v1 → v2 (named vectors + sparse)")
    _migrate_v1_to_v2(qc, collection)


def _create_collection_v2(qc: QdrantClient, collection: str):
    """Create collection with v2 schema: named dense (float16) + sparse vectors."""
    qc.create_collection(
        collection,
        vectors_config={
            "dense": VectorParams(
                size=config.EMBED_DIM,
                distance=Distance.COSINE,
                datatype=Datatype.FLOAT16,
            ),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )
    _ensure_payload_indexes(qc, collection)
    _log.info(f"created collection '{collection}' v2 (dense float16 + sparse + indexes)")


def _ensure_payload_indexes(qc: QdrantClient, collection: str):
    """Create payload indexes if they don't exist yet."""
    try:
        info = qc.get_collection(collection)
        existing = set(info.payload_schema.keys()) if info.payload_schema else set()
        # Keyword + float indexes
        indexes = {
            "tag": PayloadSchemaType.KEYWORD,
            "thread_id": PayloadSchemaType.KEYWORD,
            "ts": PayloadSchemaType.FLOAT,
        }
        for field, schema_type in indexes.items():
            if field not in existing:
                qc.create_payload_index(collection, field, schema_type)
                _log.info(f"created payload index: {field} ({schema_type})")
        # Full-text index on "text" field for keyword search
        if "text" not in existing:
            try:
                qc.create_payload_index(
                    collection, "text",
                    TextIndexParams(
                        type="text",
                        tokenizer=TokenizerType.WORD,
                        min_token_len=2,
                        lowercase=True,
                    ),
                )
                _log.info("created full-text index on 'text'")
            except Exception as e:
                _log.debug(f"full-text index creation skipped: {e}")
    except Exception as e:
        _log.debug(f"payload index creation skipped: {e}")


def _migrate_v1_to_v2(qc: QdrantClient, collection: str):
    """Migrate from v1 (unnamed vector) to v2 (named dense + sparse).

    Uses a temp collection to avoid data loss on crash:
    1. Read all points from v1
    2. Create temp collection with v2 schema
    3. Populate temp collection
    4. Delete v1 collection
    5. Create v2 collection and copy from temp
    6. Delete temp
    """
    temp_name = f"{collection}_v2_migration"

    # Read all existing points
    all_points = []
    offset = None
    while True:
        result = qc.scroll(collection, limit=100, offset=offset, with_vectors=True)
        batch, offset = result
        all_points.extend(batch)
        if offset is None:
            break

    _log.info(f"read {len(all_points)} points from v1 collection")

    # Build new points with named vectors
    new_points = []
    for p in all_points:
        text = p.payload.get("text", "") if p.payload else ""
        sparse = sparse_embed(text) if text else SparseVector(indices=[0], values=[1.0])
        new_points.append(PointStruct(
            id=p.id,
            vector={"dense": p.vector, "sparse": sparse},
            payload=p.payload or {},
        ))

    # Create temp collection with v2 schema and populate it
    # (if crash here, v1 data is still intact)
    cols = [c.name for c in qc.get_collections().collections]
    if temp_name in cols:
        qc.delete_collection(temp_name)
    _create_collection_v2(qc, temp_name)

    if new_points:
        for i in range(0, len(new_points), 100):
            qc.upsert(temp_name, points=new_points[i:i + 100])

    # Swap: delete v1, recreate as v2, write from memory (not re-read from temp)
    qc.delete_collection(collection)
    _create_collection_v2(qc, collection)

    if new_points:
        for i in range(0, len(new_points), 100):
            qc.upsert(collection, points=new_points[i:i + 100])

    # Cleanup temp
    qc.delete_collection(temp_name)
    _log.info(f"migrated {len(new_points)} points to v2")


def _resume_migration(qc: QdrantClient, collection: str, temp_name: str):
    """Resume an interrupted v1→v2 migration from temp collection."""
    # Read all points from temp
    all_points = []
    offset = None
    while True:
        result = qc.scroll(temp_name, limit=100, offset=offset, with_vectors=True)
        batch, offset = result
        all_points.extend(batch)
        if offset is None:
            break

    # Delete old collection if it exists, create fresh v2
    cols = [c.name for c in qc.get_collections().collections]
    if collection in cols:
        qc.delete_collection(collection)
    _create_collection_v2(qc, collection)

    # Copy points from temp → final
    if all_points:
        for i in range(0, len(all_points), 100):
            batch = all_points[i:i + 100]
            pts = [PointStruct(
                id=p.id,
                vector={"dense": p.vector["dense"], "sparse": p.vector["sparse"]},
                payload=p.payload or {},
            ) for p in batch]
            qc.upsert(collection, points=pts)

    qc.delete_collection(temp_name)
    _log.info(f"resumed migration: {len(all_points)} points recovered")


# ── Embedding ──

def _get_embed() -> OpenAI:
    global _embed_client
    if _embed_client is None:
        _embed_client = OpenAI(
            base_url=config.EMBED_BASE_URL,
            api_key=config.EMBED_API_KEY,
        )
    return _embed_client


def _embed(text: str) -> list[float]:
    # Sanitize surrogates that can appear in WSL terminals
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    try:
        resp = _get_embed().embeddings.create(input=text, model=config.EMBED_MODEL)
        return resp.data[0].embedding
    except Exception as e:
        _log.warning(f"embedding failed: {e}")
        raise


def embed(text: str) -> list[float]:
    """Public wrapper — compute embedding vector for text.

    Use this to cache the vector and pass it to search_by_vector()
    to avoid redundant embedding API calls.
    """
    return _embed(text)


# ── Search ──

def _build_filter(tag: str | None = None,
                  thread_id: str | None = None) -> Filter | None:
    """Build Qdrant filter from optional tag/thread_id."""
    conditions = []
    if tag:
        conditions.append(FieldCondition(key="tag", match=MatchValue(value=tag)))
    if thread_id:
        conditions.append(FieldCondition(key="thread_id", match=MatchValue(value=thread_id)))
    return Filter(must=conditions) if conditions else None


def _points_to_dicts(points) -> list[dict]:
    """Convert Qdrant scored points to dicts."""
    out = []
    for r in points:
        item = {
            "id": r.id,
            "text": r.payload.get("text", ""),
            "tag": r.payload.get("tag", ""),
            "thread_id": r.payload.get("thread_id", ""),
            "score": round(r.score, 3),
            "ts": r.payload.get("ts", 0),
        }
        # Include extra metadata (outcome_score, etc.)
        for k, v in r.payload.items():
            if k not in item:
                item[k] = v
        out.append(item)
    return out


def _search_hybrid(qc, vector: list[float], text: str, limit: int,
                   filt: Filter | None) -> list[dict]:
    """Hybrid search: dense + sparse prefetch → RRF fusion."""
    sparse = _sparse_embed(text)
    try:
        results = qc.query_points(
            config.QDRANT_COLLECTION,
            prefetch=[
                Prefetch(query=sparse, using="sparse", limit=limit * 4,
                         filter=filt),
                Prefetch(query=vector, using="dense", limit=limit * 4,
                         filter=filt),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=limit,
        )
        return _points_to_dicts(results.points)
    except Exception as e:
        _log.debug(f"hybrid search failed, falling back to dense-only: {e}")
        return _search_dense_only(qc, vector, limit, filt)


def _search_dense_only(qc, vector: list[float], limit: int,
                       filt: Filter | None) -> list[dict]:
    """Fallback: dense-only search (for v1 collections or errors)."""
    try:
        results = qc.query_points(
            config.QDRANT_COLLECTION,
            query=vector,
            using="dense",
            limit=limit,
            query_filter=filt,
        )
    except Exception:
        # Last resort: unnamed vector query (v1 compat)
        results = qc.query_points(
            config.QDRANT_COLLECTION,
            query=vector,
            limit=limit,
            query_filter=filt,
        )
    return _points_to_dicts(results.points)


def _search_impl(vector: list[float], limit: int,
                 tag: str | None, thread_id: str | None,
                 query_text: str | None = None) -> list[dict]:
    """Core search — uses hybrid (dense+sparse RRF) when query_text available."""
    try:
        qc = _get_qdrant()
    except Exception as e:
        _log.warning(f"qdrant unavailable for search: {e}")
        return []

    filt = _build_filter(tag, thread_id)

    # Hybrid search if we have the original query text
    if query_text:
        return _search_hybrid(qc, vector, query_text, limit, filt)
    else:
        return _search_dense_only(qc, vector, limit, filt)


def search(query: str, limit: int = config.MAX_MEMORY_RESULTS,
           tag: str | None = None, thread_id: str | None = None) -> list[dict]:
    """Hybrid semantic + keyword search over memories.

    Uses dense (embedding) + sparse (BM25-like) vectors with RRF fusion
    for best recall on both semantic and exact keyword matches.

    Args:
        query: search text
        limit: max results
        tag: filter by tag
        thread_id: filter by thread (or None for global search)

    Returns [{text, tag, thread_id, score, ts, ...}]
    """
    try:
        vector = _embed(query)
    except Exception as e:
        _log.warning(f"embedding unavailable for search: {e}")
        return []
    return _search_impl(vector, limit, tag, thread_id, query_text=query)


def search_by_vector(vector: list[float], limit: int = config.MAX_MEMORY_RESULTS,
                     tag: str | None = None, thread_id: str | None = None,
                     query_text: str | None = None) -> list[dict]:
    """Search using a pre-computed embedding vector.

    If query_text is provided, uses hybrid search (dense + sparse RRF).
    Otherwise falls back to dense-only search.
    """
    return _search_impl(vector, limit, tag, thread_id, query_text=query_text)


# ── Recommend (for Memento experience learning) ──

def recommend(positive_ids: list[str], negative_ids: list[str] | None = None,
              limit: int = 5, tag: str | None = None) -> list[dict]:
    """Recommend similar memories based on positive/negative examples.

    Uses Qdrant's Recommend API with BEST_SCORE strategy —
    ideal for Memento experience learning:
    "find experiences similar to these successes, unlike these failures"

    Args:
        positive_ids: point IDs of good examples
        negative_ids: point IDs of bad examples to avoid
        limit: max results
        tag: optional tag filter

    Returns [{text, tag, score, ts, ...}]
    """
    try:
        qc = _get_qdrant()
    except Exception as e:
        _log.warning(f"qdrant unavailable for recommend: {e}")
        return []

    filt = _build_filter(tag=tag)

    try:
        results = qc.query_points(
            config.QDRANT_COLLECTION,
            query=RecommendQuery(recommend=RecommendInput(
                positive=positive_ids,
                negative=negative_ids or [],
                strategy="best_score",
            )),
            using="dense",
            query_filter=filt,
            limit=limit,
        )
        return _points_to_dicts(results.points)
    except Exception as e:
        _log.warning(f"recommend failed: {e}")
        return []


# ── Search with grouping (dedup by thread) ──

def search_grouped(query: str, limit: int = config.MAX_MEMORY_RESULTS,
                   tag: str | None = None, group_size: int = 1) -> list[dict]:
    """Search with grouping by thread_id — returns max group_size results per thread.

    Prevents search results from being dominated by memories from one conversation.

    Args:
        query: search text
        limit: number of groups to return
        tag: optional tag filter
        group_size: max results per thread group

    Returns [{text, tag, thread_id, score, ts, ...}]
    """
    try:
        vector = _embed(query)
    except Exception:
        return []

    try:
        qc = _get_qdrant()
    except Exception:
        return []

    filt = _build_filter(tag=tag)

    try:
        results = qc.query_points_groups(
            config.QDRANT_COLLECTION,
            query=vector,
            using="dense",
            group_by="thread_id",
            limit=limit,
            group_size=group_size,
            query_filter=filt,
        )
        all_hits = [hit for group in results.groups for hit in group.hits]
        return _points_to_dicts(all_hits)
    except Exception as e:
        _log.debug(f"grouped search failed, falling back to regular: {e}")
        return search(query, limit=limit, tag=tag)


# ── Save ──

def save(text: str, tag: str = "general", dedup: bool = True,
         thread_id: str | None = None, meta: dict | None = None) -> str:
    """Save a memory with both dense and sparse vectors.

    Args:
        text: memory content
        tag: category (general, user, compaction, project, etc.)
        dedup: if True, update existing memory if >0.9 similarity
        thread_id: associate with a specific thread/topic
        meta: extra metadata dict (source, topic_name, etc.)
    """
    qc = _get_qdrant()
    try:
        dense_vector = _embed(text)
    except Exception as e:
        _log.warning(f"embedding failed in save(): {e}")
        raise
    sparse_vector = sparse_embed(text)

    payload = {
        "text": text,
        "tag": tag,
        "ts": time.time(),
    }
    if thread_id:
        payload["thread_id"] = thread_id
    if meta:
        payload.update(meta)

    vectors = {"dense": dense_vector, "sparse": sparse_vector}

    # Deduplicate: if very similar memory exists (same tag), update it
    if dedup:
        try:
            dedup_filter = Filter(must=[
                FieldCondition(key="tag", match=MatchValue(value=tag))
            ])
            results = qc.query_points(
                config.QDRANT_COLLECTION, query=dense_vector,
                using="dense", limit=1,
                query_filter=dedup_filter,
            )
            if results.points and results.points[0].score > 0.9:
                existing = results.points[0]
                qc.upsert(
                    config.QDRANT_COLLECTION,
                    points=[PointStruct(
                        id=existing.id, vector=vectors, payload=payload,
                    )],
                )
                return str(existing.id)
        except Exception as e:
            _log.debug(f"dedup check failed, saving as new: {e}")

    point_id = str(uuid.uuid4())
    qc.upsert(
        config.QDRANT_COLLECTION,
        points=[PointStruct(id=point_id, vector=vectors, payload=payload)],
    )
    return point_id


# ── Delete / Cleanup ──

def delete(point_id: str) -> bool:
    """Delete a single memory by its point ID."""
    try:
        qc = _get_qdrant()
        qc.delete(config.QDRANT_COLLECTION, points_selector=[point_id])
        return True
    except Exception:
        return False


def cleanup(max_age_days: int = 7, tag: str = "session"):
    """Remove old memories by tag. Returns count deleted."""
    qc = _get_qdrant()
    cutoff = time.time() - (max_age_days * 86400)
    try:
        from qdrant_client.models import FilterSelector
        qc.delete(
            config.QDRANT_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="tag", match=MatchValue(value=tag)),
                    FieldCondition(key="ts", range=Range(lt=cutoff)),
                ])
            ),
        )
    except Exception:
        pass


def count() -> int:
    """Count total memories."""
    try:
        qc = _get_qdrant()
        info = qc.get_collection(config.QDRANT_COLLECTION)
        return info.points_count or 0
    except Exception:
        return 0
