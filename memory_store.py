"""Markdown-backed memory storage — canonical, inspectable, diffable.

Phase 1 of the Living Memory architecture (see ``docs/adr/0001-living-memory.md``).

Every memory that Qdrant holds has a parallel ``.md`` file under
``~/.qwe-qwe/memories/atoms/<shard>/mem_<id>.md`` with YAML frontmatter.
On this layer, markdown is the **source of truth**; Qdrant is a derived
search index rebuildable from the files.

Phase 1 scope is storage-only: dual-write from save(), dual-delete,
on-boot backfill of existing Qdrant points that have no markdown yet.
Living Memory semantics (salience decay, anchors, typed connections,
nightly mutation, crystallisation) arrive in later phases.

Design notes:

- Sharding by first 2 chars of the point_id (UUID hex) keeps at most
  ~40 files per leaf directory under the default Qdrant UUID4 space,
  even at 10k+ memories. Avoids Windows filesystem slowness on huge
  single-directory listings.
- YAML frontmatter is read by PyYAML (already a transitive dep). All
  frontmatter keys the reader doesn't recognise are preserved on
  round-trip — future phases can add fields without a migration.
- Secret scrubbing happens in ``memory.save`` BEFORE we're called.
  That's a deliberate contract: this module never sees unscrubbed text.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

import config

_log = logging.getLogger("qwe.memory_store")

# ── Paths ─────────────────────────────────────────────────────────────


def memories_root() -> Path:
    """Root of the markdown memory store. Created on first access."""
    p = config.DATA_DIR / "memories"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atoms_dir() -> Path:
    return memories_root() / "atoms"


def _shard_for(point_id: str) -> str:
    """First 2 chars of the point_id → shard directory name.

    UUID hex chars are ``[0-9a-f]``, giving 256 possible shards. Enough
    to keep each leaf below ~50 files at 10k memories without requiring
    a rebalance as the corpus grows.
    """
    # Strip any non-hex prefix (e.g. "mem_") just in case, lowercase for
    # a stable shard regardless of how the id was written.
    s = re.sub(r"[^a-z0-9]", "", point_id.lower())
    return (s[:2] or "00")


def path_for(point_id: str) -> Path:
    """Full .md path for a memory id (does NOT check existence)."""
    return _atoms_dir() / _shard_for(point_id) / f"mem_{point_id}.md"


# ── Frontmatter format ────────────────────────────────────────────────

_FRONTMATTER_DELIM = "---"


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Parse a raw .md file into (frontmatter_dict, body_text).

    A file missing the opening ``---`` is treated as all-body with empty
    frontmatter, so hand-written memories without frontmatter still load.
    """
    if not raw.startswith(_FRONTMATTER_DELIM):
        return {}, raw
    # Split into 3 parts: empty_before, frontmatter, body
    parts = raw.split("\n" + _FRONTMATTER_DELIM + "\n", 1)
    if len(parts) != 2:
        # Malformed — treat as plain body
        return {}, raw
    head, body = parts
    # head starts with "---\n..."; strip the opening delim line
    fm_text = head[len(_FRONTMATTER_DELIM):].lstrip("\n")
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        _log.warning(f"invalid frontmatter YAML, treating as empty: {e}")
        meta = {}
    if not isinstance(meta, dict):
        # Shouldn't happen for a frontmatter block, but defensive
        meta = {}
    return meta, body.lstrip("\n")


def _serialize(frontmatter: dict, body: str) -> str:
    """Assemble frontmatter + body into a .md file's text form."""
    fm_text = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    # Make sure body has a trailing newline for clean git diffs
    body = body.rstrip() + "\n"
    return f"{_FRONTMATTER_DELIM}\n{fm_text}\n{_FRONTMATTER_DELIM}\n\n{body}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Public API ────────────────────────────────────────────────────────


def write(point_id: str, text: str, tag: str = "general",
          thread_id: str | None = None, meta: dict | None = None,
          created_iso: str | None = None) -> Path:
    """Create or overwrite the .md file for ``point_id``.

    Idempotent: if a file already exists we merge its frontmatter with
    the new one, keeping any Phase-2+ fields the caller didn't know
    about (salience, anchor, connections, …). Body always overwrites.
    """
    p = path_for(point_id)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing_meta: dict = {}
    if p.exists():
        try:
            existing_meta, _ = _split_frontmatter(p.read_text(encoding="utf-8"))
        except Exception as e:
            _log.debug(f"couldn't read existing {p} for merge: {e}")

    frontmatter = {
        **existing_meta,                          # preserve unknown fields
        "id": point_id,
        "type": "atom",                           # Phase 1: only atoms
        "tag": tag,
        "created": existing_meta.get("created") or created_iso or _now_iso(),
        "updated": _now_iso(),
    }
    if thread_id:
        frontmatter["thread_id"] = thread_id
    # User-supplied meta passthrough — wiki_path, source_type, indexed_at, …
    if meta:
        # meta is part of payload, NOT merged at top level to avoid
        # accidental overwrite of id/type/tag/created/updated.
        frontmatter["meta"] = {**(existing_meta.get("meta") or {}), **meta}

    p.write_text(_serialize(frontmatter, text), encoding="utf-8")
    return p


