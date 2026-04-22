"""Qdrant-backed semantic memory — hybrid search (dense + sparse via FastEmbed), recommendations, grouping."""

import atexit, re, uuid, time
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter,
    FieldCondition, MatchValue, Range, PayloadSchemaType,
    SparseVectorParams, SparseVector,
    Fusion, FusionQuery, Prefetch, Datatype, TextIndexParams,
    TokenizerType, RecommendInput, RecommendQuery,
)
import config
import logger

_log = logger.get("memory")

_qclient: QdrantClient | None = None
_dense_model = None
_sparse_model = None


# ── Secret scrubbing ──
# Ordered: most-specific prefixes first (sk-ant- before sk-, github_pat_ before ghp_)
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{30,}"), "anthropic_key"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "github_pat"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "github_token"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "groq_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "slack_token"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "jwt"),
    # Generic sk- last so it doesn't eat the more specific sk-ant- above
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "openai_key"),
]

# Dotenv-style KEY=value lines (e.g. `OPENAI_API_KEY=foo`). Multiline-capable.
_ENV_LINE_RE = re.compile(
    r"^([A-Z_][A-Z0-9_]{2,}_(?:KEY|TOKEN|SECRET|PASSWORD|PASS))(\s*=\s*)(.+)$",
    re.MULTILINE,
)


def _scrub_secrets(text: str) -> tuple[str, bool]:
    """Strip common secret patterns from text.

    Returns (scrubbed_text, was_scrubbed). When a match is found it is replaced
    with ``[REDACTED:<type>]`` (or ``[REDACTED]`` for env-style lines, which keep
    the variable name so the redaction is traceable).
    """
    if not text:
        return text, False
    scrubbed = text
    hit = False

    for pat, label in _SECRET_PATTERNS:
        new_text, n = pat.subn(f"[REDACTED:{label}]", scrubbed)
        if n:
            hit = True
            scrubbed = new_text

    def _env_sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}[REDACTED]"

    new_text, n = _ENV_LINE_RE.subn(_env_sub, scrubbed)
    if n:
        hit = True
        scrubbed = new_text

    return scrubbed, hit

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


# ── FastEmbed models (lazy-loaded, ONNX-based, no server needed) ──

DENSE_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384d, 50+ languages
SPARSE_MODEL_NAME = "prithivida/Splade_PP_en_v1"  # learned sparse (SPLADE++)
EMBED_DIM = 384  # multilingual-MiniLM output dimension


def _get_dense_model():
    global _dense_model
    if _dense_model is None:
        import warnings
        from fastembed import TextEmbedding
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            _dense_model = TextEmbedding(model_name=DENSE_MODEL_NAME)
        _log.info(f"loaded dense model: {DENSE_MODEL_NAME}")
    return _dense_model


def _get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding
        _sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)
        _log.info(f"loaded sparse model: {SPARSE_MODEL_NAME}")
    return _sparse_model


def sparse_embed(text: str) -> SparseVector:
    """Generate SPLADE++ sparse vector via FastEmbed (learned sparse, not BM25 hash)."""
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    try:
        model = _get_sparse_model()
        result = list(model.embed([text]))[0]
        return SparseVector(
            indices=result.indices.tolist(),
            values=result.values.tolist(),
        )
    except Exception as e:
        _log.warning(f"sparse embedding failed: {e}")
        return SparseVector(indices=[0], values=[1.0])


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
                size=EMBED_DIM,
                distance=Distance.COSINE,
                datatype=Datatype.FLOAT16,
            ),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(
                modifier="idf",  # IDF weighting: rare words score higher
            ),
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
            "synthesis_status": PayloadSchemaType.KEYWORD,
            "synthesis_group": PayloadSchemaType.KEYWORD,
            "file_path": PayloadSchemaType.KEYWORD,
            "source_type": PayloadSchemaType.KEYWORD,
            "document_tags": PayloadSchemaType.KEYWORD,
        }
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
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

