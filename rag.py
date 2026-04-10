"""RAG — index and search local files via Qdrant (hybrid dense + sparse)."""

import os, time, uuid, threading
from pathlib import Path
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
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PDF_EXTENSION = ".pdf"
ALL_INDEXABLE = SUPPORTED_EXTENSIONS | IMAGE_EXTENSIONS | {PDF_EXTENSION}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
VISION_RATE_LIMIT = 1.0  # seconds between vision API calls
SCANNED_PAGE_THRESHOLD = 50  # chars — below this, page is likely scanned
MAX_SCAN_FILES = 1000

# Lazy imports
_qclient = None


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
                        size=memory.EMBED_DIM,
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
            if "tags" not in existing:
                _qclient.create_payload_index(RAG_COLLECTION, "tags", PayloadSchemaType.KEYWORD)
                _log.info("created payload index: tags")
        except Exception as e:
            _log.debug(f"RAG payload index creation skipped: {e}")
    return _qclient


def _embed(text: str) -> list[float]:
    """Generate dense embedding via memory module's FastEmbed model."""
    import memory
    return memory._embed(text)


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
    """Read file content. Supports text files, PDFs, and images."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        try:
            return _describe_image(path)
        except Exception as e:
            _log.error(f"image read failed: {path}: {e}")
            return None
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


def index_file(filepath: str, tags: list[str] | None = None) -> dict:
    """Index a file into the unified memory collection (tag=knowledge).
    File content is auto-chunked by memory.save() and queued for synthesis."""
    import memory

    path = Path(filepath).expanduser().resolve()
    if not path.exists():
        return {"path": str(path), "chunks": 0, "status": "not found"}

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS and ext != ".pdf" and ext not in IMAGE_EXTENSIONS:
        return {"path": str(path), "chunks": 0, "status": f"unsupported format: {ext}"}

    # Check if already indexed and unchanged
    mtime_key = f"rag:mtime:{path}"
    tags_key = f"rag:tags:{path}"
    stored_mtime = db.kv_get(mtime_key)
    current_mtime = str(path.stat().st_mtime)
    stored_tags = db.kv_get(tags_key) or ""
    new_tags = ",".join(tags) if tags else ""
    if stored_mtime == current_mtime and stored_tags == new_tags:
        return {"path": str(path), "chunks": 0, "status": "already up to date"}

    # Read file content
    content = _read_file(path)
    if not content or not content.strip():
        return {"path": str(path), "chunks": 0, "status": "empty file"}

    # Delete old chunks for this file (keeps kv entries — updated below on success)
    _delete_file_chunks(str(path))

    # Save via memory.save() — handles chunking, embeddings, FTS5, synthesis queue
    meta = {
        "source": str(path),
        "source_type": "file",
        "file_path": str(path),
        "filename": path.name,
    }
    if tags:
        meta["document_tags"] = tags

    memory.save(content, tag="knowledge", dedup=False, meta=meta)

    # Count chunks (for UI feedback) — matches memory.save() branching logic
    if len(content) > memory._CHUNK_THRESHOLD:
        chunk_count = len(memory._chunk_text(content))
    else:
        chunk_count = 1

    # Store mtime + tags
    db.kv_set(mtime_key, current_mtime)
    if tags:
        db.kv_set(tags_key, ",".join(tags))
    else:
        db.execute("DELETE FROM kv WHERE key = ?", (tags_key,))
    _log.info(f"indexed {path} → memory collection: {chunk_count} chunks")
    return {"path": str(path), "chunks": chunk_count, "status": "indexed"}


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
        if ext in ALL_INDEXABLE:
            result = index_file(str(f))
            results.append(result)
    return results


def search(query: str, limit: int = 5, tags: list[str] | None = None) -> list[dict]:
    """Search indexed files via unified memory collection.

    Args:
        query: search text.
        limit: max results.
        tags: optional tags filter (matches document_tags in payload).

    Returns [{text, file_path, chunk_index, score, tags}].
    """
    import memory

    # Search with tag=knowledge (files) via memory.search
    results = memory.search(query, limit=limit * 3, tag="knowledge")

    output = []
    for r in results:
        # Filter by document_tags if requested
        if tags:
            doc_tags = r.get("document_tags", [])
            if not any(t in doc_tags for t in tags):
                continue
        output.append({
            "text": r.get("text", ""),
            "file_path": r.get("file_path", r.get("source", "")),
            "chunk_index": r.get("chunk_index", 0),
            "score": round(r.get("score", 0.0), 4),
            "tags": r.get("document_tags", []),
        })
        if len(output) >= limit:
            break
    return output


def get_status() -> dict:
    """Get knowledge index status (unified memory collection)."""
    import memory
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    count = 0
    try:
        qc = memory._get_qdrant()
        # Count knowledge-tagged points with source_type=file
        result = qc.count(
            config.QDRANT_COLLECTION,
            count_filter=Filter(must=[
                FieldCondition(key="source_type", match=MatchValue(value="file")),
            ]),
        )
        count = result.count
    except Exception:
        pass

    # Count indexed files from kv
    files = db.fetchone("SELECT COUNT(*) FROM kv WHERE key LIKE 'rag:mtime:%'")[0]

    return {"files": files, "chunks": count}


def stats() -> dict:
    """Alias for get_status() — for cli/telegram_bot compatibility."""
    s = get_status()
    return {"total_files": s["files"], "total_chunks": s["chunks"]}


def _delete_file_chunks(file_path: str):
    """Remove all chunks for a given file path from unified memory collection."""
    import memory
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    try:
        qc = memory._get_qdrant()
        qc.delete(
            config.QDRANT_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="file_path", match=MatchValue(value=file_path)),
                ])
            ),
        )
    except Exception as e:
        _log.debug(f"delete chunks from memory collection failed: {e}")
    # Legacy RAG collection cleanup
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
    db.fts_delete_match("fts_rag", "file_path", file_path)
    # fts_memory schema: (point_id, tag, text) — no file_path column.
    # Stale rows for re-indexed files remain but new chunks dedup by content in memory.save().


# ---------------------------------------------------------------------------
# Vision / image description
# ---------------------------------------------------------------------------

_vision_lock = threading.Lock()
_last_vision_call = 0.0


def _describe_image(path_or_bytes, prompt=None) -> str:
    """Describe an image using LM Studio vision API.

    Args:
        path_or_bytes: Path object or raw bytes of an image.
        prompt: optional text prompt for the vision model.

    Returns:
        Description string, or fallback placeholder on error.
    """
    import base64
    global _last_vision_call

    try:
        from PIL import Image
        import io
    except ImportError:
        _log.warning("Pillow not installed — pip install Pillow to process images")
        return "[image: description unavailable — Pillow not installed]"

    try:
        # Read bytes
        if isinstance(path_or_bytes, (str, Path)):
            img_bytes = Path(path_or_bytes).read_bytes()
        else:
            img_bytes = path_or_bytes

        # Resize to max 512px
        img = Image.open(io.BytesIO(img_bytes))
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        resized_bytes = buf.getvalue()

        b64 = base64.b64encode(resized_bytes).decode("ascii")

        # Rate limit (thread-safe)
        with _vision_lock:
            elapsed = time.time() - _last_vision_call
            if elapsed < VISION_RATE_LIMIT:
                time.sleep(VISION_RATE_LIMIT - elapsed)
            _last_vision_call = time.time()

        import providers
        client = providers.get_client()
        model = providers.get_model()

        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or "Describe this image in detail. Include any text, diagrams, or visual elements you can see."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            max_tokens=512,
        )
        return resp.choices[0].message.content or "[image: empty response]"
    except Exception as e:
        _log.error(f"vision describe failed: {e}")
        return "[image: description unavailable]"


# ---------------------------------------------------------------------------
# PDF with vision fallback for scanned pages
# ---------------------------------------------------------------------------


def _read_pdf_with_vision(path) -> str:
    """Read PDF with page markers; notes scanned pages that lack text.

    Args:
        path: Path to the PDF file.

    Returns:
        Concatenated page text with markers.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        _log.warning("pypdf not installed — pip install pypdf to read PDFs")
        return ""

    path = Path(path)
    reader = PdfReader(str(path))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if len(text.strip()) < SCANNED_PAGE_THRESHOLD:
            parts.append(f"--- page {i} ---\n[scanned page {i} - text extraction not available]")
        else:
            parts.append(f"--- page {i} ---\n{text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Scan path — analyse files before indexing
# ---------------------------------------------------------------------------


def scan_path(path_str: str, recursive: bool = True) -> dict:
    """Scan a file or directory and return indexing plan.

    Args:
        path_str: path to a file or directory.
        recursive: whether to recurse into subdirectories (default True).

    Returns:
        dict with ``files``, ``summary``, and ``estimate`` keys.
    """
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        return {"files": [], "summary": {}, "estimate": {}, "error": f"path not found: {path}"}

    # Gather file list
    if path.is_file():
        file_list = [path]
    else:
        pattern = "**/*" if recursive else "*"
        file_list = sorted(f for f in path.glob(pattern) if f.is_file())

    summary = {"text": 0, "pdf": 0, "image": 0, "unsupported": 0, "skipped": 0}
    est_chunks = 0
    est_time_cpu = 0.0
    est_time_gpu = 0.0
    gpu_required = False
    files = []

    for f in file_list[:MAX_SCAN_FILES]:
        ext = f.suffix.lower()
        size = f.stat().st_size

        if size > MAX_FILE_SIZE:
            files.append({"path": str(f), "type": "skipped", "reason": "too large", "size": size})
            summary["skipped"] += 1
            continue

        if ext in SUPPORTED_EXTENSIONS:
            ftype, method = "text", "chunk_text"
            summary["text"] += 1
            est_chunks += max(1, size // CHUNK_SIZE)
            est_time_cpu += 0.1
        elif ext == PDF_EXTENSION:
            ftype, method = "pdf", "pdf_extract"
            f_stat = f.stat()
            # Estimate pages from file size (~50KB per page) — avoids opening every PDF during scan
            est_pages = max(1, f_stat.st_size // 50000)
            summary["pdf"] += 1
            est_chunks += max(1, est_pages * 2)
            est_time_cpu += 0.2 * est_pages
            entry = {"path": str(f), "type": ftype, "method": method, "pages": est_pages, "size": size}
            files.append(entry)
            continue
        elif ext in IMAGE_EXTENSIONS:
            ftype, method = "image", "vision_describe"
            summary["image"] += 1
            est_chunks += 1
            est_time_gpu += 2.0
            gpu_required = True
        else:
            files.append({"path": str(f), "type": "unsupported", "ext": ext})
            summary["unsupported"] += 1
            continue

        files.append({"path": str(f), "type": ftype, "method": method, "size": size})

    return {
        "files": files,
        "summary": summary,
        "estimate": {
            "chunks": est_chunks,
            "time_cpu_sec": round(est_time_cpu, 1),
            "time_gpu_sec": round(est_time_gpu, 1),
            "gpu_required": gpu_required,
        },
    }


# ---------------------------------------------------------------------------
# Batch indexing with progress callbacks
# ---------------------------------------------------------------------------


def index_files_batch(files: list[dict], progress_cb=None, phase_cb=None,
                      tags: list[str] | None = None) -> list[dict]:
    """Batch-index a list of file descriptors from scan_path.

    Args:
        files: list of dicts with ``path``, ``type``, ``method`` keys.
        progress_cb: optional ``(current, total, path, phase, detail) -> None``.
        phase_cb: optional ``(event, count, estimate_sec) -> None``.
        tags: optional list of tag strings to attach to all indexed chunks.

    Returns:
        list of result dicts ``[{path, chunks, status, method}]``.
    """
    from qdrant_client.models import PointStruct
    import memory

    # Normalize method names (frontend sends "text"/"pdf"/"vision",
    # backend uses "chunk_text"/"pdf_extract"/"vision_describe")
    _METHOD_MAP = {
        "text": "chunk_text", "pdf": "pdf_extract", "vision": "vision_describe",
        "chunk_text": "chunk_text", "pdf_extract": "pdf_extract",
        "vision_describe": "vision_describe", "pdf_scan": "pdf_scan",
    }
    for f in files:
        f["method"] = _METHOD_MAP.get(f.get("method", ""), "chunk_text")

    cpu_files = [f for f in files if f.get("method") in ("chunk_text", "pdf_extract")]
    gpu_files = [f for f in files if f.get("method") in ("vision_describe", "pdf_scan")]
    total = len(cpu_files) + len(gpu_files)
    results = []
    current = 0

    # Phase 1: CPU — text and normal PDF
    for f in cpu_files:
        current += 1
        fpath = f["path"]
        if progress_cb:
            progress_cb(current, total, fpath, "cpu", f.get("method", ""))
        result = index_file(fpath, tags=tags)
        result["method"] = f.get("method", "chunk_text")
        results.append(result)

    # Phase 2: GPU — images and scanned PDFs
    if gpu_files:
        est_sec = sum(2.0 if f.get("method") == "vision_describe" else 3.0 * f.get("pages", 1) for f in gpu_files)
        if phase_cb:
            phase_cb("gpu_warning", len(gpu_files), est_sec)

        for f in gpu_files:
            current += 1
            fpath = f["path"]
            method = f.get("method", "vision_describe")
            if progress_cb:
                progress_cb(current, total, fpath, "gpu", method)

            try:
                path = Path(fpath).expanduser().resolve()

                if method == "vision_describe":
                    content = _describe_image(path)
                elif method == "pdf_scan":
                    content = _read_pdf_with_vision(path)
                else:
                    content = _read_file(path)

                if not content or not content.strip():
                    results.append({"path": str(path), "chunks": 0, "status": "empty", "method": method})
                    continue

                chunks = _chunk_text(content)
                qc = _get_qdrant()
                _delete_file_chunks(str(path))

                points = []
                for i, chunk in enumerate(chunks):
                    dense = _embed(chunk)
                    sparse = memory.sparse_embed(chunk)
                    payload = {
                            "text": chunk,
                            "file_path": str(path),
                            "chunk_index": i,
                            "total_chunks": len(chunks),
                            "indexed_at": time.time(),
                        }
                    if tags:
                        payload["tags"] = tags
                    points.append(PointStruct(
                        id=str(uuid.uuid4()),
                        vector={"dense": dense, "sparse": sparse},
                        payload=payload,
                    ))

                if points:
                    for batch_start in range(0, len(points), 100):
                        batch = points[batch_start:batch_start + 100]
                        qc.upsert(RAG_COLLECTION, points=batch)

                    # Mirror to FTS5 for BM25 keyword search
                    for pt in points:
                        db.fts_upsert("fts_rag", "chunk_id", pt.id,
                                      {"file_path": pt.payload["file_path"], "text": pt.payload["text"]})

                mtime_key = f"rag:mtime:{path}"
                tags_key = f"rag:tags:{path}"
                db.kv_set(mtime_key, str(path.stat().st_mtime))
                if tags:
                    db.kv_set(tags_key, ",".join(tags))
                else:
                    db.execute("DELETE FROM kv WHERE key = ?", (tags_key,))
                _log.info(f"indexed {path}: {len(chunks)} chunks (method={method})")
                results.append({"path": str(path), "chunks": len(chunks), "status": "indexed", "method": method})
            except Exception as e:
                _log.error(f"batch index failed for {fpath}: {e}")
                results.append({"path": fpath, "chunks": 0, "status": f"error: {e}", "method": method})

    return results


# ---------------------------------------------------------------------------
# List / delete indexed files
# ---------------------------------------------------------------------------


def list_indexed_files() -> list[dict]:
    """Return all indexed files from the kv store.

    Returns:
        list of ``{path, mtime, filename, tags}`` dicts.
    """
    rows = db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'rag:mtime:%'")
    results = []
    prefix_len = len("rag:mtime:")
    for key, value in rows:
        fpath = key[prefix_len:]
        tags_str = db.kv_get(f"rag:tags:{fpath}") or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
        results.append({
            "path": fpath,
            "mtime": float(value) if value else 0.0,
            "filename": Path(fpath).name,
            "tags": tags,
        })
    return results


def delete_file(file_path: str) -> dict:
    """Delete a file from the RAG index.

    Args:
        file_path: absolute path of the file to remove.

    Returns:
        ``{path, status}`` dict.
    """
    path = Path(file_path).expanduser().resolve()
    _delete_file_chunks(str(path))
    # Remove mtime + tags tracking
    db.execute("DELETE FROM kv WHERE key = ?", (f"rag:mtime:{path}",))
    db.execute("DELETE FROM kv WHERE key = ?", (f"rag:tags:{path}",))
    return {"path": str(path), "status": "deleted"}
