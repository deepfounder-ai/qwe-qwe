"""Qdrant-backed semantic memory — search & store."""

import uuid, time
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter,
    FieldCondition, MatchValue,
)
import config

_qclient: QdrantClient | None = None
_embed_client: OpenAI | None = None


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
    resp = _get_embed().embeddings.create(input=text, model=config.EMBED_MODEL)
    return resp.data[0].embedding


def search(query: str, limit: int = config.MAX_MEMORY_RESULTS,
           tag: str | None = None) -> list[dict]:
    """Semantic search over memories. Returns [{text, tag, score, ts}]."""
    qc = _get_qdrant()
    filt = None
    if tag:
        filt = Filter(must=[FieldCondition(key="tag", match=MatchValue(value=tag))])
    results = qc.query_points(
        config.QDRANT_COLLECTION,
        query=_embed(query),
        limit=limit,
        query_filter=filt,
    )
    return [
        {
            "id": r.id,
            "text": r.payload.get("text", ""),
            "tag": r.payload.get("tag", ""),
            "score": round(r.score, 3),
            "ts": r.payload.get("ts", 0),
        }
        for r in results.points
    ]


def save(text: str, tag: str = "general", dedup: bool = True) -> str:
    """Save a memory. Deduplicates by default (updates if similar exists)."""
    qc = _get_qdrant()
    vector = _embed(text)

    # Deduplicate: if very similar memory exists, update it
    if dedup:
        try:
            results = qc.query_points(
                config.QDRANT_COLLECTION, query=vector, limit=1,
            )
            if results.points and results.points[0].score > 0.9:
                # Update existing point
                existing = results.points[0]
                qc.upsert(
                    config.QDRANT_COLLECTION,
                    points=[PointStruct(
                        id=existing.id, vector=vector,
                        payload={"text": text, "tag": tag, "ts": time.time()},
                    )],
                )
                return str(existing.id)
        except Exception:
            pass

    point_id = str(uuid.uuid4())
    qc.upsert(
        config.QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=point_id,
                vector=vector,
                payload={"text": text, "tag": tag, "ts": time.time()},
            )
        ],
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
