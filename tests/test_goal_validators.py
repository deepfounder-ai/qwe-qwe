"""Tests for goal_validators — acceptance-criteria executors.

Covers per-kind pass/fail paths plus validate_criterion schema checks.
NO REAL NETWORK CALLS — urllib.request.urlopen / build_opener are
monkeypatched for http_200 cases. tmp_path is used for filesystem cases
with CASTOR_DATA_DIR pointed at the tempdir so the module's workspace
resolution honors the test sandbox.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import goal_validators


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point CASTOR_DATA_DIR at tmp_path; return the workspace dir."""
    monkeypatch.setenv("CASTOR_DATA_DIR", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class _FakeResponse:
    """Minimal stand-in for an urllib HTTPResponse."""

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def _stub_opener(monkeypatch, response_or_exc):
    """Replace ``urllib.request.build_opener`` so the returned opener's .open
    yields ``response_or_exc`` (raises if it's an Exception instance).
    """

    class _Opener:
        def open(self, url, timeout=None):  # noqa: ARG002
            if isinstance(response_or_exc, Exception):
                raise response_or_exc
            return response_or_exc

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: _Opener())


# ─────────────────────────────────────────────────────────────────────────────
# files_exist
# ─────────────────────────────────────────────────────────────────────────────


def test_files_exist_pass_two_files(workspace):
    (workspace / "a.txt").write_text("a")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.txt").write_text("b")
    crit = {
        "kind": "files_exist",
        "spec": {"paths": ["a.txt", "sub/b.txt"]},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is True
    assert remediation == ""


def test_files_exist_fail_one_missing_mentions_path(workspace):
    (workspace / "a.txt").write_text("a")
    crit = {
        "kind": "files_exist",
        "spec": {"paths": ["a.txt", "missing.md"]},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "missing.md" in remediation


def test_files_exist_absolute_path(workspace, tmp_path):
    """Absolute paths are used as-is, not anchored at workspace."""
    abs_file = tmp_path / "absolute.txt"
    abs_file.write_text("x")
    crit = {
        "kind": "files_exist",
        "spec": {"paths": [str(abs_file)]},
    }
    passed, _ = goal_validators.run_validator(crit)
    assert passed is True


def test_files_exist_empty_paths_is_malformed(workspace):
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "files_exist", "spec": {"paths": []}}
        )


def test_files_exist_remediation_contains_create_hint(workspace):
    crit = {
        "kind": "files_exist",
        "spec": {"paths": ["docs/API.md"]},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    # Spec example: "Expected file 'docs/API.md' to exist, but it does not. Create it."
    assert "docs/API.md" in remediation
    assert "Create" in remediation


# ─────────────────────────────────────────────────────────────────────────────
# min_count
# ─────────────────────────────────────────────────────────────────────────────


def test_min_count_pass_glob_with_matches(workspace):
    for i in range(5):
        (workspace / f"module_{i:02d}.md").write_text("x")
    crit = {
        "kind": "min_count",
        "spec": {"glob": "module_*.md", "min": 3},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is True
    assert remediation == ""


def test_min_count_fail_zero_matches_remediation_has_glob_and_count(workspace):
    crit = {
        "kind": "min_count",
        "spec": {"glob": "docs/module_*.md", "min": 50},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "docs/module_*.md" in remediation
    assert "50" in remediation
    assert "0" in remediation  # actual count


def test_min_count_missing_glob_key_malformed():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "min_count", "spec": {"min": 1}}
        )


def test_min_count_min_zero_malformed():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "min_count", "spec": {"glob": "*.md", "min": 0}}
        )


# ─────────────────────────────────────────────────────────────────────────────
# regex_in_file
# ─────────────────────────────────────────────────────────────────────────────


def test_regex_in_file_pass(workspace):
    (workspace / "API.md").write_text("# Module Index\n\nstuff\n")
    crit = {
        "kind": "regex_in_file",
        "spec": {"path": "API.md", "pattern": r"Module Index"},
    }
    passed, _ = goal_validators.run_validator(crit)
    assert passed is True


