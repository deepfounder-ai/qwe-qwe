"""Goal outputs — files, links, reports surfaced as UI deliverables.

User feedback: when a goal produces a file/link/report, the UI should
offer Download / Open / Save-to-memory buttons. Burying the deliverable
in the orchestrator's markdown summary makes the user grep prose.

Coverage:
  - db.attach_goal_output validation
  - goal_attach_output tool: per-kind security (workspace containment,
    URL scheme, report size cap)
  - REST endpoints: list / download / save-to-memory / delete
  - Path-traversal guards rechecked at download time
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  db helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_attach_and_list_outputs(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    oid = db.attach_goal_output(
        gid, kind="link", title="My link",
        value="https://example.com",
    )
    assert oid > 0
    outs = db.get_goal_outputs(gid)
    assert len(outs) == 1
    assert outs[0]["kind"] == "link"
    assert outs[0]["title"] == "My link"
    assert outs[0]["value"] == "https://example.com"


def test_attach_rejects_invalid_kind(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    with pytest.raises(ValueError):
        db.attach_goal_output(gid, kind="bogus", title="t", value="v")


def test_attach_rejects_empty_title_or_value(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    with pytest.raises(ValueError):
        db.attach_goal_output(gid, kind="link", title="", value="https://x")
    with pytest.raises(ValueError):
        db.attach_goal_output(gid, kind="link", title="t", value="")


def test_delete_output(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    oid = db.attach_goal_output(gid, kind="link", title="t", value="https://x")
    assert db.delete_goal_output(gid, oid) is True
    assert db.get_goal_outputs(gid) == []
    # Deleting a nonexistent output returns False
    assert db.delete_goal_output(gid, oid) is False


def test_outputs_cascade_delete_with_goal(qwe_temp_data_dir):
    """When the goal row is removed, its outputs should be cascade-deleted."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.attach_goal_output(gid, kind="link", title="t", value="https://x")
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM goals WHERE id=?", (gid,))
    conn.commit()
    assert db.get_goal_outputs(gid) == []


# ─────────────────────────────────────────────────────────────────────────────
#  goal_attach_output tool — per-kind security
# ─────────────────────────────────────────────────────────────────────────────


def test_tool_rejects_file_outside_workspace(qwe_temp_data_dir):
    """A file path outside ~/.castor/workspace/ must be refused to prevent
    the orchestrator from leaking /etc/passwd via the Download endpoint."""
    import tools
    import db
    from turn_context import TurnContext

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    r = tools.execute("goal_attach_output", {
        "kind": "file",
        "title": "passwd",
        "value": "/etc/passwd",
    })
    assert "Error" in r and "workspace" in r.lower()


def test_tool_accepts_file_inside_workspace(qwe_temp_data_dir):
    """A real file in workspace gets attached with byte_size metadata."""
    import config
    import db
    import tools
    from turn_context import TurnContext

    workspace = Path(config.WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)
    test_file = workspace / "report.csv"
    test_file.write_text("header\n1,2,3\n")

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    r = tools.execute("goal_attach_output", {
        "kind": "file",
        "title": "report",
        "value": str(test_file),
    })
    assert "attached" in r.lower()
    outs = db.get_goal_outputs(gid)
    assert len(outs) == 1
    assert outs[0]["kind"] == "file"
    # Metadata includes byte_size for the UI to render "1.2 KB"
    assert "byte_size" in outs[0]["meta"]
    assert outs[0]["meta"]["byte_size"] > 0


def test_tool_rejects_missing_file(qwe_temp_data_dir):
    import config
    import db
    import tools
    from turn_context import TurnContext

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    # Path inside workspace but the file doesn't exist
    fake = str(Path(config.WORKSPACE_DIR) / "not_written_yet.csv")
    r = tools.execute("goal_attach_output", {
        "kind": "file",
        "title": "x",
        "value": fake,
    })
    assert "Error" in r and "does not exist" in r.lower()


