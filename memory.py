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
            "text": r.payload.get("text", ""),
            "tag": r.payload.get("tag", ""),
            "score": round(r.score, 3),
            "ts": r.payload.get("ts", 0),
        }
        for r in results.points
    ]


def save(text: str, tag: str = "general") -> str:
    """Save a memory. Returns the point id."""
    qc = _get_qdrant()
    point_id = str(uuid.uuid4())
    qc.upsert(
        config.QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=point_id,
                vector=_embed(text),
                payload={"text": text, "tag": tag, "ts": time.time()},
            )
        ],
    )
    return point_id
