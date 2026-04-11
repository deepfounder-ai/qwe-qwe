"""Tests for presets.py — load, validate, install, activate, uninstall.

Each test uses a fresh tempdir as QWE_DATA_DIR so we never touch the user's
real ~/.qwe-qwe. Modules are reloaded per-test-class to pick up the temp path.
"""

import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixture helpers ─────────────────────────────────────────────────────

MINIMAL_MANIFEST = {
    "schema_version": 1,
    "id": "test-preset",
    "name": "Test Preset",
    "category": "testing",
    "version": "0.1.0",
    "author": {"name": "Test Author"},
    "license": {"type": "free"},
    "description": {
        "short": "A minimal preset used by the test suite",
        "long": "Just enough fields to pass schema validation and exercise the installer.",
        "language": "en",
    },
    "soul": {
        "agent_name": "TestBot",
        "language": "en",
        "traits": {
            "humor": "low",
            "honesty": "high",
            "curiosity": "moderate",
            "brevity": "high",
            "formality": "high",
            "proactivity": "moderate",
            "empathy": "moderate",
            "creativity": "low",
        },
    },
    "system_prompt": {"path": "system_prompt.md"},
    "compatibility": {"qwe_qwe_version": ">=0.1.0"},
}


def _write_fixture(base: Path, manifest: dict | None = None) -> Path:
    """Create a minimal valid preset directory at base/<id>/."""
    manifest = manifest or dict(MINIMAL_MANIFEST)
    preset_id = manifest["id"]
    d = base / preset_id
    d.mkdir(parents=True, exist_ok=True)
    import yaml
    (d / "preset.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    (d / "system_prompt.md").write_text(
        "You are TestBot. Be concise and helpful.\n",
        encoding="utf-8",
    )
    (d / "README.md").write_text("# Test preset\n", encoding="utf-8")
    return d


def _zip_fixture(preset_dir: Path, archive_path: Path) -> Path:
    """Zip a preset directory into a .qwp archive."""
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in preset_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(preset_dir))
    return archive_path


# ── Environment isolation ──────────────────────────────────────────────

_original_data_dir = None
_tmp_root: Path | None = None


def setup_module(module):
    """Point QWE_DATA_DIR at a fresh tempdir and reload config/db/presets."""
    global _original_data_dir, _tmp_root
    _original_data_dir = os.environ.get("QWE_DATA_DIR")
    _tmp_root = Path(tempfile.mkdtemp(prefix="qwe_preset_test_"))
    os.environ["QWE_DATA_DIR"] = str(_tmp_root)
    _reload_modules()


def teardown_module(module):
    global _tmp_root
    if _original_data_dir is not None:
        os.environ["QWE_DATA_DIR"] = _original_data_dir
    else:
        os.environ.pop("QWE_DATA_DIR", None)
    if _tmp_root and _tmp_root.exists():
        shutil.rmtree(_tmp_root, ignore_errors=True)
    # Reload modules back to normal data dir so other tests don't see stale state
    _reload_modules()


def _reload_modules():
    """Fresh config + db + presets + soul import. Clears db connection state."""
    import importlib
    # Close + drop stale db connection if present
    if "db" in sys.modules:
        try:
            conn = getattr(sys.modules["db"]._local, "conn", None)
            if conn is not None:
                conn.close()
            sys.modules["db"]._local.conn = None
            sys.modules["db"]._migrated = False
        except Exception:
            pass
    # Explicit order: config → db → soul → presets
    # (soul imports db at module load; presets imports soul lazily)
    for mod in ("config", "db", "soul", "presets"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


def _reset_db():
    """Delete installed presets + active marker + on-disk files between tests."""
    import db
    import config
    db.execute("DELETE FROM presets", ())
    db.execute("DELETE FROM kv WHERE key IN ('active_preset', 'soul_backup')", ())
    # Nuke everything under PRESETS_DIR so _tmp_root stays clean across tests
    if config.PRESETS_DIR.exists():
        for child in config.PRESETS_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except Exception:
                    pass


# ── Tests ──────────────────────────────────────────────────────────────

def test_load_directory():
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        assert info.id == "test-preset"
        assert info.version == "0.1.0"
        assert info.name == "Test Preset"
        assert info.category == "testing"
        assert info.source_kind == "directory"


def test_load_archive():
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "test-preset.qwp"
        _zip_fixture(src, archive)
        info = presets.load_archive(archive)
        assert info.id == "test-preset"
        assert info.source_kind == "archive"
        # After loading, the temp extract dir should contain the manifest
        assert (info.source_dir / "preset.yaml").exists()


def test_validate_ok():
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        errors = presets.validate(info)
        assert errors == [], f"Expected no errors, got: {errors}"


def test_validate_bad_schema():
    """Missing required top-level field is caught."""
    import presets
    _reset_db()
    bad = dict(MINIMAL_MANIFEST)
    del bad["compatibility"]
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=bad)
        info = presets.load_directory(src)
        errors = presets.validate(info)
        assert errors, "Expected validation errors for missing compatibility"
        assert any("compatibility" in e.lower() for e in errors)


