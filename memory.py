"""Qdrant-backed semantic memory — search & store."""

import atexit, uuid, time
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter,
    FieldCondition, MatchValue,
)
import config
import logger

_log = logger.get("memory")

_qclient: QdrantClient | None = None
_embed_client: OpenAI | None = None


def _close_qdrant():
    global _qclient
    if _qclient:
        try:
            _qclient.close()
        except Exception:
            pass
        _qclient = None

atexit.register(_close_qdrant)


def _get_qdrant() -> QdrantClient:
    global _qclient
    if _qclient is None:
        if config.QDRANT_MODE == "memory":
            _qclient = QdrantClient(":memory:")
        elif config.QDRANT_MODE == "disk":
            _qclient = QdrantClient(path=config.QDRANT_PATH)
        else:
            _qclient = QdrantClient(url=config.QDRANT_URL)
        # Ensure collection exists
        cols = [c.name for c in _qclient.get_collections().collections]
        if config.QDRANT_COLLECTION not in cols:
            _qclient.create_collection(
                config.QDRANT_COLLECTION,
                vectors_config=VectorParams(
                    size=config.EMBED_DIM, distance=Distance.COSINE
                ),
            )
    return _qclient


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


def _search_impl(vector: list[float], limit: int,
                 tag: str | None, thread_id: str | None) -> list[dict]:
    """Core search using pre-computed vector."""
    try:
        qc = _get_qdrant()
    except Exception as e:
        _log.warning(f"qdrant unavailable for search: {e}")
        return []

    conditions = []
    if tag:
        conditions.append(FieldCondition(key="tag", match=MatchValue(value=tag)))
    if thread_id:
        conditions.append(FieldCondition(key="thread_id", match=MatchValue(value=thread_id)))
    filt = Filter(must=conditions) if conditions else None

    results = qc.query_points(
        config.QDRANT_COLLECTION,
        query=vector,
        limit=limit,
        query_filter=filt,
    )
    out = []
    for r in results.points:
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


def search(query: str, limit: int = config.MAX_MEMORY_RESULTS,
           tag: str | None = None, thread_id: str | None = None) -> list[dict]:
    """Semantic search over memories with optional tag/thread filters.

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
    return _search_impl(vector, limit, tag, thread_id)


def search_by_vector(vector: list[float], limit: int = config.MAX_MEMORY_RESULTS,
                     tag: str | None = None, thread_id: str | None = None) -> list[dict]:
    """Search using a pre-computed embedding vector (avoids redundant API calls)."""
    return _search_impl(vector, limit, tag, thread_id)


def save(text: str, tag: str = "general", dedup: bool = True,
         thread_id: str | None = None, meta: dict | None = None) -> str:
    """Save a memory with optional thread context and metadata.
    
    Args:
        text: memory content
        tag: category (general, user, compaction, project, etc.)
        dedup: if True, update existing memory if >0.9 similarity
        thread_id: associate with a specific thread/topic
        meta: extra metadata dict (source, topic_name, etc.)
    """
    qc = _get_qdrant()
    vector = _embed(text)

    payload = {
        "text": text,
        "tag": tag,
        "ts": time.time(),
    }
    if thread_id:
        payload["thread_id"] = thread_id
    if meta:
        payload.update(meta)

    # Deduplicate: if very similar memory exists (same tag), update it
    if dedup:
        try:
            dedup_filter = Filter(must=[
                FieldCondition(key="tag", match=MatchValue(value=tag))
            ])
            results = qc.query_points(
                config.QDRANT_COLLECTION, query=vector, limit=1,
                query_filter=dedup_filter,
            )
            if results.points and results.points[0].score > 0.9:
                existing = results.points[0]
                qc.upsert(
                    config.QDRANT_COLLECTION,
                    points=[PointStruct(
                        id=existing.id, vector=vector, payload=payload,
                    )],
                )
                return str(existing.id)
        except Exception as e:
            _log.debug(f"dedup check failed, saving as new: {e}")

    point_id = str(uuid.uuid4())
    qc.upsert(
        config.QDRANT_COLLECTION,
        points=[PointStruct(id=point_id, vector=vector, payload=payload)],
    )
    return point_id


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
                    FieldCondition(key="ts", range={"lt": cutoff}),
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
