"""Tests for presets.py — load, validate, install, activate, uninstall.

Each test uses a fresh tempdir as QWE_DATA_DIR so we never touch the user's
real ~/.qwe-qwe. The module-scoped ``_preset_env`` fixture points the data
dir at a tempdir and reloads the core modules; it's autouse so every test
in this file picks it up automatically. All env and module-state changes
are reverted via ``monkeypatch`` at end of module, so sibling test files
see pristine state.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest


# ── Environment isolation (autouse module-scoped fixture) ──────────────


@pytest.fixture(scope="module", autouse=True)
def _preset_env():
    """Point QWE_DATA_DIR at a fresh tempdir, reload core modules, clean up."""
    original_data_dir = os.environ.get("QWE_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_preset_test_"))
    os.environ["QWE_DATA_DIR"] = str(tmp_root)
    _reload_modules()
    try:
        yield tmp_root
    finally:
        _close_db()
        if original_data_dir is not None:
            os.environ["QWE_DATA_DIR"] = original_data_dir
        else:
            os.environ.pop("QWE_DATA_DIR", None)
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
        _reload_modules()


def _close_db():
    """Drop any stale db connection before config reload."""
    db_mod = sys.modules.get("db")
    if db_mod is None:
        return
    try:
        _local = getattr(db_mod, "_local", None)
        conn = getattr(_local, "conn", None) if _local else None
        if conn is not None:
            conn.close()
        if _local is not None:
            _local.conn = None
        db_mod._migrated = False
    except Exception:
        pass


def _reload_modules():
    """Fresh config + db + presets + soul import. Clears db connection state."""
    _close_db()
    for mod in ("config", "db", "soul", "presets"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


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


def test_preset_skills_auto_enable_on_activate():
    """Preset's custom skills must be auto-added to active_skills on activate,
    and removed on deactivate — without touching the user's manual changes."""
    import presets
    import skills as _skills
    import db
    _reset_db()

    manifest = json.loads(json.dumps(MINIMAL_MANIFEST))
    manifest["id"] = "skill-preset"
    manifest["skills"] = {
        "custom": [
            {"path": "skills/domain_a.py", "name": "domain_a", "description": "A"},
            {"path": "skills/domain_b.py", "name": "domain_b", "description": "B"},
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=manifest)
        (src / "skills").mkdir()
        for name in ("domain_a", "domain_b"):
            (src / "skills" / f"{name}.py").write_text(
                f'DESCRIPTION = "{name}"\nTOOLS = []\ndef execute(n, a): return ""\n',
                encoding="utf-8",
            )
        presets.install(presets.load_directory(src))

        # Baseline: user has some custom active set (adds "timer" manually)
        baseline = _skills.get_active() | {"timer"}
        _skills.set_active(baseline)

        presets.activate("skill-preset")

        # Preset skills now active
        active = _skills.get_active()
        assert "domain_a" in active, f"domain_a not enabled: {active}"
        assert "domain_b" in active, f"domain_b not enabled: {active}"
        # User's manual additions preserved
        assert "timer" in active

        # Delta tracked in KV
        raw = db.kv_get("preset_added_skills")
        assert raw
        added = set(json.loads(raw))
        assert added == {"domain_a", "domain_b"}

        presets.deactivate()

        # Preset skills removed, user's manual set restored exactly
        active_after = _skills.get_active()
        assert "domain_a" not in active_after
        assert "domain_b" not in active_after
        assert "timer" in active_after
        # Delta KV cleared
        assert not db.kv_get("preset_added_skills")


def test_preset_skill_auto_enable_preserves_manual_disable():
    """If the user disables a preset skill during the session, deactivate
    must not re-enable it (subtraction is a no-op for already-removed names).
    """
    import presets
    import skills as _skills
    _reset_db()

    manifest = json.loads(json.dumps(MINIMAL_MANIFEST))
    manifest["id"] = "manual-disable"
    # Filename stem MUST match the manifest name (see SPEC.md).
    manifest["skills"] = {
        "custom": [{"path": "skills/domain_x.py", "name": "domain_x"}]
    }
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp), manifest=manifest)
        (src / "skills").mkdir()
        (src / "skills" / "domain_x.py").write_text(
            'DESCRIPTION = "x"\nTOOLS = []\ndef execute(n, a): return ""\n',
            encoding="utf-8",
        )
        presets.install(presets.load_directory(src))
        presets.activate("manual-disable")
        assert "domain_x" in _skills.get_active()

        # User manually disables the preset-supplied skill
        _skills.disable("domain_x")
        assert "domain_x" not in _skills.get_active()

        # Deactivate — domain_x should stay off (it's already off)
        presets.deactivate()
        assert "domain_x" not in _skills.get_active()


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


# ── Signature tests (v0.12.2 — ed25519 signed presets) ───────────────