def test_validate_missing_file():
    """Referenced system_prompt.md missing → error."""
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        (src / "system_prompt.md").unlink()
        info = presets.load_directory(src)
        errors = presets.validate(info)
        assert any("system_prompt" in e for e in errors), f"Got: {errors}"


def test_install_and_list():
    import presets
    import db
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        result = presets.install(info)
        assert result["id"] == "test-preset"
        assert Path(result["path"]).is_dir()
        # DB row
        items = presets.list_installed()
        assert len(items) == 1
        assert items[0]["id"] == "test-preset"
        assert items[0]["name"] == "Test Preset"
        assert items[0]["active"] is False


def test_install_already_installed_raises():
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info1 = presets.load_directory(src)
        presets.install(info1)
        info2 = presets.load_directory(src)
        try:
            presets.install(info2)
            assert False, "Expected FileExistsError"
        except FileExistsError:
            pass
        # Overwrite succeeds
        info3 = presets.load_directory(src)
        presets.install(info3, overwrite=True)


def test_uninstall_removes_everything():
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        presets.install(info)
        target_dir = presets.preset_dir("test-preset")
        assert target_dir.is_dir()

        presets.uninstall("test-preset")
        assert not target_dir.exists()
        assert presets.list_installed() == []


def test_activate_backs_up_and_applies_soul():
    import presets
    import soul
    import db
    _reset_db()
    # Set an initial distinctive soul
    soul.save("name", "OriginalBot")
    soul.save("humor", "high")

    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        presets.install(info)
        presets.activate("test-preset")

        # Active marker + backup present
        assert presets.get_active() == "test-preset"
        backup_raw = db.kv_get("soul_backup")
        assert backup_raw
        backup = json.loads(backup_raw)
        assert backup["name"] == "OriginalBot"
        assert backup["humor"] == "high"

        # Current soul reflects preset
        current = soul.load()
        assert current["name"] == "TestBot"
        assert current["humor"] == "low"
        assert current["brevity"] == "high"


def test_deactivate_restores_soul():
    import presets
    import soul
    import db
    _reset_db()
    soul.save("name", "OriginalBot")
    soul.save("brevity", "low")

    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        presets.install(info)
        presets.activate("test-preset")

        # Confirm mutation
        assert soul.load()["brevity"] == "high"

        presets.deactivate()
        assert presets.get_active() is None
        assert db.kv_get("soul_backup") in (None, "")

        restored = soul.load()
        assert restored["name"] == "OriginalBot"
        assert restored["brevity"] == "low"


def test_single_active_constraint():
    """Activating B while A is active deactivates A first."""
    import presets
    import soul
    _reset_db()
    soul.save("name", "OriginalBot")

    with tempfile.TemporaryDirectory() as tmp:
        a = _write_fixture(Path(tmp) / "a-root", manifest={**MINIMAL_MANIFEST, "id": "preset-a"})
        b = _write_fixture(Path(tmp) / "b-root", manifest={**MINIMAL_MANIFEST, "id": "preset-b"})
        presets.install(presets.load_directory(a))
        presets.install(presets.load_directory(b))

        presets.activate("preset-a")
        assert presets.get_active() == "preset-a"

        presets.activate("preset-b")
        assert presets.get_active() == "preset-b"

        # Soul still has been replaced but the original backup chain should
        # have restored back to OriginalBot when A was deactivated, then
        # overwritten again by B's soul.
        current_name = soul.load()["name"]
        assert current_name == "TestBot"  # both presets share agent_name

        presets.deactivate()
        assert soul.load()["name"] == "OriginalBot"


def test_system_prompt_suffix_wiring():
    """get_system_prompt_suffix returns the preset's prompt text when active."""
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        presets.install(info)

        # Not active → empty
        assert presets.get_system_prompt_suffix() == ""

        presets.activate("test-preset")
        text = presets.get_system_prompt_suffix()
        assert "TestBot" in text

        presets.deactivate()
        assert presets.get_system_prompt_suffix() == ""


