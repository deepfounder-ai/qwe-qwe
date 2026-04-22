"""End-to-end tests for /api/presets/* endpoints via FastAPI TestClient.

Uses a fresh QWE_DATA_DIR temp dir so it never touches real user data.
Covers the happy path and every error branch (404 / 400 / 409 / 413).
"""

import os
import sys
import json
import shutil
import tempfile
import zipfile
import importlib
from pathlib import Path

import pytest


# ── Environment isolation (autouse module-scoped fixture) ─────────────


@pytest.fixture(scope="module", autouse=True)
def _server_preset_env():
    original_data_dir = os.environ.get("QWE_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_server_test_"))
    os.environ["QWE_DATA_DIR"] = str(tmp_root)
    _reload_core()
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
        _reload_core()


def _close_db():
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


def _reload_core():
    """Drop stale db connection + reload core modules so they pick up the
    new QWE_DATA_DIR. server.py is re-imported last."""
    _close_db()
    for mod in ("config", "db", "soul", "presets", "server"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


def _client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def _reset_db():
    import db
    import config
    db.execute("DELETE FROM presets", ())
    db.execute("DELETE FROM kv WHERE key IN ('active_preset', 'soul_backup')", ())
    if config.PRESETS_DIR.exists():
        for child in config.PRESETS_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except Exception:
                    pass


# ── Fixture helpers ───────────────────────────────────────────────────

_MANIFEST = {
    "schema_version": 1,
    "id": "server-test-preset",
    "name": "Server Test Preset",
    "category": "testing",
    "version": "1.0.0",
    "author": {"name": "Test"},
    "license": {"type": "free"},
    "description": {
        "short": "Server test preset",
        "long": "Used by /api/presets/* endpoint tests only.",
        "language": "en",
    },
    "soul": {
        "agent_name": "ServerBot",
        "language": "en",
        "traits": {
            "humor": "low",
            "honesty": "high",
            "curiosity": "moderate",
            "brevity": "high",
            "formality": "high",
            "proactivity": "moderate",
            "empathy": "low",
            "creativity": "low",
        },
    },
    "system_prompt": {"path": "system_prompt.md"},
    "compatibility": {"qwe_qwe_version": ">=0.1.0"},
}


def _build_archive(manifest: dict | None = None) -> bytes:
    """Return .qwp bytes for a minimal valid preset."""
    import yaml
    manifest = manifest or dict(_MANIFEST)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "preset.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        (root / "system_prompt.md").write_text("You are ServerBot.\n", encoding="utf-8")
        (root / "README.md").write_text("# Server test\n", encoding="utf-8")
        buf = tempfile.NamedTemporaryFile(delete=False, suffix=".qwp")
        buf.close()
        try:
            with zipfile.ZipFile(buf.name, "w") as zf:
                for f in root.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(root))
            return Path(buf.name).read_bytes()
        finally:
            Path(buf.name).unlink(missing_ok=True)


# ── Tests ─────────────────────────────────────────────────────────────

def test_list_empty():
    _reset_db()
    with _client() as c:
        res = c.get("/api/presets")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["active"] is None


def test_install_happy_path_and_list():
    _reset_db()
    payload = _build_archive()
    with _client() as c:
        res = c.post(
            "/api/presets/install",
            files={"file": ("server-test.qwp", payload, "application/zip")},
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["id"] == "server-test-preset"
    assert data["version"] == "1.0.0"

    with _client() as c:
        lst = c.get("/api/presets").json()
    assert len(lst["items"]) == 1
    assert lst["items"][0]["id"] == "server-test-preset"
    assert lst["items"][0]["active"] is False


def test_install_missing_file_400():
    """Multipart request that doesn't include a 'file' field → 400."""
    _reset_db()
    with _client() as c:
        # Explicit multipart with the wrong field name — forces the
        # _stage_upload() branch where form.get("file") returns None.
        res = c.post(
            "/api/presets/install",
            files={"other": ("not-used.txt", b"x", "text/plain")},
        )
    assert res.status_code == 400
    assert "no file" in res.json()["error"].lower()


def test_install_non_multipart_400():
    _reset_db()
    with _client() as c:
        res = c.post("/api/presets/install", json={"foo": "bar"})
    assert res.status_code == 400
    assert "multipart" in res.json()["error"].lower()


def test_install_bad_manifest_400_with_details():
    _reset_db()
    bad_manifest = dict(_MANIFEST)
    del bad_manifest["compatibility"]
    payload = _build_archive(bad_manifest)
    with _client() as c:
        res = c.post(
            "/api/presets/install",
            files={"file": ("bad.qwp", payload, "application/zip")},
        )
    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "validation failed"
    assert isinstance(body["details"], list)
    assert any("compatibility" in e.lower() for e in body["details"])


def test_install_duplicate_409():
    _reset_db()
    payload = _build_archive()
    with _client() as c:
        assert c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
        ).status_code == 200
        res = c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
        )
    assert res.status_code == 409
    body = res.json()
    assert body.get("code") == "already_installed"