def _embed(text: str) -> list[float]:
    """Generate dense embedding via FastEmbed (ONNX, no server)."""
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    try:
        model = _get_dense_model()
        result = list(model.embed([text]))[0]
        return result.tolist()
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
                   filt: Filter | None,
                   score_threshold: float | None = None) -> list[dict]:
    """3-way hybrid: BM25 (FTS5) + dense + sparse (SPLADE++) → RRF fusion."""
    import db

    # --- BM25 keyword search via FTS5 ---
    bm25_ranked: list[tuple[str, float]] = []
    try:
        fts_hits = db.fts_search("fts_memory", text, limit=limit * 3)
        for hit in fts_hits:
            pid = hit.get("point_id", "")
            bm25_score = -hit.get("rank", 0.0)
            if pid:
                bm25_ranked.append((pid, bm25_score))
    except Exception:
        pass

    # --- Qdrant vector search (dense + SPLADE++) ---
    sparse = sparse_embed(text)
    qdrant_results = []
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
            limit=limit * 3,
            score_threshold=score_threshold,
        )
        qdrant_results = results.points
    except Exception as e:
        _log.debug(f"hybrid search failed, falling back to dense-only: {e}")
        qdrant_results = _search_dense_only_points(qc, vector, limit, filt, score_threshold)

    # --- RRF merge if BM25 results exist ---
    if bm25_ranked and qdrant_results:
        qdrant_ranked = [(str(r.id), r.score) for r in qdrant_results]
        qdrant_payloads = {str(r.id): r for r in qdrant_results}
        merged = db.rrf_merge([bm25_ranked, qdrant_ranked], limit=limit)

        output = []
        for pid, score in merged:
            if pid in qdrant_payloads:
                r = qdrant_payloads[pid]
                d = {k: v for k, v in (r.payload or {}).items()}
                d["id"] = str(r.id)
                d["score"] = round(score, 4)
                output.append(d)
            else:
                # BM25-only hit — find text from FTS
                for hit in fts_hits:
                    if hit.get("point_id") == pid:
                        output.append({
                            "id": pid, "text": hit.get("text", ""),
                            "tag": hit.get("tag", ""), "score": round(score, 4),
                        })
                        break
        return output

    # No BM25 results — use Qdrant only
    return _points_to_dicts(qdrant_results if qdrant_results else [])


def _search_dense_only_points(qc, vector: list[float], limit: int,
                              filt: Filter | None,
                              score_threshold: float | None = None) -> list:
    """Fallback: dense-only search returning raw Qdrant points."""
    try:
        results = qc.query_points(
            config.QDRANT_COLLECTION,
            query=vector,
            using="dense",
            limit=limit,
            query_filter=filt,
            score_threshold=score_threshold,
        )
    except Exception:
        results = qc.query_points(
            config.QDRANT_COLLECTION,
            query=vector,
            limit=limit,
            query_filter=filt,
            score_threshold=score_threshold,
        )
    return results.points


def _search_dense_only(qc, vector: list[float], limit: int,
                       filt: Filter | None,
                       score_threshold: float | None = None) -> list[dict]:
    """Fallback: dense-only search (for v1 collections or errors)."""
    return _points_to_dicts(_search_dense_only_points(qc, vector, limit, filt, score_threshold))


def _search_impl(vector: list[float], limit: int,
                 tag: str | None, thread_id: str | None,
                 query_text: str | None = None,
                 score_threshold: float | None = None) -> list[dict]:
    """Core search — uses hybrid (dense+sparse RRF) when query_text available.

    score_threshold: if set, Qdrant filters results below this score
    before returning — saves bandwidth and context budget.
    """
    try:
        qc = _get_qdrant()
    except Exception as e:
        _log.warning(f"qdrant unavailable for search: {e}")
        return []

    filt = _build_filter(tag, thread_id)

    if query_text:
        return _search_hybrid(qc, vector, query_text, limit, filt, score_threshold)
    else:
        return _search_dense_only(qc, vector, limit, filt, score_threshold)