def read(point_id: str) -> dict | None:
    """Load a memory by id. Returns ``None`` if no file exists."""
    p = path_for(point_id)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as e:
        _log.warning(f"read failed for {p}: {e}")
        return None
    fm, body = _split_frontmatter(raw)
    return {
        "id": fm.get("id") or point_id,
        "type": fm.get("type") or "atom",
        "tag": fm.get("tag") or "general",
        "thread_id": fm.get("thread_id"),
        "created": fm.get("created"),
        "updated": fm.get("updated"),
        "meta": fm.get("meta") or {},
        # Strip the single trailing newline _serialize adds for clean git
        # diffs — callers of read() want the raw text they wrote.
        "text": body.rstrip("\n"),
        "_raw_frontmatter": fm,  # future-phase fields preserved here
        "_path": str(p),
    }


def delete(point_id: str) -> bool:
    """Remove the .md file. Returns True on success, False if missing or error."""
    p = path_for(point_id)
    if not p.exists():
        return False
    try:
        p.unlink()
    except Exception as e:
        _log.warning(f"delete failed for {p}: {e}")
        return False
    # Best-effort cleanup of now-empty shard dir
    try:
        if not any(p.parent.iterdir()):
            p.parent.rmdir()
    except Exception:
        pass
    return True


def iter_all() -> list[str]:
    """List every point_id that has a markdown file. Used by migration +
    consistency checks. Sorted for deterministic test output."""
    root = _atoms_dir()
    if not root.exists():
        return []
    ids: list[str] = []
    pattern = re.compile(r"^mem_(.+)\.md$")
    for shard in sorted(root.iterdir()):
        if not shard.is_dir():
            continue
        for f in sorted(shard.iterdir()):
            m = pattern.match(f.name)
            if m:
                ids.append(m.group(1))
    return ids


# ── Consistency with Qdrant ───────────────────────────────────────────


def backfill_from_qdrant(limit: int | None = None) -> dict:
    """One-time: export every Qdrant point that has no .md file yet.

    Called on boot from memory._init() (guarded by a kv flag so it only
    runs once). Safe to re-run — skips files that already exist.

    Returns ``{"scanned": N, "written": N, "skipped": N, "errors": N}``.
    """
    import db
    stamp = db.kv_get("memory_store:backfill_done")
    # The stamp is "done" once we've walked the whole corpus. If a later
    # run finds new files to write (e.g. a user restored an old DB), we
    # re-stamp.

    try:
        from memory import _get_qdrant
    except Exception as e:
        _log.warning(f"qdrant not available for backfill: {e}")
        return {"scanned": 0, "written": 0, "skipped": 0, "errors": 1}

    qc = _get_qdrant()
    stats = {"scanned": 0, "written": 0, "skipped": 0, "errors": 0}

    # Scroll through all Qdrant points; skip those already on disk.
    offset = None
    page_size = 200
    remaining = limit
    while True:
        try:
            points, next_offset = qc.scroll(
                collection_name=config.QDRANT_COLLECTION,
                limit=min(page_size, remaining) if remaining else page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            _log.warning(f"qdrant scroll failed mid-backfill: {e}")
            stats["errors"] += 1
            break

        if not points:
            break

        for pt in points:
            stats["scanned"] += 1
            pid = str(pt.id)
            payload = pt.payload or {}
            text = payload.get("text") or ""
            if not text:
                stats["skipped"] += 1
                continue
            if path_for(pid).exists():
                stats["skipped"] += 1
                continue
            try:
                # Preserve the original ts as ``created`` when migrating
                created_iso = None
                if payload.get("ts"):
                    try:
                        created_iso = datetime.fromtimestamp(
                            payload["ts"], timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception:
                        pass
                # Split top-level passthrough fields from the free-form meta
                # bucket. text/tag/ts live at top level of payload by
                # convention; everything else lands under meta.
                top_level = {"text", "tag", "ts", "thread_id", "synthesis_status"}
                extra_meta = {k: v for k, v in payload.items() if k not in top_level}
                write(
                    pid,
                    text,
                    tag=payload.get("tag", "general"),
                    thread_id=payload.get("thread_id"),
                    meta=extra_meta or None,
                    created_iso=created_iso,
                )
                stats["written"] += 1
            except Exception as e:
                _log.warning(f"backfill write failed for {pid}: {e}")
                stats["errors"] += 1

        if remaining is not None:
            remaining -= len(points)
            if remaining <= 0:
                break
        if next_offset is None:
            break
        offset = next_offset

    if stats["written"] > 0 or not stamp:
        db.kv_set("memory_store:backfill_done",
                  f"{_now_iso()} (scanned={stats['scanned']} written={stats['written']})")
    _log.info(f"memory_store backfill: {stats}")
    return stats


def memories_dir_str() -> str:
    """Return str path of the memories root — used by integrity blocks
    in tools.py to gate write_file against the whole subtree."""
    return str(memories_root().resolve())