def test_active_skills_dir_wiring():
    """Skills discovery hook returns the active preset skills dir."""
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        # Add a skills dir with one module
        (src / "skills").mkdir()
        (src / "skills" / "domain_tool.py").write_text(
            'DESCRIPTION = "demo"\nTOOLS = []\ndef execute(name, args): return ""\n',
            encoding="utf-8",
        )
        info = presets.load_directory(src)
        presets.install(info)

        # Not active → None
        assert presets.get_active_skills_dir() is None

        presets.activate("test-preset")
        active_dir = presets.get_active_skills_dir()
        assert active_dir is not None
        assert (active_dir / "domain_tool.py").exists()


# ── Security-focused tests (added in v0.12.1) ─────────────────────────

def test_id_regex_rejects_traversal():
    """preset_dir/uninstall/activate must reject ids that would escape."""
    import presets
    _reset_db()
    for bad in ("../evil", "foo/bar", "foo bar", "Foo", "foo_bar", "",
                "../../etc/passwd", "foo\\bar"):
        try:
            presets.preset_dir(bad)
        except ValueError:
            pass
        else:
            assert False, f"preset_dir accepted bad id {bad!r}"
        try:
            presets.uninstall(bad)
        except ValueError:
            pass
        else:
            assert False, f"uninstall accepted bad id {bad!r}"


def test_uninstall_missing_id_is_noop():
    """uninstall for a valid-format but unregistered id does NOT touch disk."""
    import presets
    import config
    _reset_db()
    # Create a sentinel file under PRESETS_DIR to prove we never walk it
    sentinel = config.PRESETS_DIR / "not-installed" / "sentinel.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("must not be removed", encoding="utf-8")
    try:
        presets.uninstall("not-installed")  # no DB row → no-op
        assert sentinel.exists(), "uninstall touched disk for unregistered id"
    finally:
        shutil.rmtree(sentinel.parent, ignore_errors=True)


def test_zip_rejects_absolute_path():
    """A zip member with a leading '/' or drive letter must fail fast."""
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "malicious.qwp"
        with zipfile.ZipFile(archive, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src))
            # Inject an evil member
            zf.writestr("/etc/passwd.txt", "pwned")
        try:
            presets.load_archive(archive)
            assert False, "expected ValueError for absolute-path member"
        except ValueError as e:
            assert "unsafe" in str(e).lower() or "absolute" in str(e).lower(), str(e)


def test_zip_rejects_parent_ref():
    """A zip member with `..` must fail fast."""
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "malicious.qwp"
        with zipfile.ZipFile(archive, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src))
            zf.writestr("../escape.txt", "pwned")
        try:
            presets.load_archive(archive)
            assert False, "expected ValueError for `..` member"
        except ValueError:
            pass


def test_zip_bomb_guard():
    """A zip whose members claim huge uncompressed sizes is rejected."""
    import presets
    _reset_db()
    # Build a zip with a bogus ZipInfo claiming huge size.
    # Simplest: write lots of small files until > MAX_EXTRACT_FILES (2000)
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "toomany.qwp"
        with zipfile.ZipFile(archive, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src))
            for i in range(presets._MAX_EXTRACT_FILES + 5):
                zf.writestr(f"filler_{i}.txt", "x")
        try:
            presets.load_archive(archive)
            assert False, "expected ValueError for too many files"
        except ValueError as e:
            assert "too many files" in str(e).lower() or "cap" in str(e).lower(), str(e)


def test_manifest_path_traversal_rejected():
    """system_prompt.path referencing outside the preset dir is a hard error."""
    import presets
    _reset_db()
    bad_manifest = dict(MINIMAL_MANIFEST)
    bad_manifest["system_prompt"] = {"path": "../../../outside.md"}
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=bad_manifest)
        # Create an actual file outside the preset to make the traversal
        # "succeed" on the filesystem level — so only our check blocks it.
        outside = Path(tmp) / "outside.md"
        outside.write_text("SECRET", encoding="utf-8")
        info = presets.load_directory(src)
        errors = presets.validate(info)
        assert any("escape" in e.lower() or "not found" in e.lower() for e in errors), errors


def test_manifest_absolute_path_rejected():
    """Absolute path in a manifest field must be rejected."""
    import presets
    _reset_db()
    bad = dict(MINIMAL_MANIFEST)
    # Pick an OS-appropriate absolute path
    if os.name == "nt":
        bad["system_prompt"] = {"path": "C:\\Windows\\System32\\notepad.exe"}
    else:
        bad["system_prompt"] = {"path": "/etc/passwd"}
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=bad)
        info = presets.load_directory(src)
        errors = presets.validate(info)
        assert any("absolute" in e.lower() or "escape" in e.lower() for e in errors), errors