def search(query: str, limit: int = config.MAX_MEMORY_RESULTS,
           tag: str | None = None, thread_id: str | None = None,
           score_threshold: float | None = None) -> list[dict]:
    """Hybrid semantic + keyword search over memories.

    Uses dense (embedding) + sparse (SPLADE++) vectors with RRF fusion
    for best recall on both semantic and exact keyword matches.

    Args:
        query: search text
        limit: max results
        tag: filter by tag
        thread_id: filter by thread (or None for global search)
        score_threshold: minimum score cutoff (Qdrant-side filtering)

    Returns [{text, tag, thread_id, score, ts, ...}]
    """
    try:
        vector = _embed(query)
    except Exception as e:
        _log.warning(f"embedding unavailable for search: {e}")
        return []
    return _search_impl(vector, limit, tag, thread_id, query_text=query,
                        score_threshold=score_threshold)


def search_by_vector(vector: list[float], limit: int = config.MAX_MEMORY_RESULTS,
                     tag: str | None = None, thread_id: str | None = None,
                     query_text: str | None = None,
                     score_threshold: float | None = None) -> list[dict]:
    """Search using a pre-computed embedding vector.

    If query_text is provided, uses hybrid search (dense + sparse RRF).
    Otherwise falls back to dense-only search.
    """
    return _search_impl(vector, limit, tag, thread_id, query_text=query_text,
                        score_threshold=score_threshold)


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


# ── Chunking ──

