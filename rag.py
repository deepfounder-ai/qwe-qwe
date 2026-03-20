"""RAG — index and search local files via Qdrant (hybrid dense + sparse)."""

import os, time, uuid
from pathlib import Path
from openai import OpenAI
import config, db, logger

_log = logger.get("rag")

# Separate Qdrant collection for RAG (not mixed with agent memory)
RAG_COLLECTION = "qwe_rag"
CHUNK_SIZE = 800        # chars (~200 tokens), configurable via settings
CHUNK_OVERLAP = 100     # chars (~25 tokens), configurable via settings
SUPPORTED_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx",
                        ".json", ".csv", ".yaml", ".yml", ".toml", ".cfg",
                        ".sh", ".bash", ".html", ".css", ".sql", ".go",
                        ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".php"}

# Lazy imports
_qclient = None
_embed_client = None


def _get_qdrant():
    """Get shared Qdrant client from memory module (avoids duplicate connections)."""
    global _qclient
    if _qclient is None:
        import memory
        _qclient = memory._get_qdrant()
        # Ensure RAG collection exists with v2 schema (named dense + sparse)
        from qdrant_client.models import (
            VectorParams, Distance, PayloadSchemaType, Datatype,
            SparseVectorParams,
        )
        cols = [c.name for c in _qclient.get_collections().collections]
        if RAG_COLLECTION not in cols:
            _qclient.create_collection(
                RAG_COLLECTION,
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
        # Ensure payload indexes
        try:
            info = _qclient.get_collection(RAG_COLLECTION)
            existing = set(info.payload_schema.keys()) if info.payload_schema else set()
            if "file_path" not in existing:
                _qclient.create_payload_index(RAG_COLLECTION, "file_path", PayloadSchemaType.KEYWORD)
                _log.info("created payload index: file_path")
        except Exception as e:
            _log.debug(f"RAG payload index creation skipped: {e}")
    return _qclient


def _embed(text: str) -> list[float]:
    global _embed_client
    if _embed_client is None:
        _embed_client = OpenAI(base_url=config.EMBED_BASE_URL, api_key=config.EMBED_API_KEY)
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    resp = _embed_client.embeddings.create(input=text, model=config.EMBED_MODEL)
    return resp.data[0].embedding


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks. Uses configurable sizes from settings."""
    chunk_size = config.get("rag_chunk_size") if "rag_chunk_size" in config.EDITABLE_SETTINGS else CHUNK_SIZE
    chunk_overlap = config.get("rag_chunk_overlap") if "rag_chunk_overlap" in config.EDITABLE_SETTINGS else CHUNK_OVERLAP
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start += chunk_size - chunk_overlap
    return chunks


def _read_file(path: Path) -> str | None:
    """Read file content. Supports text files and optionally PDF."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            _log.warning("pypdf not installed — pip install pypdf to index PDFs")
            return None
        except Exception as e:
            _log.error(f"PDF read failed: {path}: {e}")
            return None
    # Text files
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        _log.error(f"read failed: {path}: {e}")
        return None


def index_file(filepath: str) -> dict:
    """Index a single file with dense + sparse vectors. Returns {path, chunks, status}."""
    from qdrant_client.models import PointStruct
    import memory

    path = Path(filepath).expanduser().resolve()
    if not path.exists():
        return {"path": str(path), "chunks": 0, "status": "not found"}

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS and ext != ".pdf":
        return {"path": str(path), "chunks": 0, "status": f"unsupported format: {ext}"}

    # Check if already indexed and unchanged
    mtime_key = f"rag:mtime:{path}"
    stored_mtime = db.kv_get(mtime_key)
    current_mtime = str(path.stat().st_mtime)
    if stored_mtime == current_mtime:
        return {"path": str(path), "chunks": 0, "status": "already up to date"}

    # Read and chunk
    content = _read_file(path)
    if not content or not content.strip():
        return {"path": str(path), "chunks": 0, "status": "empty file"}

    chunks = _chunk_text(content)
    qc = _get_qdrant()

    # Delete old chunks for this file
    _delete_file_chunks(str(path))

    # Index chunks with both dense and sparse vectors
    points = []
    for i, chunk in enumerate(chunks):
        dense = _embed(chunk)
        sparse = memory.sparse_embed(chunk)
        points.append(PointStruct(
            id=str(uuid.uuid4()),
            vector={"dense": dense, "sparse": sparse},
            payload={
                "text": chunk,
                "file_path": str(path),
                "chunk_index": i,
                "total_chunks": len(chunks),
                "indexed_at": time.time(),
            },
        ))

    if points:
        # Batch upsert (max 100 per batch)
        for batch_start in range(0, len(points), 100):
            batch = points[batch_start:batch_start + 100]
            qc.upsert(RAG_COLLECTION, points=batch)

    # Store mtime
    db.kv_set(mtime_key, current_mtime)
    _log.info(f"indexed {path}: {len(chunks)} chunks")
    return {"path": str(path), "chunks": len(chunks), "status": "indexed"}


def index_directory(dirpath: str, recursive: bool = True) -> list[dict]:
    """Index all supported files in a directory."""
    path = Path(dirpath).expanduser().resolve()
    if not path.is_dir():
        return [{"path": str(path), "chunks": 0, "status": "not a directory"}]

    results = []
    pattern = "**/*" if recursive else "*"
    for f in sorted(path.glob(pattern)):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in SUPPORTED_EXTENSIONS or ext == ".pdf":
            result = index_file(str(f))
            results.append(result)
    return results


def search(query: str, limit: int = 5) -> list[dict]:
    """Hybrid search over indexed files (dense + sparse RRF).

    Returns [{text, file_path, chunk_index, score}].
    """
    import memory
    from qdrant_client.models import Prefetch, FusionQuery, Fusion

    qc = _get_qdrant()
    dense = _embed(query)
    sparse = memory.sparse_embed(query)

    try:
        results = qc.query_points(
            RAG_COLLECTION,
            prefetch=[
                Prefetch(query=sparse, using="sparse", limit=limit * 4),
                Prefetch(query=dense, using="dense", limit=limit * 4),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=limit,
        )
    except Exception as e:
        _log.debug(f"hybrid RAG search failed, falling back to dense: {e}")
        try:
            results = qc.query_points(RAG_COLLECTION, query=dense,
                                      using="dense", limit=limit)
        except Exception:
            results = qc.query_points(RAG_COLLECTION, query=dense, limit=limit)

    return [
        {
            "text": r.payload.get("text", ""),
            "file_path": r.payload.get("file_path", ""),
            "chunk_index": r.payload.get("chunk_index", 0),
            "score": round(r.score, 3),
        }
        for r in results.points
    ]


def get_status() -> dict:
    """Get RAG index status."""
    try:
        qc = _get_qdrant()
        info = qc.get_collection(RAG_COLLECTION)
        count = info.points_count or 0
    except Exception:
        count = 0

    # Count indexed files from kv
    files = db.fetchone("SELECT COUNT(*) FROM kv WHERE key LIKE 'rag:mtime:%'")[0]

    return {"files": files, "chunks": count}


def _delete_file_chunks(file_path: str):
    """Remove all chunks for a given file path."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    try:
        qc = _get_qdrant()
        qc.delete(
            RAG_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="file_path", match=MatchValue(value=file_path)),
                ])
            ),
        )
    except Exception:
        pass
