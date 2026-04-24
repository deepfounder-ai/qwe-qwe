"""HTTP integration tests for the Routines endpoints.

The unit-test files (``test_scheduler_cron.py``,
``test_telegram_notify_tool.py``, ``test_thread_folders.py``) cover the
Python-layer logic. This file hits the actual FastAPI routes through
``TestClient`` so we catch:

- Endpoint shape regressions (missing keys, wrong status codes)
- Wiring mistakes between server.py and scheduler.py / threads.py
- JSON-body parsing edge cases (None vs missing vs empty string)
- Background-thread fire semantics (auto-run, manual run, run-again-while-busy)

agent.run is mocked — these tests verify the HTTP surface only, not the
LLM turn itself. Test shape matches test_integration.py's module-level
env bootstrap (fresh tempdir, reloaded server).
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _routines_env():
    original = os.environ.get("QWE_DATA_DIR")
    tmp = Path(tempfile.mkdtemp(prefix="qwe_routines_"))
    os.environ["QWE_DATA_DIR"] = str(tmp)
    _reload_core()
    try:
        yield tmp
    finally:
        _close_db()
        if original is not None:
            os.environ["QWE_DATA_DIR"] = original
        else:
            os.environ.pop("QWE_DATA_DIR", None)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        _reload_core()


def _close_db():
    db_mod = sys.modules.get("db")
    if db_mod is None:
        return
    try:
        local = getattr(db_mod, "_local", None)
        conn = getattr(local, "conn", None) if local else None
        if conn is not None:
            conn.close()
        if local is not None:
            local.conn = None
        db_mod._migrated = False
    except Exception:
        pass


def _reload_core():
    _close_db()
    for mod in ("config", "db", "soul", "threads", "presets", "scheduler", "server"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def mock_agent_run(monkeypatch):
    """Replace agent.run with a synchronous stub so endpoint tests don't
    spin up the LLM. Captures calls for assertion.

    Yields a list — each entry is a dict of {user_input, thread_id, source}.
    """
    import agent

    captured: list[dict] = []

    class _FakeResult:
        def __init__(self, r):
            self.reply = r

    def _fake_run(user_input, thread_id=None, source="cli", **kw):
        captured.append({
            "user_input": user_input,
            "thread_id": thread_id,
            "source": source,
        })
        # Mimic what the real agent.run does: persist user + assistant.
        try:
            import db
            db.save_message("user", user_input, thread_id=thread_id)
            db.save_message("assistant", "ok.", thread_id=thread_id,
                             meta={"tools": [], "tool_details": []})
        except Exception:
            pass
        return _FakeResult("ok.")

    monkeypatch.setattr(agent, "run", _fake_run)
    return captured


# ── POST /api/cron — creation + auto-fire ─────────────────────────────


def test_post_cron_creates_routine_thread_and_metrics(client, mock_agent_run):
    """Successful create returns thread_id + schedule is parsed."""
    r = client.post("/api/cron", json={
        "name": "daily-brief",
        "schedule": "daily 09:00",
        "task": "summarise the day",
        "skip_dry_run": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("name") == "daily-brief"
    assert body.get("thread_id", "").startswith("t_")
    assert body.get("repeat") is True

    # Background auto-fire will call agent.run eventually — poll briefly.
    for _ in range(40):
        if mock_agent_run:
            break
        time.sleep(0.05)
    assert mock_agent_run, "auto-fire never invoked agent.run"
    # The routine task was passed as user_input, in its own thread,
    # with source='routine' (distinct from web/cli for downstream filters)
    last = mock_agent_run[-1]
    assert last["user_input"] == "summarise the day"
    assert last["thread_id"] == body["thread_id"]
    assert last["source"] == "routine"

    # Cleanup — delete so it doesn't leak into other tests
    client.delete(f"/api/cron/{_find_id(client, 'daily-brief')}")


def test_post_cron_rejects_bad_schedule(client, mock_agent_run):
    """5-field cron syntax is not supported; error is returned cleanly."""
    r = client.post("/api/cron", json={
        "name": "broken",
        "schedule": "0 9 * * *",  # parser doesn't understand this
        "task": "x",
        "skip_dry_run": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert "error" in body
    assert "parse" in body["error"].lower() or "schedule" in body["error"].lower()
    # No thread id — nothing was created
    assert "thread_id" not in body


# ── POST /api/cron/{id}/run — manual fire ─────────────────────────────


def test_run_endpoint_fires_in_background(client, mock_agent_run):
    """Manual Run returns instantly with thread_id + kicks agent.run async."""
    r = client.post("/api/cron", json={
        "name": "runme", "schedule": "every 2h",
        "task": "do the thing", "skip_dry_run": True,
    })
    cron_id = _find_id(client, "runme")
    mock_agent_run.clear()  # forget auto-fire

    # Wait for auto-fire lock to release before hitting /run
    import scheduler
    tid = r.json()["thread_id"]
    for _ in range(60):
        if not scheduler.is_routine_firing(tid):
            break
        time.sleep(0.1)

    r = client.post(f"/api/cron/{cron_id}/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("thread_id", "").startswith("t_")
    assert body.get("name") == "runme"

    # agent.run invoked once more
    for _ in range(40):
        if mock_agent_run:
            break
        time.sleep(0.05)
    assert mock_agent_run, "/run didn't invoke agent.run"

    client.delete(f"/api/cron/{cron_id}")


def test_run_endpoint_404_on_unknown_id(client):
    r = client.post("/api/cron/99999/run")
    assert r.status_code == 404
    assert "error" in r.json()


def test_run_endpoint_signals_already_running(client, mock_agent_run, monkeypatch):
    """When the routine's per-thread lock is held, /run returns
    ``already_running: true`` instead of silently skipping."""
    import scheduler

    r = client.post("/api/cron", json={
        "name": "busyone", "schedule": "every 2h",
        "task": "never returns", "skip_dry_run": True,
    })
    cron_id = _find_id(client, "busyone")
    tid = r.json()["thread_id"]

    # Force the lock to appear held — monkeypatch is_routine_firing
    monkeypatch.setattr(scheduler, "is_routine_firing",
                         lambda t: t == tid)

    r2 = client.post(f"/api/cron/{cron_id}/run")
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("already_running") is True
    assert body.get("ok") is False
    assert "hint" in body

    client.delete(f"/api/cron/{cron_id}")


# ── POST /api/cron/{id}/toggle — pause / resume ───────────────────────


def test_toggle_endpoint_flips_enabled(client, mock_agent_run):
    r = client.post("/api/cron", json={
        "name": "togglee", "schedule": "every 2h",
        "task": "t", "skip_dry_run": True,
    })
    cron_id = _find_id(client, "togglee")

    # Toggle (no body) → disabled
    r1 = client.post(f"/api/cron/{cron_id}/toggle")
    assert r1.status_code == 200
    assert r1.json() == {"ok": True, "enabled": False}
    # Toggle again → enabled
    r2 = client.post(f"/api/cron/{cron_id}/toggle")
    assert r2.json() == {"ok": True, "enabled": True}
    # Explicit set
    r3 = client.post(f"/api/cron/{cron_id}/toggle", json={"enabled": False})
    assert r3.json() == {"ok": True, "enabled": False}
    # list_tasks reflects the state
    listing = client.get("/api/cron").json()
    entry = next(t for t in listing if t["id"] == cron_id)
    assert entry["enabled"] is False

    client.delete(f"/api/cron/{cron_id}")


def test_toggle_endpoint_404_unknown(client):
    r = client.post("/api/cron/99999/toggle")
    assert r.status_code == 404


# ── Thread folders endpoints ──────────────────────────────────────────


def test_folder_roundtrip_via_http(client):
    # Create a fresh thread to avoid touching the default one
    r = client.post("/api/threads", json={"name": "folder-test"})
    tid = r.json()["id"]
    try:
        r = client.post(f"/api/threads/{tid}/folder", json={"folder": "Work"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "folder": "Work"}
        # GET /api/folders picks it up
        folders = client.get("/api/folders").json()
        assert "Work" in folders.get("folders", [])
        # Clear → disappears from the list
        r = client.post(f"/api/threads/{tid}/folder", json={"folder": ""})
        assert r.json() == {"ok": True, "folder": ""}
        folders = client.get("/api/folders").json()
        assert "Work" not in folders.get("folders", [])
    finally:
        client.delete(f"/api/threads/{tid}")


def test_folder_endpoint_404_unknown_thread(client):
    r = client.post("/api/threads/t_nonexistent/folder", json={"folder": "X"})
    assert r.status_code == 404


# ── list_tasks shape — exposes all fields the UI relies on ────────────


def test_list_tasks_shape_for_user_routine(client, mock_agent_run):
    """Every key the UI reads must be present in the list_tasks() dict.
    Regression guard against field renames quietly breaking card render.
    """
    r = client.post("/api/cron", json={
        "name": "shape-check", "schedule": "every 2h",
        "task": "x", "skip_dry_run": True,
    })
    cron_id = _find_id(client, "shape-check")
    entry = next(t for t in client.get("/api/cron").json()
                  if t["id"] == cron_id)
    for key in ("id", "name", "task", "schedule",
                "next_run", "last_run", "repeat", "enabled",
                "run_count", "last_status", "last",
                "last_duration_ms", "last_result",
                "thread_id", "firing"):
        assert key in entry, f"list_tasks missing key: {key!r}"
    client.delete(f"/api/cron/{cron_id}")


# ── helpers ───────────────────────────────────────────────────────────


def _find_id(client, name: str) -> int:
    """Resolve a routine's numeric id by name."""
    for j in client.get("/api/cron").json():
        if j.get("name") == name:
            return j["id"]
    raise AssertionError(f"routine {name!r} not found")