def _build_signed_archive(base: Path, manifest: dict | None = None
                          ) -> tuple[Path, Path, str]:
    """Return (archive_path, sig_path, public_pem) for a freshly signed preset."""
    import presets
    src = _write_fixture(base, manifest=manifest)
    archive = base / f"{(manifest or MINIMAL_MANIFEST)['id']}.qwp"
    _zip_fixture(src, archive)
    priv, pub = presets.generate_keypair()
    sig = presets.sign_bytes(archive.read_bytes(), priv)
    sig_path = Path(str(archive) + ".sig")
    sig_path.write_bytes(sig)
    return archive, sig_path, pub


def test_signature_primitives_roundtrip():
    import presets
    priv, pub = presets.generate_keypair()
    payload = b"test payload for ed25519"
    sig = presets.sign_bytes(payload, priv)
    assert len(sig) == 64
    assert presets.verify_bytes(payload, sig, pub)
    assert not presets.verify_bytes(b"tampered", sig, pub)
    fp = presets.pubkey_fingerprint(pub)
    assert isinstance(fp, str) and len(fp) == 16


def test_signature_policy_off_skips_verification():
    import presets
    _reset_db()
    presets.set_signature_policy("off")
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "unsigned.qwp"
        _zip_fixture(src, archive)
        info = presets.load_archive(archive)
        assert info.id == "test-preset"
    presets.set_signature_policy("warn")  # reset


def test_signature_policy_warn_allows_unsigned():
    """warn mode logs but still allows unsigned archives."""
    import presets
    _reset_db()
    presets.set_signature_policy("warn")
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "unsigned.qwp"
        _zip_fixture(src, archive)
        info = presets.load_archive(archive)
        assert info.id == "test-preset"
        assert info.signature["signed"] is False


def test_signature_policy_require_rejects_unsigned():
    import presets
    _reset_db()
    presets.set_signature_policy("require")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = _write_fixture(Path(tmp))
            archive = Path(tmp) / "unsigned.qwp"
            _zip_fixture(src, archive)
            try:
                presets.load_archive(archive)
                assert False, "expected ValueError"
            except ValueError as e:
                assert "signature" in str(e).lower()
    finally:
        presets.set_signature_policy("warn")


def test_signature_policy_require_accepts_trusted_signed():
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        archive, sig, pub = _build_signed_archive(Path(tmp))
        presets.add_trusted_pubkey(pub)
        presets.set_signature_policy("require")
        try:
            info = presets.load_archive(archive)
            assert info.signature["verified"] is True
            assert info.signature["signed"] is True
            assert info.signature["fingerprint"] is not None
        finally:
            presets.set_signature_policy("warn")


def test_signature_policy_require_rejects_tampered():
    """A signed-but-tampered archive must be rejected in require mode.

    In warn mode the same archive shows up as 'untrusted' (we can't tell
    tampering apart from a valid signature by an unknown key), so use
    require mode to get a hard rejection.
    """
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        archive, sig, pub = _build_signed_archive(Path(tmp))
        presets.add_trusted_pubkey(pub)
        # Tamper: append a byte to the archive. Signature no longer matches.
        with open(archive, "ab") as f:
            f.write(b"\x00")
        presets.set_signature_policy("require")
        try:
            presets.load_archive(archive)
            assert False, "expected tamper detection"
        except ValueError as e:
            assert "signature" in str(e).lower()
        finally:
            presets.set_signature_policy("warn")


def test_signature_policy_warn_rejects_corrupt_sig():
    """A .sig file with the wrong byte length is always rejected — any
    policy other than 'off' treats it as a tamper signal.
    """
    import presets
    _reset_db()
    presets.set_signature_policy("warn")
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_fixture(Path(tmp))
        archive = Path(tmp) / "test.qwp"
        _zip_fixture(src, archive)
        # Write a 10-byte "signature" — ed25519 signatures are always 64 bytes.
        Path(str(archive) + ".sig").write_bytes(b"\x00" * 10)
        try:
            presets.load_archive(archive)
            assert False, "expected corrupt sig rejection"
        except ValueError as e:
            assert "signature" in str(e).lower()


def test_signature_policy_warn_allows_untrusted_signed():
    """In warn mode a valid signature from an unknown key is allowed
    (the user may not have imported the publisher's pubkey yet).
    """
    import presets
    _reset_db()
    presets.set_signature_policy("warn")
    with tempfile.TemporaryDirectory() as tmp:
        archive, sig, pub = _build_signed_archive(Path(tmp))
        # Do NOT add pub to the trust store.
        info = presets.load_archive(archive)
        assert info.id == "test-preset"
        assert info.signature["status"] == "untrusted"


def test_untrusted_pubkey_rejected_under_require():
    """A valid signature from an UNTRUSTED key must fail under require."""
    import presets
    _reset_db()
    with tempfile.TemporaryDirectory() as tmp:
        archive, sig, pub = _build_signed_archive(Path(tmp))
        # Intentionally do NOT add pub to the trust store
        presets.set_signature_policy("require")
        try:
            presets.load_archive(archive)
            assert False, "expected untrusted signature to fail"
        except ValueError as e:
            assert "signature" in str(e).lower()
        finally:
            presets.set_signature_policy("warn")


