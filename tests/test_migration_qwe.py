"""Tests for _migrate_from_qwe_qwe() in config.py."""
import json
from pathlib import Path

import config


def _build_old_dir(base: Path) -> Path:
    """Create a realistic fake ~/.qwe-qwe/ directory."""
    old = base / ".qwe-qwe"
    (old / "memory" / "collection" / "qwe_qwe").mkdir(parents=True)
    (old / "memory" / "collection" / "qwe_rag").mkdir(parents=True)
    (old / "memory" / "collection" / "qwe_qwe" / "storage.sqlite").write_bytes(b"x" * 50_000)
    (old / "memory" / "collection" / "qwe_rag" / "storage.sqlite").write_bytes(b"y" * 12_288)
    (old / "qwe_qwe.db").write_bytes(b"z" * 100_000)
    (old / "qwe_qwe.db-wal").write_bytes(b"w" * 1_000)
    (old / "uploads" / "kb").mkdir(parents=True)
    (old / "uploads" / "kb" / "note.md").write_text("hello knowledge")
    (old / "workspace").mkdir()
    (old / "wiki").mkdir()
    (old / "wiki" / "page.md").write_text("wiki content")
    (old / "skills").mkdir()
    (old / "skills" / "my_tool.py").write_text("# custom")
    (old / "skills" / "skill_creator.py").write_text("# builtin")
    return old


def test_full_migration(tmp_path, monkeypatch):
    _build_old_dir(tmp_path)
    new_dir = tmp_path / ".castor"
    new_dir.mkdir()
    skills_dir = new_dir / "skills"
    skills_dir.mkdir()

    monkeypatch.setattr(config, "DATA_DIR", new_dir)
    monkeypatch.setattr(config, "USER_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    config._migrate_from_qwe_qwe()

    # Database
    assert (new_dir / "castor.db").stat().st_size == 100_000
    assert (new_dir / "castor.db-wal").exists()

    # Qdrant collections
    coll = new_dir / "memory" / "collection"
    assert (coll / "castor" / "storage.sqlite").stat().st_size == 50_000
    assert (coll / "castor_rag" / "storage.sqlite").exists()

    # meta.json has both collections registered
    meta = json.loads((new_dir / "memory" / "meta.json").read_text())
    assert "castor" in meta["collections"]
    assert "castor_rag" in meta["collections"]
    assert meta["collections"]["castor"]["vectors"]["dense"]["size"] == 384

    # User data
    assert (new_dir / "uploads" / "kb" / "note.md").read_text() == "hello knowledge"
    assert (new_dir / "wiki" / "page.md").exists()

    # Skills: custom copied, builtin skipped
    assert (skills_dir / "my_tool.py").exists()
    assert not (skills_dir / "skill_creator.py").exists()

    # Marker written
    assert (new_dir / ".migrated_from_qwe_qwe").exists()


def test_no_old_dir_writes_marker(tmp_path, monkeypatch):
    """If ~/.qwe-qwe/ doesn't exist, marker is written and nothing errors."""
    new_dir = tmp_path / ".castor"
    new_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", new_dir)
    monkeypatch.setattr(config, "USER_SKILLS_DIR", new_dir / "skills")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    config._migrate_from_qwe_qwe()

    assert (new_dir / ".migrated_from_qwe_qwe").read_text() == "no source\n"


def test_idempotent(tmp_path, monkeypatch):
    """Second call is a no-op when marker exists."""
    _build_old_dir(tmp_path)
    new_dir = tmp_path / ".castor"
    new_dir.mkdir()
    (new_dir / ".migrated_from_qwe_qwe").write_text("done\n")

    monkeypatch.setattr(config, "DATA_DIR", new_dir)
    monkeypatch.setattr(config, "USER_SKILLS_DIR", new_dir / "skills")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    config._migrate_from_qwe_qwe()

    # castor.db must NOT be created (migration was skipped)
    assert not (new_dir / "castor.db").exists()


def test_no_overwrite_larger_db(tmp_path, monkeypatch):
    """Existing castor.db larger than qwe_qwe.db is preserved."""
    _build_old_dir(tmp_path)
    new_dir = tmp_path / ".castor"
    new_dir.mkdir()
    existing_db = new_dir / "castor.db"
    existing_db.write_bytes(b"A" * 200_000)  # larger than old (100_000)
    (new_dir / "skills").mkdir()

    monkeypatch.setattr(config, "DATA_DIR", new_dir)
    monkeypatch.setattr(config, "USER_SKILLS_DIR", new_dir / "skills")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    config._migrate_from_qwe_qwe()

    assert existing_db.stat().st_size == 200_000  # unchanged
