"""Auto-attach: workspace-diff scan registers files written during a goal
as kind=file goal_outputs.

The orchestrator can forget to call goal_attach_output for each deliverable
— the runtime safety net (db.auto_attach_workspace_outputs) finds new
files in workspace and surfaces them automatically. Without this, the user
has to navigate the filesystem to find what the agent produced.

These tests cover: positive case (new files attached), filters (hidden,
blacklisted dirs, suffixes, size, mtime), dedup, idempotency.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import db


def _make_goal(input_text: str = "x") -> tuple[str, float]:
    """Create a goal, mark it started at NOW, return (gid, started_at)."""
    gid = db.create_goal(user_input=input_text, source="cli")
    # Force started_at — claim_next_goal would do it in production.
    started_at = time.time() - 1.0  # 1s ago
    db._get_conn().execute(
        "UPDATE goals SET started_at=?, status='running' WHERE id=?",
        (started_at, gid),
    )
    db._get_conn().commit()
    return gid, started_at


def test_auto_attach_picks_up_new_files(qwe_temp_data_dir, tmp_path):
    """A file created in workspace after started_at is attached as kind=file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, started = _make_goal()

    # Create a file with current mtime (after started_at).
    f = workspace / "report.md"
    f.write_text("# Report\n\nfindings...\n")
    # Ensure mtime > started_at (some filesystems have 1s resolution).
    os.utime(f, (time.time() + 1, time.time() + 1))

    new_ids = db.auto_attach_workspace_outputs(
        gid, workspace_root=str(workspace), since_ts=started,
    )

    assert len(new_ids) == 1
    outs = db.get_goal_outputs(gid)
    assert len(outs) == 1
    o = outs[0]
    assert o["kind"] == "file"
    assert o["title"] == "report.md"
    assert o["value"] == str(f)
    assert o["meta"]["auto_attached"] is True
    assert o["meta"]["size_bytes"] > 0


def test_auto_attach_skips_files_older_than_started(qwe_temp_data_dir, tmp_path):
    """A file with mtime BEFORE goal.started_at is NOT attached.
    Without this, every goal would attach the entire workspace history."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, started = _make_goal()

    # File with mtime BEFORE started_at.
    old_f = workspace / "ancient.md"
    old_f.write_text("old")
    os.utime(old_f, (started - 10, started - 10))

    new_ids = db.auto_attach_workspace_outputs(
        gid, workspace_root=str(workspace), since_ts=started,
    )
    assert new_ids == []
    assert db.get_goal_outputs(gid) == []


def test_auto_attach_skips_hidden_files(qwe_temp_data_dir, tmp_path):
    """Hidden files (.foo) and hidden subdirs (./.tmp/) are skipped."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    (workspace / ".hidden.md").write_text("scratch")
    (workspace / ".DS_Store").write_bytes(b"\x00mac\x00")
    hidden_dir = workspace / ".tmp"
    hidden_dir.mkdir()
    (hidden_dir / "report.md").write_text("inside hidden")

    visible = workspace / "real.md"
    visible.write_text("the real deal")

    # Bump mtimes for all.
    now = time.time() + 1
    for p in workspace.rglob("*"):
        if p.is_file():
            os.utime(p, (now, now))

    new_ids = db.auto_attach_workspace_outputs(
        gid, workspace_root=str(workspace),
    )

    outs = db.get_goal_outputs(gid)
    assert len(outs) == 1
    assert outs[0]["title"] == "real.md"


def test_auto_attach_skips_blacklisted_subdirs(qwe_temp_data_dir, tmp_path):
    """browser_sessions/, memory/, kb/, module_data/ are skipped wholesale.
    These are infrastructure, not user deliverables."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    for excluded in ("browser_sessions", "memory", "kb", "module_data"):
        d = workspace / excluded
        d.mkdir()
        (d / "noise.md").write_text(f"in {excluded}")

    real = workspace / "deliverable.md"
    real.write_text("user wants this")

    now = time.time() + 1
    for p in workspace.rglob("*"):
        if p.is_file():
            os.utime(p, (now, now))

    db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    outs = db.get_goal_outputs(gid)
    titles = [o["title"] for o in outs]
    assert titles == ["deliverable.md"]


def test_auto_attach_skips_excluded_suffixes(qwe_temp_data_dir, tmp_path):
    """.pyc, .log, .tmp, .bak, .swp are scratch — not surfaced."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    (workspace / "compiled.pyc").write_bytes(b"\x00pyc\x00")
    (workspace / "debug.log").write_text("INFO ...")
    (workspace / "draft.bak").write_text("backup")
    (workspace / "vim.swp").write_bytes(b"\x00")
    (workspace / "temp.tmp").write_text("scratch")
    (workspace / "real.md").write_text("the real one")

    now = time.time() + 1
    for p in workspace.iterdir():
        os.utime(p, (now, now))

    db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    titles = [o["title"] for o in db.get_goal_outputs(gid)]
    assert titles == ["real.md"]