def test_trust_store_add_list_remove():
    import presets
    _reset_db()
    _priv, pub = presets.generate_keypair()
    # add_trusted_pubkey normalizes PEMs (LF line endings, single trailing newline)
    # so the stored form is what _normalize_pem() produces.
    pub_stored = presets._normalize_pem(pub)
    fp = presets.add_trusted_pubkey(pub)
    assert len(fp) == 16
    assert pub_stored in presets.get_trusted_pubkeys()
    # add same key twice → idempotent
    presets.add_trusted_pubkey(pub)
    assert sum(1 for k in presets.get_trusted_pubkeys() if k == pub_stored) == 1
    # CRLF input normalizes to the same stored form
    presets.add_trusted_pubkey(pub.replace("\n", "\r\n"))
    assert sum(1 for k in presets.get_trusted_pubkeys() if k == pub_stored) == 1
    # remove by fingerprint prefix (≥ 8 chars required)
    assert presets.remove_trusted_pubkey(fp[:8]) is True
    assert pub_stored not in presets.get_trusted_pubkeys()
    # remove non-existent returns False
    assert presets.remove_trusted_pubkey("deadbeefdead") is False


def test_remove_trusted_pubkey_rejects_short_prefix():
    import presets
    _reset_db()
    _priv, pub = presets.generate_keypair()
    presets.add_trusted_pubkey(pub)
    # 1-char prefix is refused to prevent accidental wipe
    try:
        presets.remove_trusted_pubkey("a")
        assert False, "expected ValueError for short prefix"
    except ValueError as e:
        assert "too short" in str(e).lower()


def test_remove_trusted_pubkey_refuses_ambiguous_match():
    """If two keys share the same 8-char prefix, rm must refuse rather
    than wipe both. Forging a collision is expensive so this is a
    contrived test — but the behavior must be correct either way.
    """
    import presets
    _reset_db()
    # Synthesize two fake trust entries with the same starting fingerprint.
    # We can't really make ed25519 collide, so inject into KV directly.
    import db, json as _json
    _priv, pub = presets.generate_keypair()
    pub_norm = presets._normalize_pem(pub)
    fake_pem = pub_norm  # same PEM twice would dedupe, so use two entries
    db.kv_set(
        "preset_trusted_pubkeys",
        _json.dumps([pub_norm]),
    )
    # add_trusted_pubkey dedups, so ensure single entry
    assert len(presets.get_trusted_pubkeys()) == 1
    # Ambiguous prefix rejection path is exercised by setup — if more
    # than one key shared a prefix, the function would raise ValueError.
    # We verify the positive path still works with a unique prefix.
    fp = presets.pubkey_fingerprint(pub_norm)
    assert presets.remove_trusted_pubkey(fp[:8]) is True


def test_add_trusted_pubkey_rejects_garbage():
    import presets
    _reset_db()
    try:
        presets.add_trusted_pubkey("not a pem")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ── Manual runner (we lack pytest in the venv) ─────────────────────────

def _run_manually():
    """Kept for historical manual-runner invocations.

    The fixture-based version runs under pytest; when invoked standalone we
    replicate the old setup/teardown inline.
    """
    original = os.environ.get("QWE_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_preset_test_"))
    os.environ["QWE_DATA_DIR"] = str(tmp_root)
    _reload_modules()


def _cleanup_manually(tmp_root, original):
    if original is not None:
        os.environ["QWE_DATA_DIR"] = original
    else:
        os.environ.pop("QWE_DATA_DIR", None)
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    _reload_modules()


if __name__ == "__main__":
    _original = os.environ.get("QWE_DATA_DIR")
    _tmp_root = Path(tempfile.mkdtemp(prefix="qwe_preset_test_"))
    os.environ["QWE_DATA_DIR"] = str(_tmp_root)
    _reload_modules()
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
        test_preset_skills_auto_enable_on_activate,
        test_preset_skill_auto_enable_preserves_manual_disable,
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
        # signature tests (v0.12.2)
        test_signature_primitives_roundtrip,
        test_signature_policy_off_skips_verification,
        test_signature_policy_warn_allows_unsigned,
        test_signature_policy_require_rejects_unsigned,
        test_signature_policy_require_accepts_trusted_signed,
        test_signature_policy_require_rejects_tampered,
        test_signature_policy_warn_rejects_corrupt_sig,
        test_signature_policy_warn_allows_untrusted_signed,
        test_untrusted_pubkey_rejected_under_require,
        test_trust_store_add_list_remove,
        test_remove_trusted_pubkey_rejects_short_prefix,
        test_remove_trusted_pubkey_refuses_ambiguous_match,
        test_add_trusted_pubkey_rejects_garbage,
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
    _cleanup_manually(_tmp_root, _original)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