_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 100
_CHUNK_THRESHOLD = 1000  # auto-chunk texts longer than this


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into chunks on sentence boundaries.

    Tries to split on '. ', '\\n', '! ', '? ' to preserve meaning.
    Falls back to hard split at `size` if no boundary found.
    """
    if len(text) <= size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Find best split point (sentence boundary near end)
        best = -1
        for sep in [". ", ".\n", "\n\n", "\n", "! ", "? ", "; "]:
            idx = text.rfind(sep, start + size // 2, end)
            if idx > best:
                best = idx + len(sep)
        if best <= start:
            best = end  # hard split
        chunks.append(text[start:best])
        start = best - overlap  # overlap for context continuity
        if start < 0:
            start = 0
    return chunks


# ── Save ──

def save(text: str, tag: str = "general", dedup: bool = True,
         thread_id: str | None = None, meta: dict | None = None) -> str:
    """Save a memory with both dense and sparse vectors.

    Long texts (>1000 chars) are auto-chunked into ~800 char pieces.
    Each chunk gets synthesis_status="pending" for future knowledge graph synthesis.
    Short facts get synthesis_status="skip".

    Args:
        text: memory content
        tag: category (general, user, knowledge, project, etc.)
        dedup: if True, update existing memory if >0.9 similarity
        thread_id: associate with a specific thread/topic
        meta: extra metadata dict (source, source_type, etc.)

    Returns:
        point ID (or first chunk ID for chunked saves)
    """
    # Scrub well-known secret patterns before persistence. Counting matches with
    # a second pass is fine — the payloads are already small and this runs once.
    text, scrubbed = _scrub_secrets(text)
    if scrubbed:
        hits = text.count("[REDACTED")
        _log.warning(f"scrubbed {hits} secret-like pattern(s) from memory save (tag={tag})")

    # Auto-chunk long texts
    if len(text) > _CHUNK_THRESHOLD and tag not in ("experience", "compaction"):
        return _save_chunked(text, tag, thread_id, meta)

    # Short text — save as single point
    return _save_single(text, tag, dedup, thread_id, meta,
                        synthesis_status="skip")


def _save_chunked(text: str, tag: str,
                  thread_id: str | None, meta: dict | None) -> str:
    """Save long text as multiple chunks with synthesis metadata."""
    chunks = _chunk_text(text)
    source = (meta or {}).get("source", f"mem_{int(time.time())}")
    group = f"{source}_{int(time.time())}"
    first_id = None

    _log.info(f"chunking text ({len(text)} chars) into {len(chunks)} chunks, group={group}")

    for i, chunk in enumerate(chunks):
        chunk_meta = dict(meta or {})
        chunk_meta.update({
            "synthesis_status": "pending",
            "synthesis_group": group,
            "chunk_index": i,
            "chunk_total": len(chunks),
            "source": source,
        })
        pid = _save_single(chunk, tag, dedup=False, thread_id=thread_id,
                           meta=chunk_meta, synthesis_status="pending")
        if i == 0:
            first_id = pid

    return first_id or ""


def _save_single(text: str, tag: str, dedup: bool = True,
                 thread_id: str | None = None, meta: dict | None = None,
                 synthesis_status: str = "skip") -> str:
    """Save a single memory point to Qdrant + FTS5."""
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
        "synthesis_status": synthesis_status,
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
                import db
                db.fts_upsert("fts_memory", "point_id", str(existing.id),
                              {"tag": tag, "text": text})
                return str(existing.id)
        except Exception as e:
            _log.debug(f"dedup check failed, saving as new: {e}")

    point_id = str(uuid.uuid4())
    qc.upsert(
        config.QDRANT_COLLECTION,
        points=[PointStruct(id=point_id, vector=vectors, payload=payload)],
    )
    import db
    db.fts_upsert("fts_memory", "point_id", point_id, {"tag": tag, "text": text})
    return point_id


# ── Synthesis Queue ──

def get_pending_synthesis(limit: int = 50) -> dict[str, list[dict]]:
    """Get pending synthesis items grouped by synthesis_group.

    Returns: {group_name: [{"id": ..., "text": ..., "tag": ..., ...}, ...]}
    """
    qc = _get_qdrant()
    try:
        results = qc.scroll(
            config.QDRANT_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="synthesis_status",
                               match=MatchValue(value="pending"))
            ]),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        points = results[0] if results else []
        groups: dict[str, list[dict]] = {}
        for p in points:
            group = p.payload.get("synthesis_group", "ungrouped")
            entry = {"id": p.id, **p.payload}
            groups.setdefault(group, []).append(entry)
        return groups
    except Exception as e:
        _log.warning(f"get_pending_synthesis failed: {e}")
        return {}


def mark_synthesized(point_ids: list[str]):
    """Mark points as synthesized (status=done)."""
    qc = _get_qdrant()
    for pid in point_ids:
        try:
            qc.set_payload(
                config.QDRANT_COLLECTION,
                payload={"synthesis_status": "done"},
                points=[pid],
            )
        except Exception as e:
            _log.warning(f"mark_synthesized failed for {pid}: {e}")


def get_all_entities(limit: int = 200) -> list[dict]:
    """Get all entity nodes for graph visualization.

    Returns list of {id, name, type, description, relations, observation_count}.
    """
    qc = _get_qdrant()
    try:
        results = qc.scroll(
            config.QDRANT_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="tag", match=MatchValue(value="entity"))
            ]),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        points = results[0] if results else []
        entities = []
        for p in points:
            entities.append({
                "id": str(p.id),
                "name": p.payload.get("text", ""),
                "type": p.payload.get("entity_type", "concept"),
                "description": p.payload.get("description", ""),
                "relations": p.payload.get("relations", []),
                "observation_count": p.payload.get("observation_count", 1),
            })
        return entities
    except Exception as e:
        _log.warning(f"get_all_entities failed: {e}")
        return []


# ── Delete / Cleanup ──

def delete(point_id: str) -> bool:
    """Delete a single memory by its point ID (Qdrant + FTS5)."""
    try:
        qc = _get_qdrant()
        qc.delete(config.QDRANT_COLLECTION, points_selector=[point_id])
    except Exception:
        return False
    import db
    db.fts_delete("fts_memory", "point_id", point_id)
    return True


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


def clear_graph():
    """Delete all entity and wiki memories (clears the knowledge graph)."""
    qc = _get_qdrant()
    deleted = 0
    try:
        from qdrant_client.models import FilterSelector
        for tag in ("entity", "wiki"):
            result = qc.delete(
                config.QDRANT_COLLECTION,
                points_selector=FilterSelector(
                    filter=Filter(must=[
                        FieldCondition(key="tag", match=MatchValue(value=tag)),
                    ])
                ),
            )
            _log.info(f"cleared graph: tag={tag}")
            deleted += 1
    except Exception as e:
        _log.warning(f"clear_graph failed: {e}")
    return deleted


def count() -> int:
    """Count total memories."""
    try:
        qc = _get_qdrant()
        info = qc.get_collection(config.QDRANT_COLLECTION)
        return info.points_count or 0
    except Exception:
        return 0
