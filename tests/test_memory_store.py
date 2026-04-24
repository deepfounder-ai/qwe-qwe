"""Living Memory — Phase 1 storage tests (ADR-0001).

Covers the markdown-backed memory store that lives alongside Qdrant:

- Round-trip write/read preserves text, tag, thread_id, meta
- Frontmatter is valid YAML with the expected keys
- Sharding distributes files across subdirs (no 10k-in-one-dir)
- Idempotent re-write preserves unknown frontmatter fields (so Phase 2+
  can add salience/anchor/connections without a migration)
- Delete removes the file and cleans the empty shard dir
- iter_all lists every written id deterministically
- Integrity block refuses write_file paths under the memory dir
- memory.save dual-writes: Qdrant point AND companion .md
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture
def store(qwe_temp_data_dir):
    """Fresh memory_store module against the tempdir."""
    import importlib
    import sys
    for m in ("config", "memory_store"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    return sys.modules["memory_store"]


# ── Round-trip ────────────────────────────────────────────────────────


def test_write_then_read_roundtrip(store):
    pid = "abcdef12-3456-7890-abcd-ef1234567890"
    store.write(pid, "hello world", tag="knowledge",
                thread_id="t_foo", meta={"source_type": "manual"})
    got = store.read(pid)
    assert got is not None
    assert got["id"] == pid
    assert got["tag"] == "knowledge"
    assert got["thread_id"] == "t_foo"
    assert got["text"] == "hello world"
    assert got["meta"]["source_type"] == "manual"


def test_read_missing_returns_none(store):
    assert store.read("no-such-id-here") is None


def test_write_creates_markdown_file_with_frontmatter(store):
    pid = "deadbeef-0000-1111-2222-333344445555"
    p = store.write(pid, "body text here", tag="fact")
    raw = Path(p).read_text(encoding="utf-8")
    # Frontmatter delimiters
    assert raw.startswith("---\n"), "missing opening frontmatter delim"
    assert "\n---\n" in raw, "missing closing frontmatter delim"
    # Expected keys visible in frontmatter
    for key in ("id:", "type:", "tag:", "created:", "updated:"):
        assert key in raw, f"frontmatter missing {key!r}"
    # Body preserved
    assert "body text here" in raw


# ── Sharding ──────────────────────────────────────────────────────────


def test_sharding_splits_files_across_subdirs(store):
    ids = [f"{prefix}1234-5678-9abc-def0-0000000000{i:02x}"
           for prefix, i in [("aa", 0), ("bb", 1), ("cc", 2), ("aa", 3)]]
    for pid in ids:
        store.write(pid, f"body {pid}")
    root = store.memories_root() / "atoms"
    shards = sorted(d.name for d in root.iterdir() if d.is_dir())
    # Two aa ids share a shard; bb and cc get their own
    assert shards == ["aa", "bb", "cc"]
    # aa shard contains both aa ids
    aa_files = sorted(f.name for f in (root / "aa").iterdir())
    assert len(aa_files) == 2


def test_path_for_is_deterministic(store):
    pid = "abcd1234"
    p1 = store.path_for(pid)
    p2 = store.path_for(pid)
    assert p1 == p2


# ── Idempotent writes preserve unknown frontmatter ────────────────────


def test_rewrite_preserves_unknown_frontmatter_fields(store):
    """Phase 2+ fields (salience, anchor, connections) added by future
    code must survive a Phase-1-style rewrite that doesn't know about
    them. Otherwise we'd need a migration every time we extend the
    schema."""
    pid = "feed0000-aaaa-bbbb-cccc-000000000001"
    # Initial write
    p = store.write(pid, "v1", tag="knowledge")
    # Someone (future phase) appends fields the current writer doesn't know.
    # Inject them at the end of the frontmatter block via a line-level
    # replace that won't collide with any UUID substring.
    raw = Path(p).read_text(encoding="utf-8")
    raw = raw.replace(
        "updated:",
        "salience: 0.8\nanchor: true\nupdated:",
        1,
    )
    Path(p).write_text(raw, encoding="utf-8")
    # Phase-1 rewriter overwrites body + touches updated — but must keep
    # salience + anchor intact.
    store.write(pid, "v2 body", tag="knowledge")
    got = store.read(pid)
    assert got["text"] == "v2 body"
    fm = got["_raw_frontmatter"]
    assert fm.get("salience") == 0.8
    assert fm.get("anchor") is True


def test_rewrite_preserves_created_timestamp(store):
    pid = "cafe0000-bbbb-cccc-dddd-000000000002"
    store.write(pid, "v1")
    first = store.read(pid)
    created1 = first["created"]
    store.write(pid, "v2")
    second = store.read(pid)
    # created stays; updated changes
    assert second["created"] == created1
    assert second["updated"] >= first["updated"]


# ── Delete ────────────────────────────────────────────────────────────


def test_delete_removes_file_and_empty_shard(store):
    pid = "00112233-4455-6677-8899-aabbccddeeff"
    p = store.write(pid, "temp")
    shard_dir = Path(p).parent
    assert p.exists()
    assert store.delete(pid) is True
    assert not p.exists()
    # Only one file was in the shard — shard dir should be gone
    assert not shard_dir.exists()


def test_delete_missing_returns_false(store):
    assert store.delete("not-a-real-id") is False


# ── iter_all ──────────────────────────────────────────────────────────


def test_iter_all_lists_every_written_id(store):
    ids = [
        "aaaa0000-1111-2222-3333-444444444444",
        "bbbb0000-5555-6666-7777-888888888888",
        "cccc0000-9999-aaaa-bbbb-cccccccccccc",
    ]
    for pid in ids:
        store.write(pid, f"body {pid}")
    listed = store.iter_all()
    assert sorted(listed) == sorted(ids)


def test_iter_all_returns_empty_on_fresh_store(store):
    assert store.iter_all() == []


# ── Integrity block ───────────────────────────────────────────────────


def test_integrity_block_refuses_write_file_under_memories_dir(qwe_temp_data_dir):
    """The agent's write_file tool can't touch the markdown memory store.

    Teardown is critical here — we reload tools to repopulate the
    whitelist against the tempdir, and if we don't null the cache on
    exit, the NEXT test (without this fixture) reads a stale whitelist
    pointing at a deleted tempdir and every write-path check fails.
    """
    import importlib
    import sys
    for m in ("config", "tools"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    tools = sys.modules["tools"]
    tools._WRITE_WHITELIST = None  # force recompute

    try:
        import config
        target = config.DATA_DIR / "memories" / "atoms" / "aa" / "evil.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(PermissionError, match="markdown memory store"):
            tools._resolve_path(str(target), for_write=True)
    finally:
        # Re-null the cache so later tests compute whitelist from the
        # restored config rather than the (now-deleted) tempdir paths.
        tools._WRITE_WHITELIST = None
        try:
            importlib.reload(sys.modules["tools"])
        except Exception:
            pass


# ── Dual-write integration (memory.save → both Qdrant and markdown) ───


def test_memory_save_writes_markdown_companion(qwe_temp_data_dir, monkeypatch):
    """memory.save() saves to Qdrant (mocked) AND writes the .md.

    We don't bring up a real Qdrant — monkeypatch _save_single to
    return a canned id after calling through to the markdown writer.
    """
    import importlib
    import sys
    for m in ("config", "memory_store", "memory"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    memory = sys.modules["memory"]
    store = sys.modules["memory_store"]

    # Fake the Qdrant-touching portion but keep the companion write call
    canned_id = "11112222-3333-4444-5555-666677778888"

    def _fake_save_single(text, tag="general", dedup=True,
                           thread_id=None, meta=None, synthesis_status="skip"):
        # Exercise the same companion-writer memory uses
        memory._write_markdown_companion(canned_id, text, tag, thread_id, meta)
        return canned_id

    monkeypatch.setattr(memory, "_save_single", _fake_save_single)

    returned = memory.save("fact: ducks are birds", tag="knowledge",
                            thread_id="t_test")
    assert returned == canned_id
    got = store.read(canned_id)
    assert got is not None
    assert got["text"] == "fact: ducks are birds"
    assert got["tag"] == "knowledge"
    assert got["thread_id"] == "t_test"


def test_memory_delete_removes_markdown_companion(qwe_temp_data_dir, monkeypatch):
    import importlib
    import sys
    for m in ("config", "memory_store", "memory"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    memory = sys.modules["memory"]
    store = sys.modules["memory_store"]

    pid = "99998888-7777-6666-5555-444433332222"
    store.write(pid, "to be deleted")
    assert store.read(pid) is not None

    # Stub the Qdrant side of memory.delete so we don't need a live backend
    class _FakeQc:
        def delete(self, *a, **kw): pass
    monkeypatch.setattr(memory, "_get_qdrant", lambda: _FakeQc())
    import db

    def _fake_fts_delete(*a, **kw):
        return None
    monkeypatch.setattr(db, "fts_delete", _fake_fts_delete, raising=False)

    assert memory.delete(pid) is True
    assert store.read(pid) is None  # md file gone too