def test_malicious_skill_rejected():
    """A preset whose skill file is syntactically invalid must fail install."""
    import presets
    _reset_db()
    manifest = json.loads(json.dumps(MINIMAL_MANIFEST))
    manifest["skills"] = {
        "custom": [
            {"path": "skills/broken.py", "name": "broken", "description": "bad"}
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=manifest)
        (src / "skills").mkdir()
        (src / "skills" / "broken.py").write_text(
            "this is not valid python $$$\n",
            encoding="utf-8",
        )
        info = presets.load_directory(src)
        errors = presets.validate(info)
        assert any("broken.py" in e.lower() for e in errors), errors


def test_activate_rollback_on_failure(monkeypatch=None):
    """If _apply_soul_from_manifest raises, soul + backup are restored."""
    import presets
    import soul
    import db
    _reset_db()
    soul.save("name", "PreActivate")
    soul.save("humor", "moderate")

    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)
        presets.install(info)

        # Monkey-patch _apply_soul_from_manifest to explode mid-way
        original_apply = presets._apply_soul_from_manifest

        def _boom(manifest):
            # Partially apply (name only), then raise
            soul.save("name", "HalfApplied")
            raise RuntimeError("simulated activation failure")

        presets._apply_soul_from_manifest = _boom
        try:
            try:
                presets.activate("test-preset")
                assert False, "expected activation to raise"
            except RuntimeError:
                pass
        finally:
            presets._apply_soul_from_manifest = original_apply

        # Active marker should NOT be set
        assert presets.get_active() is None
        # soul_backup should be cleared
        assert not db.kv_get("soul_backup")
        # Soul should be restored to pre-activation state
        s = soul.load()
        assert s["name"] == "PreActivate", f"soul not rolled back: {s['name']}"


def test_install_partial_copy_no_ghost_row():
    """If copytree fails, no DB row is written and target dir is cleaned."""
    import presets
    import db
    _reset_db()

    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        info = presets.load_directory(src)

        original_copytree = shutil.copytree

        def _fail_copy(src_, dst_, *a, **kw):
            # Partial copy: create the dir then fail
            Path(dst_).mkdir(parents=True, exist_ok=True)
            (Path(dst_) / "partial.txt").write_text("x")
            raise OSError("simulated copy failure")

        shutil.copytree = _fail_copy
        try:
            try:
                presets.install(info)
                assert False, "expected copy to fail"
            except OSError:
                pass
        finally:
            shutil.copytree = original_copytree

        # No DB row, no target dir
        assert db.fetchone("SELECT id FROM presets WHERE id = ?", ("test-preset",)) is None
        assert not presets.preset_dir("test-preset").exists()


def test_install_cleans_tempdir_on_validation_failure():
    """A .qwp with a broken manifest must not leak its extract tempdir."""
    import presets
    _reset_db()
    before = set(Path(tempfile.gettempdir()).glob("qwe_preset_*"))

    # Build a zip whose manifest is missing a required field
    bad = dict(MINIMAL_MANIFEST)
    del bad["compatibility"]
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=bad)
        archive = Path(tmp) / "bad.qwp"
        with zipfile.ZipFile(archive, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src))
        info = presets.load_archive(archive)
        try:
            presets.install(info)
            assert False, "expected install to raise on bad manifest"
        except ValueError:
            pass

    # No new qwe_preset_ tempdir should be left behind
    after = set(Path(tempfile.gettempdir()).glob("qwe_preset_*"))
    new = after - before
    assert not new, f"tempdir leaked after failed install: {new}"


# ── Manual runner (we lack pytest in the venv) ─────────────────────────

if __name__ == "__main__":
    setup_module(None)
    tests = [
        test_load_directory,
        test_load_archive,
        test_validate_ok,
        test_validate_bad_schema,
        test_validate_missing_file,
        test_install_and_list,
        test_install_already_installed_raises,
        test_uninstall_removes_everything,
        test_activate_backs_up_and_applies_soul,
        test_deactivate_restores_soul,
        test_single_active_constraint,
        test_system_prompt_suffix_wiring,
        test_active_skills_dir_wiring,
        # security-focused v0.12.1 tests
        test_id_regex_rejects_traversal,
        test_uninstall_missing_id_is_noop,
        test_zip_rejects_absolute_path,
        test_zip_rejects_parent_ref,
        test_zip_bomb_guard,
        test_manifest_path_traversal_rejected,
        test_manifest_absolute_path_rejected,
        test_malicious_skill_rejected,
        test_activate_rollback_on_failure,
        test_install_partial_copy_no_ghost_row,
        test_install_cleans_tempdir_on_validation_failure,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except Exception as e:
            failures += 1
            print(f"  FAIL {fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
    teardown_module(None)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