def test_auto_attach_skips_oversized_files(qwe_temp_data_dir, tmp_path,
                                            monkeypatch):
    """Files > 10MB cap aren't surfaced (would be unwieldy in UI)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    # Drop the cap so we don't actually need to make a 10MB file in tests.
    monkeypatch.setattr(db, "_WORKSPACE_AUTOATTACH_MAX_BYTES", 100)

    small = workspace / "small.txt"
    small.write_text("ok")
    big = workspace / "big.txt"
    big.write_text("x" * 500)  # 500 bytes > 100-byte cap

    now = time.time() + 1
    for p in workspace.iterdir():
        os.utime(p, (now, now))

    db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    titles = [o["title"] for o in db.get_goal_outputs(gid)]
    assert titles == ["small.txt"]


def test_auto_attach_skips_empty_files(qwe_temp_data_dir, tmp_path):
    """0-byte files are noise (touch-leftovers, partial writes)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    (workspace / "empty.md").touch()
    (workspace / "has_content.md").write_text("content")

    now = time.time() + 1
    for p in workspace.iterdir():
        os.utime(p, (now, now))

    db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    titles = [o["title"] for o in db.get_goal_outputs(gid)]
    assert titles == ["has_content.md"]


def test_auto_attach_is_idempotent_dedups_by_path(qwe_temp_data_dir, tmp_path):
    """Running the scan twice produces no duplicate outputs."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    f = workspace / "report.md"
    f.write_text("content")
    os.utime(f, (time.time() + 1, time.time() + 1))

    first = db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))
    second = db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    assert len(first) == 1
    assert len(second) == 0  # no new attachments on rescan
    assert len(db.get_goal_outputs(gid)) == 1


def test_auto_attach_respects_orchestrator_explicit_attach(qwe_temp_data_dir,
                                                            tmp_path):
    """If the orchestrator already called goal_attach_output for a file,
    the auto-scan does NOT attach it a second time."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    f = workspace / "report.md"
    f.write_text("content")
    os.utime(f, (time.time() + 1, time.time() + 1))

    # Orchestrator-style explicit attach first.
    db.attach_goal_output(
        gid, kind="file", title="Custom Title", value=str(f),
        meta={"set_by": "orchestrator"},
    )

    db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    outs = db.get_goal_outputs(gid)
    assert len(outs) == 1
    assert outs[0]["title"] == "Custom Title"  # orchestrator's version kept
    assert outs[0]["meta"].get("set_by") == "orchestrator"


def test_auto_attach_handles_missing_workspace_gracefully(qwe_temp_data_dir,
                                                           tmp_path):
    """If workspace dir doesn't exist, return empty list — don't crash."""
    gid, _ = _make_goal()
    result = db.auto_attach_workspace_outputs(
        gid, workspace_root=str(tmp_path / "nonexistent"),
    )
    assert result == []


def test_auto_attach_handles_missing_goal_gracefully(qwe_temp_data_dir,
                                                      tmp_path):
    """If the goal_id doesn't exist (and since_ts is auto-derived), return
    empty — don't crash on get_goal returning None."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "real.md").write_text("content")

    result = db.auto_attach_workspace_outputs(
        "g_does_not_exist", workspace_root=str(workspace),
    )
    assert result == []


def test_auto_attach_defaults_to_goal_started_at(qwe_temp_data_dir, tmp_path):
    """When since_ts is not passed, derive it from goal.started_at.

    Verifies the production call path (goal_runner doesn't pass since_ts).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, started = _make_goal()

    # File AFTER started_at — should be picked up.
    fresh = workspace / "fresh.md"
    fresh.write_text("fresh content")
    os.utime(fresh, (started + 10, started + 10))

    # File BEFORE started_at — should be ignored.
    stale = workspace / "stale.md"
    stale.write_text("old content")
    os.utime(stale, (started - 10, started - 10))

    # Pass workspace_root but NOT since_ts.
    db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))

    titles = [o["title"] for o in db.get_goal_outputs(gid)]
    assert titles == ["fresh.md"]


def test_auto_attach_recursive_into_subdirs(qwe_temp_data_dir, tmp_path):
    """Files in nested (non-blacklisted) subdirs are also attached.
    A goal might write docs/API.md + docs/module_*.md and we want them all."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gid, _ = _make_goal()

    docs = workspace / "docs"
    docs.mkdir()
    (docs / "API.md").write_text("# api")
    (docs / "module_foo.md").write_text("# foo")
    (docs / "module_bar.md").write_text("# bar")

    now = time.time() + 1
    for p in workspace.rglob("*"):
        if p.is_file():
            os.utime(p, (now, now))

    new_ids = db.auto_attach_workspace_outputs(gid, workspace_root=str(workspace))
    assert len(new_ids) == 3

    titles = sorted(o["title"] for o in db.get_goal_outputs(gid))
    assert titles == ["API.md", "module_bar.md", "module_foo.md"]