def test_regex_in_file_fail_pattern_not_found(workspace):
    (workspace / "API.md").write_text("nothing relevant here\n")
    crit = {
        "kind": "regex_in_file",
        "spec": {"path": "API.md", "pattern": r"Module Index"},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "Module Index" in remediation
    assert "API.md" in remediation


def test_regex_in_file_fail_file_missing(workspace):
    crit = {
        "kind": "regex_in_file",
        "spec": {"path": "nope.md", "pattern": r"x"},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "nope.md" in remediation


def test_regex_in_file_invalid_regex_validate_raises():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "regex_in_file", "spec": {"path": "x.md", "pattern": "(["}}
        )


def test_regex_in_file_multiline_match(workspace):
    """Patterns that need MULTILINE flag still match — runner retries with it."""
    (workspace / "log.txt").write_text("line one\n^^^ marker line\nline three\n")
    crit = {
        "kind": "regex_in_file",
        "spec": {"path": "log.txt", "pattern": r"^\^\^\^ marker"},
    }
    passed, _ = goal_validators.run_validator(crit)
    assert passed is True


# ─────────────────────────────────────────────────────────────────────────────
# shell_returns_zero
# ─────────────────────────────────────────────────────────────────────────────


def test_shell_returns_zero_pass_true(workspace):
    crit = {
        "kind": "shell_returns_zero",
        "spec": {"cmd": "true", "timeout": 5},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is True
    assert remediation == ""


def test_shell_returns_zero_fail_false_includes_exit_code(workspace):
    crit = {
        "kind": "shell_returns_zero",
        "spec": {"cmd": "false", "timeout": 5},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    # `false` exits 1
    assert "1" in remediation
    assert "exited" in remediation.lower() or "code" in remediation.lower()


def test_shell_returns_zero_fail_includes_stderr_snippet(workspace):
    crit = {
        "kind": "shell_returns_zero",
        "spec": {
            "cmd": "echo 'boom! something went wrong' 1>&2; exit 2",
            "timeout": 5,
        },
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "2" in remediation  # exit code
    assert "boom" in remediation  # stderr was captured


def test_shell_returns_zero_timeout(workspace):
    crit = {
        "kind": "shell_returns_zero",
        "spec": {"cmd": "sleep 5", "timeout": 1},
    }
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "timed out" in remediation.lower() or "timeout" in remediation.lower()


def test_shell_returns_zero_empty_cmd_malformed():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "shell_returns_zero", "spec": {"cmd": ""}}
        )


def test_shell_returns_zero_runs_in_workspace_cwd(workspace):
    """The cmd should run with cwd = workspace, so a relative file is visible."""
    (workspace / "hello.txt").write_text("hi")
    crit = {
        "kind": "shell_returns_zero",
        "spec": {"cmd": "test -f hello.txt", "timeout": 5},
    }
    passed, _ = goal_validators.run_validator(crit)
    assert passed is True


# ─────────────────────────────────────────────────────────────────────────────
# http_200
# ─────────────────────────────────────────────────────────────────────────────


def test_http_200_pass(monkeypatch, workspace):
    _stub_opener(monkeypatch, _FakeResponse(status=200))
    crit = {"kind": "http_200", "spec": {"url": "https://example.com/health"}}
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is True
    assert remediation == ""


def test_http_200_2xx_range_pass(monkeypatch, workspace):
    _stub_opener(monkeypatch, _FakeResponse(status=204))
    crit = {"kind": "http_200", "spec": {"url": "https://example.com/x"}}
    passed, _ = goal_validators.run_validator(crit)
    assert passed is True


def test_http_200_fail_500_remediation_includes_status(monkeypatch, workspace):
    # urllib raises HTTPError for non-2xx; simulate that path.
    err = urllib.error.HTTPError(
        url="https://example.com/x",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=io.BytesIO(b""),
    )
    _stub_opener(monkeypatch, err)
    crit = {"kind": "http_200", "spec": {"url": "https://example.com/x"}}
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "500" in remediation
    assert "https://example.com/x" in remediation


def test_http_200_fail_503_remediation_includes_status(monkeypatch, workspace):
    err = urllib.error.HTTPError(
        url="https://example.com/y",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=io.BytesIO(b""),
    )
    _stub_opener(monkeypatch, err)
    crit = {"kind": "http_200", "spec": {"url": "https://example.com/y"}}
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "503" in remediation


def test_http_200_fail_network_error_remediation_includes_class(monkeypatch, workspace):
    err = urllib.error.URLError("connection refused")
    _stub_opener(monkeypatch, err)
    crit = {"kind": "http_200", "spec": {"url": "https://example.com/z"}}
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    # Remediation should mention the error class or reason
    assert "URLError" in remediation or "connection refused" in remediation


def test_http_200_url_must_have_scheme():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "http_200", "spec": {"url": "example.com/no-scheme"}}
        )


