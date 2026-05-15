"""HTTP surface tests for /api/goals (Phase 1).

Uses TestClient against the real FastAPI app with CASTOR_DATA_DIR pointed at a
tempdir. Mirrors the fixture pattern in tests/test_integration.py so the
isolation rules (no real ~/.castor data leaks) are identical.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _goals_api_env():
    original = os.environ.get("CASTOR_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_goals_api_"))
    os.environ["CASTOR_DATA_DIR"] = str(tmp_root)
    (tmp_root / ".migrated_v2").write_text("test skip\n")
    (tmp_root / ".migrated_from_qwe_qwe").write_text("test skip\n")
    _reload_core()
    try:
        yield tmp_root
    finally:
        _close_db()
        if original is not None:
            os.environ["CASTOR_DATA_DIR"] = original
        else:
            os.environ.pop("CASTOR_DATA_DIR", None)
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
    _close_db()
    for mod in ("config", "db", "soul", "threads", "presets", "server"):
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


def test_create_goal_returns_id_and_pending_status(client):
    r = client.post("/api/goals", json={"user_input": "scrape leads", "source": "api"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("g_")
    assert body["status"] == "pending"


def test_create_goal_rejects_empty_input(client):
    r = client.post("/api/goals", json={"user_input": "   "})
    assert r.status_code == 400


def test_get_goal_returns_full_row(client):
    gid = client.post("/api/goals", json={"user_input": "x", "source": "api"}).json()["id"]
    r = client.get(f"/api/goals/{gid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == gid
    assert body["status"] == "pending"
    assert body["user_input"] == "x"
    assert body["source"] == "api"
    assert body["cost_usd"] == 0.0


def test_get_goal_404_for_missing(client):
    r = client.get("/api/goals/g_nope")
    assert r.status_code == 404


def test_list_goals_filters_by_status(client):
    import db
    g1 = client.post("/api/goals", json={"user_input": "a"}).json()["id"]
    g2 = client.post("/api/goals", json={"user_input": "b"}).json()["id"]
    db.mark_goal_done(g1, result="done")

    pending = client.get("/api/goals?status=pending").json()["goals"]
    done = client.get("/api/goals?status=done").json()["goals"]
    pending_ids = {g["id"] for g in pending}
    done_ids = {g["id"] for g in done}
    assert g2 in pending_ids
    assert g1 in done_ids


def test_get_goal_events_returns_at_least_creation_event(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.get(f"/api/goals/{gid}/events")
    assert r.status_code == 200
    types = [e["event_type"] for e in r.json()["events"]]
    assert "goal_created" in types


def test_get_goal_events_404_for_missing(client):
    r = client.get("/api/goals/g_nope/events")
    assert r.status_code == 404


def test_pause_goal_transitions_to_paused(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.post(f"/api/goals/{gid}/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"
    # Verify persisted state.
    body = client.get(f"/api/goals/{gid}").json()
    assert body["status"] == "paused"


def test_pause_already_done_goal_conflicts(client):
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    db.mark_goal_done(gid, result="finished")
    r = client.post(f"/api/goals/{gid}/pause")
    assert r.status_code == 409


def test_abort_goal_marks_aborted(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.post(f"/api/goals/{gid}/abort")
    assert r.status_code == 200
    assert r.json()["status"] == "aborted"
    # Aborted goals are NOT re-claimable.
    import db
    assert db.claim_next_goal("worker_X", lease_sec=60) != gid


def test_resume_goal_transitions_paused_to_pending(client):
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    client.post(f"/api/goals/{gid}/pause")
    r = client.post(f"/api/goals/{gid}/resume")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    g = client.get(f"/api/goals/{gid}").json()
    assert g["status"] == "pending"


def test_resume_only_works_on_paused_goals(client):
    """Resuming a running/done/aborted goal returns 409."""
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    # Try resuming a pending goal — should 409
    r = client.post(f"/api/goals/{gid}/resume")
    assert r.status_code == 409


def test_get_goal_facts_empty_returns_empty_dict(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.get(f"/api/goals/{gid}/facts")
    assert r.status_code == 200
    assert r.json() == {"facts": {}}


def test_get_goal_facts_returns_saved_kv(client):
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    db.fact_save(gid, "login_url", "https://example.com")
    db.fact_save(gid, "count", "47")
    r = client.get(f"/api/goals/{gid}/facts")
    assert r.status_code == 200
    facts = r.json()["facts"]
    assert facts["login_url"] == "https://example.com"
    assert facts["count"] == "47"


def test_get_goal_facts_404_for_missing_goal(client):
    r = client.get("/api/goals/g_nope/facts")
    assert r.status_code == 404


def test_delete_goal_cascades_facts_events_checkpoints(client):
    """DELETE /api/goals/{id} removes the goal and all related rows."""
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    db.fact_save(gid, "k", "v")
    db.log_goal_event(gid, "test_event", {"a": 1})
    db.save_checkpoint(gid, round_num=1, messages=[{"role": "user", "content": "x"}])
    # Sanity: rows exist
    assert db.get_goal(gid) is not None
    assert db.fact_get(gid) == {"k": "v"}
    # Delete
    r = client.delete(f"/api/goals/{gid}")
    assert r.status_code == 200
    assert r.json() == {"id": gid, "deleted": True}
    # Cascade — children gone
    assert db.get_goal(gid) is None
    assert db.fact_get(gid) == {}
    assert db.load_latest_checkpoint(gid) is None
    assert db.get_goal_events(gid) == []


def test_delete_goal_404_for_missing(client):
    r = client.delete("/api/goals/g_nope")
    assert r.status_code == 404


def test_delete_goal_refuses_running(client):
    """A running goal must be aborted/paused before delete."""
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    # Force status=running (no actual worker)
    conn = db._get_conn()
    conn.execute("UPDATE goals SET status='running' WHERE id=?", (gid,))
    conn.commit()
    r = client.delete(f"/api/goals/{gid}")
    assert r.status_code == 409
    assert "abort" in r.json()["error"].lower()


def test_cleanup_goals_deletes_terminal_only(client):
    """POST /api/goals/cleanup deletes done+failed+aborted by default,
    leaves pending/running/paused alone.

    Module-scoped client fixture means earlier tests may have left some
    terminal goals in the DB. We assert against the SET of our own ids,
    not against the absolute deleted count.
    """
    import db
    ids = {
        "done":    client.post("/api/goals", json={"user_input": "cleanup-d"}).json()["id"],
        "failed":  client.post("/api/goals", json={"user_input": "cleanup-f"}).json()["id"],
        "aborted": client.post("/api/goals", json={"user_input": "cleanup-a"}).json()["id"],
        "pending": client.post("/api/goals", json={"user_input": "cleanup-p"}).json()["id"],
        "paused":  client.post("/api/goals", json={"user_input": "cleanup-ps"}).json()["id"],
    }
    db.mark_goal_done(ids["done"], result="ok")
    db.mark_goal_failed(ids["failed"], error="x")
    db.mark_goal_aborted(ids["aborted"])
    db.mark_goal_paused(ids["paused"], reason="user")

    r = client.post("/api/goals/cleanup", json={})
    assert r.status_code == 200
    assert r.json()["deleted"] >= 3  # at least our 3, possibly more from prior tests
    # Non-terminal goals survived
    assert db.get_goal(ids["pending"]) is not None
    assert db.get_goal(ids["paused"]) is not None
    # Terminal goals are gone
    assert db.get_goal(ids["done"]) is None
    assert db.get_goal(ids["failed"]) is None
    assert db.get_goal(ids["aborted"]) is None


def test_cleanup_goals_status_filter(client):
    """Filter to only delete e.g. 'aborted' — leaves done/failed intact."""
    import db
    done_id    = client.post("/api/goals", json={"user_input": "csf-d"}).json()["id"]
    aborted_id = client.post("/api/goals", json={"user_input": "csf-a"}).json()["id"]
    db.mark_goal_done(done_id, result="ok")
    db.mark_goal_aborted(aborted_id)
    r = client.post("/api/goals/cleanup", json={"status": "aborted"})
    assert r.status_code == 200
    assert r.json()["deleted"] >= 1
    # done survives
    assert db.get_goal(done_id) is not None
    # aborted gone
    assert db.get_goal(aborted_id) is None


def test_cleanup_goals_rejects_non_terminal_status(client):
    """Passing status='running' must be rejected — no foot-gun."""
    r = client.post("/api/goals/cleanup", json={"status": "running"})
    assert r.status_code == 400


def test_cleanup_goals_accepts_list_status(client):
    """status can be a list of terminal states."""
    import db
    a = client.post("/api/goals", json={"user_input": "cls-a"}).json()["id"]
    b = client.post("/api/goals", json={"user_input": "cls-b"}).json()["id"]
    db.mark_goal_done(a, result="ok")
    db.mark_goal_failed(b, error="x")
    r = client.post("/api/goals/cleanup", json={"status": ["done", "failed"]})
    assert r.status_code == 200
    assert r.json()["deleted"] >= 2
    # Both our goals gone
    assert db.get_goal(a) is None
    assert db.get_goal(b) is None