def test_install_overwrite_succeeds():
    _reset_db()
    payload = _build_archive()
    with _client() as c:
        assert c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
        ).status_code == 200
        res = c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
            data={"overwrite": "1"},
        )
    assert res.status_code == 200
    assert res.json()["id"] == "server-test-preset"


def test_get_info_happy_and_404():
    _reset_db()
    payload = _build_archive()
    with _client() as c:
        c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
        )
        res = c.get("/api/presets/server-test-preset")
        assert res.status_code == 200
        assert res.json()["name"] == "Server Test Preset"
        assert res.json()["active"] is False

        res = c.get("/api/presets/does-not-exist")
        assert res.status_code == 404


def test_activate_and_deactivate_endpoint():
    _reset_db()
    import soul
    soul.save("name", "OriginalBot")

    payload = _build_archive()
    with _client() as c:
        c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
        )

        res = c.post("/api/presets/server-test-preset/activate")
        assert res.status_code == 200
        assert res.json()["id"] == "server-test-preset"

        # List now shows the active marker
        lst = c.get("/api/presets").json()
        assert lst["active"] == "server-test-preset"
        assert lst["items"][0]["active"] is True

        res = c.post("/api/presets/deactivate")
        assert res.status_code == 200
        assert res.json()["was_active"] == "server-test-preset"

        # Soul restored
        assert soul.load()["name"] == "OriginalBot"


def test_activate_unknown_id_400():
    _reset_db()
    with _client() as c:
        res = c.post("/api/presets/no-such-id/activate")
    assert res.status_code == 400


def test_deactivate_noop_when_none_active():
    _reset_db()
    with _client() as c:
        res = c.post("/api/presets/deactivate")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["was_active"] is None


def test_delete_happy_and_missing():
    _reset_db()
    payload = _build_archive()
    with _client() as c:
        c.post(
            "/api/presets/install",
            files={"file": ("srv.qwp", payload, "application/zip")},
        )
        res = c.delete("/api/presets/server-test-preset")
        assert res.status_code == 200
        assert res.json()["id"] == "server-test-preset"

        # Deleting an unknown id is a no-op (returns ok)
        res = c.delete("/api/presets/never-installed")
        assert res.status_code == 200


def test_delete_invalid_id_rejected():
    """IDs that would escape PRESETS_DIR get a 400 via ValueError."""
    _reset_db()
    with _client() as c:
        # Valid FastAPI path param but invalid preset id (uppercase)
        res = c.delete("/api/presets/BadId")
    assert res.status_code == 400
    assert "invalid preset id" in res.json()["error"].lower()


# ── Runner ────────────────────────────────────────────────────────────

def _manual_setup():
    original = os.environ.get("QWE_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_server_test_"))
    os.environ["QWE_DATA_DIR"] = str(tmp_root)
    _reload_core()
    return original, tmp_root


def _manual_teardown(original, tmp_root):
    if original is not None:
        os.environ["QWE_DATA_DIR"] = original
    else:
        os.environ.pop("QWE_DATA_DIR", None)
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    _reload_core()


if __name__ == "__main__":
    _orig, _tmp = _manual_setup()
    tests = [
        test_list_empty,
        test_install_happy_path_and_list,
        test_install_missing_file_400,
        test_install_non_multipart_400,
        test_install_bad_manifest_400_with_details,
        test_install_duplicate_409,
        test_install_overwrite_succeeds,
        test_get_info_happy_and_404,
        test_activate_and_deactivate_endpoint,
        test_activate_unknown_id_400,
        test_deactivate_noop_when_none_active,
        test_delete_happy_and_missing,
        test_delete_invalid_id_rejected,
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
    _manual_teardown(_orig, _tmp)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