# ─────────────────────────────────────────────────────────────────────────────
# validate_criterion — happy / sad
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_criterion_all_kinds_happy():
    """Each of the 5 kinds with a minimal valid spec passes validation."""
    cases = [
        {"kind": "files_exist", "spec": {"paths": ["a.txt"]}},
        {"kind": "min_count", "spec": {"glob": "*.md", "min": 1}},
        {"kind": "regex_in_file", "spec": {"path": "x.md", "pattern": r"hi"}},
        {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
        {"kind": "http_200", "spec": {"url": "https://example.com/"}},
    ]
    for crit in cases:
        # Must not raise
        assert goal_validators.validate_criterion(crit) is None


def test_validate_criterion_unknown_kind():
    with pytest.raises(ValueError) as ei:
        goal_validators.validate_criterion(
            {"kind": "rocket_launch", "spec": {}}
        )
    assert "kind" in str(ei.value)


def test_validate_criterion_non_dict_top_level():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion("not a dict")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(None)  # type: ignore[arg-type]


def test_validate_criterion_non_dict_spec():
    with pytest.raises(ValueError):
        goal_validators.validate_criterion(
            {"kind": "files_exist", "spec": "nope"}
        )


# ─────────────────────────────────────────────────────────────────────────────
# run_validator never raises — defense-in-depth
# ─────────────────────────────────────────────────────────────────────────────


def test_run_validator_on_malformed_returns_false_not_raises():
    """Spec: run_validator must NOT raise on malformed criterion either."""
    passed, remediation = goal_validators.run_validator({"kind": "garbage"})
    assert passed is False
    assert "Malformed" in remediation


def test_run_validator_on_non_dict_returns_false_not_raises():
    passed, remediation = goal_validators.run_validator(None)  # type: ignore[arg-type]
    assert passed is False
    assert "Malformed" in remediation


# ─────────────────────────────────────────────────────────────────────────────
# Path resolution helper
# ─────────────────────────────────────────────────────────────────────────────


def test_workspace_root_honors_castor_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CASTOR_DATA_DIR", str(tmp_path))
    assert goal_validators._workspace_root() == tmp_path / "workspace"


def test_resolve_relative_anchors_at_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("CASTOR_DATA_DIR", str(tmp_path))
    assert (
        goal_validators._resolve("docs/API.md")
        == tmp_path / "workspace" / "docs" / "API.md"
    )


def test_resolve_absolute_passes_through(monkeypatch, tmp_path):
    monkeypatch.setenv("CASTOR_DATA_DIR", str(tmp_path))
    abs_path = "/tmp/some/abs/path.txt" if Path("/tmp").exists() else str(tmp_path / "abs.txt")
    assert goal_validators._resolve(abs_path) == Path(abs_path)
