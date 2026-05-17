"""Tests for symlink traversal logging in tools._resolve_path()."""
import logging
from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace directory for path resolution tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr("tools.WORKSPACE", ws)
    # Whitelist only the workspace
    monkeypatch.setattr("tools._get_write_whitelist", lambda: [str(ws)])
    return ws


def test_regular_write_no_warning(workspace, caplog):
    from tools import _resolve_path

    target = workspace / "hello.txt"
    target.write_text("hi")
    with caplog.at_level(logging.WARNING, logger="tools"):
        result = _resolve_path(str(target), for_write=True)
    assert result == target.resolve()
    assert "symlink" not in caplog.text


def test_symlink_inside_whitelist_logs_warning(workspace, caplog):
    from tools import _resolve_path

    real = workspace / "real.txt"
    real.write_text("content")
    link = workspace / "link.txt"
    link.symlink_to(real)

    with caplog.at_level(logging.WARNING, logger="tools"):
        result = _resolve_path(str(link), for_write=True)
    # Resolved target is inside whitelist — should succeed but log
    assert result == real.resolve()
    assert "symlink" in caplog.text


def test_symlink_outside_whitelist_blocked(workspace, tmp_path):
    from tools import _resolve_path

    # Create a file outside the workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    # Symlink from workspace to outside
    link = workspace / "escape.txt"
    link.symlink_to(outside)

    with pytest.raises(PermissionError, match="Cannot write outside"):
        _resolve_path(str(link), for_write=True)


def test_nonexistent_path_no_warning(workspace, caplog):
    """Writing a new file (path doesn't exist yet) should work without warning."""
    from tools import _resolve_path

    target = workspace / "newfile.txt"
    with caplog.at_level(logging.WARNING, logger="tools"):
        result = _resolve_path(str(target), for_write=True)
    assert result == target.resolve()
    assert "symlink" not in caplog.text


def test_read_path_no_symlink_check(workspace, tmp_path, caplog):
    """Read path (for_write=False) should not log symlink warnings."""
    from tools import _resolve_path

    outside = tmp_path / "readable.txt"
    outside.write_text("data")
    link = workspace / "readlink.txt"
    link.symlink_to(outside)

    with caplog.at_level(logging.WARNING, logger="tools"):
        result = _resolve_path(str(link), for_write=False)
    assert result == outside.resolve()
    # No symlink warning for reads
    assert "symlink" not in caplog.text