def test_tool_rejects_non_http_link(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://foo/x"):
        r = tools.execute("goal_attach_output", {
            "kind": "link",
            "title": "x",
            "value": bad,
        })
        assert "Error" in r, f"should have rejected {bad}"


def test_tool_accepts_http_link(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    r = tools.execute("goal_attach_output", {
        "kind": "link",
        "title": "Search results",
        "value": "https://example.com/search?q=x",
    })
    assert "attached" in r.lower()
    assert len(db.get_goal_outputs(gid)) == 1


def test_tool_rejects_oversized_report(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    huge = "x" * (200 * 1024 + 100)
    r = tools.execute("goal_attach_output", {
        "kind": "report",
        "title": "Big report",
        "value": huge,
    })
    assert "Error" in r and "200 KB" in r


def test_tool_accepts_normal_report(qwe_temp_data_dir):
    import db
    import tools
    from turn_context import TurnContext

    gid = db.create_goal(user_input="x", source="cli")
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))

    r = tools.execute("goal_attach_output", {
        "kind": "report",
        "title": "Findings",
        "value": "# Audit results\n\n- finding 1\n- finding 2",
    })
    assert "attached" in r.lower()


def test_tool_requires_active_goal(qwe_temp_data_dir):
    import tools
    from turn_context import TurnContext

    tools._set_turn_ctx(TurnContext(source="cli"))  # no goal_id
    r = tools.execute("goal_attach_output", {
        "kind": "link", "title": "x", "value": "https://x.com",
    })
    assert "Error" in r and "goal" in r.lower()


# ─────────────────────────────────────────────────────────────────────────────
#  REST endpoints
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(qwe_temp_data_dir):
    """Fresh TestClient per test for output endpoints (state isolation)."""
    import importlib
    import sys
    # The qwe_temp_data_dir fixture already reloaded config + db. We also
    # need server reloaded so its app picks up the fresh DB connection.
    if "server" in sys.modules:
        importlib.reload(sys.modules["server"])
    else:
        importlib.import_module("server")
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


def test_get_outputs_endpoint_returns_attached(client, qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="api")
    db.attach_goal_output(gid, kind="link", title="ex",
                          value="https://example.com")
    r = client.get(f"/api/goals/{gid}/outputs")
    assert r.status_code == 200
    outs = r.json()["outputs"]
    assert len(outs) == 1
    assert outs[0]["kind"] == "link"


def test_get_outputs_404(client):
    r = client.get("/api/goals/g_nope/outputs")
    assert r.status_code == 404


def test_download_serves_file(client, qwe_temp_data_dir):
    import config
    import db
    workspace = Path(config.WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)
    f = workspace / "test_download.txt"
    f.write_text("hello world")

    gid = db.create_goal(user_input="x", source="api")
    oid = db.attach_goal_output(
        gid, kind="file", title="t", value=str(f),
        meta={"byte_size": f.stat().st_size},
    )
    r = client.get(f"/api/goals/{gid}/outputs/{oid}/download")
    assert r.status_code == 200
    assert r.content == b"hello world"


def test_download_rejects_non_file_kind(client, qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="api")
    oid = db.attach_goal_output(gid, kind="link", title="t",
                                value="https://x.com")
    r = client.get(f"/api/goals/{gid}/outputs/{oid}/download")
    assert r.status_code == 400


def test_download_rejects_path_escape_after_attach(client, qwe_temp_data_dir, monkeypatch):
    """Defense in depth: even if a bad path snuck past attach validation,
    download re-checks workspace containment."""
    import db
    gid = db.create_goal(user_input="x", source="api")
    # Bypass tool validation; insert directly with a path outside workspace.
    conn = db._get_conn()
    import time as _t
    cur = conn.execute(
        """INSERT INTO goal_outputs (goal_id, kind, title, value, meta, created_at)
           VALUES (?, 'file', 'evil', '/etc/passwd', '{}', ?)""",
        (gid, _t.time()),
    )
    conn.commit()
    oid = cur.lastrowid
    r = client.get(f"/api/goals/{gid}/outputs/{oid}/download")
    assert r.status_code == 403
    assert "workspace" in r.json()["error"].lower()


def test_save_to_memory_only_for_report(client, qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="api")
    file_oid = db.attach_goal_output(
        gid, kind="link", title="t", value="https://x.com",
    )
    r = client.post(f"/api/goals/{gid}/outputs/{file_oid}/save-to-memory", json={})
    assert r.status_code == 400


def test_delete_output_endpoint(client, qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="api")
    oid = db.attach_goal_output(gid, kind="link", title="t",
                                value="https://x.com")
    r = client.delete(f"/api/goals/{gid}/outputs/{oid}")
    assert r.status_code == 200
    assert db.get_goal_outputs(gid) == []
