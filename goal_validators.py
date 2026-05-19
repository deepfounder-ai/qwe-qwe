"""Acceptance-criteria validators for goal subtasks.

Used by the goal runner's acceptance gate: each subtask carries a
`done_condition` of one of 5 kinds; before a goal can be marked done the
gate runs every condition and re-enters the orchestrator with a
remediation note on any failure.

Public API:
    validate_criterion(criterion: dict) -> None
        Schema-only check. Raises ValueError on malformed input.

    run_validator(criterion: dict) -> tuple[bool, str]
        Executes the criterion against filesystem / shell / HTTP.
        Returns (passed, remediation). NEVER raises — any failure mode
        is converted into (False, "<diagnostic>").

Stdlib only — no `requests`, no `httpx`. Mirrors `config.DATA_DIR`
resolution for the workspace root so it honors `CASTOR_DATA_DIR`.
"""

from __future__ import annotations

import difflib
import glob as _glob
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# ── Closed set of validator kinds ──
_KINDS = (
    "files_exist",
    "min_count",
    "regex_in_file",
    "shell_returns_zero",
    "http_200",
)

# Cap shell timeout (spec §1).
_SHELL_TIMEOUT_DEFAULT = 10
_SHELL_TIMEOUT_MAX = 60

# HTTP fetch parameters (spec §1).
_HTTP_TIMEOUT = 10
_HTTP_MAX_REDIRECTS = 3
_HTTP_PASS_LO = 200
_HTTP_PASS_HI = 299


def _workspace_root() -> Path:
    """Return the workspace dir (``$CASTOR_DATA_DIR/workspace``).

    Mirrors the resolution logic in ``config.py`` (which itself reads
    ``CASTOR_DATA_DIR`` with a default of ``~/.castor``). We re-derive
    it here rather than importing ``config`` so this module stays a
    leaf with no agent-runtime dependencies.
    """
    data_dir = Path(os.path.expanduser(os.environ.get("CASTOR_DATA_DIR", "~/.castor")))
    return data_dir / "workspace"


def _resolve(rel_or_abs: str) -> Path:
    """Resolve a path argument: absolute → as-is, relative → workspace-anchored."""
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return _workspace_root() / p


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


def validate_criterion(criterion: dict) -> None:
    """Raise ValueError if `criterion` is malformed.

    Pure schema check. Does NOT execute the criterion.
    """
    if not isinstance(criterion, dict):
        raise ValueError(f"criterion must be a dict, got {type(criterion).__name__}")

    kind = criterion.get("kind")
    if kind not in _KINDS:
        suggestion = ""
        close = difflib.get_close_matches(kind or "", list(_KINDS), n=1, cutoff=0.6)
        if close:
            suggestion = f" Did you mean {close[0]!r}?"
        raise ValueError(
            f"criterion.kind must be one of {list(_KINDS)}, got {kind!r}.{suggestion}"
        )

    spec = criterion.get("spec")
    if not isinstance(spec, dict):
        raise ValueError(
            f"criterion.spec must be a dict, got {type(spec).__name__}"
        )

    if kind == "files_exist":
        paths = spec.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("files_exist.spec.paths must be a non-empty list")
        for p in paths:
            if not isinstance(p, str) or not p:
                raise ValueError("files_exist.spec.paths entries must be non-empty strings")

    elif kind == "min_count":
        pattern = spec.get("glob")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("min_count.spec.glob must be a non-empty string")
        minimum = spec.get("min")
        if not isinstance(minimum, int) or isinstance(minimum, bool):
            raise ValueError("min_count.spec.min must be an int")
        if minimum < 1:
            raise ValueError("min_count.spec.min must be >= 1")

    elif kind == "regex_in_file":
        path = spec.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("regex_in_file.spec.path must be a non-empty string")
        pattern = spec.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("regex_in_file.spec.pattern must be a non-empty string")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"regex_in_file.spec.pattern does not compile: {e}") from e

    elif kind == "shell_returns_zero":
        cmd = spec.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("shell_returns_zero.spec.cmd must be a non-empty string")
        timeout = spec.get("timeout", _SHELL_TIMEOUT_DEFAULT)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("shell_returns_zero.spec.timeout must be a number")
        if timeout <= 0:
            raise ValueError("shell_returns_zero.spec.timeout must be > 0")

    elif kind == "http_200":
        url = spec.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("http_200.spec.url must be a non-empty string")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("http_200.spec.url must start with http:// or https://")


# ─────────────────────────────────────────────────────────────────────────────
# Execution
# ─────────────────────────────────────────────────────────────────────────────


def run_validator(criterion: dict) -> tuple[bool, str]:
    """Execute `criterion` and return (passed, remediation).

    NEVER raises. Failures are returned as (False, "<diagnostic>").
    """
    # Defensive — caller is expected to have schema-checked first, but
    # we don't want to crash the gate if they didn't.
    try:
        validate_criterion(criterion)
    except ValueError as e:
        return False, f"Malformed criterion: {e}"
    except Exception as e:  # noqa: BLE001 — gate must not raise
        return False, f"Malformed criterion: {type(e).__name__}: {e}"

    kind = criterion["kind"]
    spec = criterion["spec"]

    try:
        if kind == "files_exist":
            return _run_files_exist(spec)
        if kind == "min_count":
            return _run_min_count(spec)
        if kind == "regex_in_file":
            return _run_regex_in_file(spec)
        if kind == "shell_returns_zero":
            return _run_shell_returns_zero(spec)
        if kind == "http_200":
            return _run_http_200(spec)
    except Exception as e:  # noqa: BLE001 — top-level shield, never propagate
        return False, f"Validator crashed unexpectedly: {type(e).__name__}: {e}"

    # Unreachable — validate_criterion gates kind.
    return False, f"Unknown criterion kind: {kind!r}"


# ── Per-kind runners ────────────────────────────────────────────────────────


def _run_files_exist(spec: dict) -> tuple[bool, str]:
    paths = spec["paths"]
    missing: list[str] = []
    for raw in paths:
        try:
            resolved = _resolve(raw)
            if not resolved.exists():
                missing.append(raw)
        except OSError as e:
            missing.append(f"{raw} ({type(e).__name__})")

    if not missing:
        return True, ""

    if len(missing) == 1:
        return (
            False,
            f"Expected file {missing[0]!r} to exist, but it does not. Create it.",
        )
    listed = ", ".join(repr(m) for m in missing)
    return (
        False,
        f"Expected files to exist: {listed}. Create the missing one(s).",
    )


def _run_min_count(spec: dict) -> tuple[bool, str]:
    pattern = spec["glob"]
    minimum = spec["min"]

    # Anchor relative globs at the workspace; absolute globs go straight through.
    abs_pattern = pattern if os.path.isabs(pattern) else str(_workspace_root() / pattern)
    try:
        matches = _glob.glob(abs_pattern, recursive=True)
    except OSError as e:
        return False, (
            f"Could not glob {pattern!r}: {type(e).__name__}: {e}. "
            f"Check the pattern and workspace state."
        )

    count = len(matches)
    if count >= minimum:
        return True, ""

    return (
        False,
        f"Expected at least {minimum} files matching glob {pattern!r}, found {count}. "
        f"Generate them (use a small Python script + shell rather than write_file with "
        f"a huge literal — escapes break easily).",
    )


def _run_regex_in_file(spec: dict) -> tuple[bool, str]:
    path = spec["path"]
    pattern = spec["pattern"]
    resolved = _resolve(path)

    if not resolved.exists():
        return (
            False,
            f"Expected file {path!r} to exist (for regex check {pattern!r}), "
            f"but it does not. Create it first.",
        )

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (
            False,
            f"Could not read {path!r}: {type(e).__name__}: {e}. Fix file permissions or path.",
        )

    try:
        compiled = re.compile(pattern)
    except re.error as e:
        # validate_criterion should have caught this, but be safe.
        return False, f"Regex {pattern!r} did not compile: {e}."

    if compiled.search(text) is None:
        # Try multiline since spec example uses re.MULTILINE; re-search if it
        # makes a difference (cheap, gives us a second chance).
        try:
            compiled_ml = re.compile(pattern, re.MULTILINE)
            if compiled_ml.search(text) is not None:
                return True, ""
        except re.error:
            pass
        return (
            False,
            f"Expected regex {pattern!r} to match in {path!r}, not found. "
            f"Add the section.",
        )

    return True, ""


def _run_shell_returns_zero(spec: dict) -> tuple[bool, str]:
    cmd = spec["cmd"]
    raw_timeout = spec.get("timeout", _SHELL_TIMEOUT_DEFAULT)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = float(_SHELL_TIMEOUT_DEFAULT)
    if timeout <= 0:
        timeout = float(_SHELL_TIMEOUT_DEFAULT)
    if timeout > _SHELL_TIMEOUT_MAX:
        timeout = float(_SHELL_TIMEOUT_MAX)

    cwd = _workspace_root()
    # Ensure cwd exists so subprocess.run doesn't blow up on a fresh install.
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't create it, fall back to the parent / cwd. Don't crash.
        cwd_arg: str | None = None
    else:
        cwd_arg = str(cwd)

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            cwd=cwd_arg,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"Shell command {cmd!r} timed out after {timeout:g}s. "
            f"Reduce the work or fix the underlying hang.",
        )
    except OSError as e:
        return (
            False,
            f"Could not execute shell command {cmd!r}: {type(e).__name__}: {e}.",
        )

    if proc.returncode == 0:
        return True, ""

    # Decode stderr safely and trim to 200 chars.
    stderr_bytes = proc.stderr or b""
    try:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        stderr_text = repr(stderr_bytes)
    stderr_snip = stderr_text.strip()[:200]
    stderr_part = f" stderr: {stderr_snip}." if stderr_snip else ""

    return (
        False,
        f"Shell command exited with code {proc.returncode}, expected 0.{stderr_part} "
        f"Fix the underlying issue.",
    )


def _run_http_200(spec: dict) -> tuple[bool, str]:
    url = spec["url"]

    # Bounded redirect handler (spec: max 3 redirects).
    class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
        max_redirections = _HTTP_MAX_REDIRECTS

    opener = urllib.request.build_opener(_BoundedRedirectHandler())

    try:
        with opener.open(url, timeout=_HTTP_TIMEOUT) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                # urllib < 3.9 fallback
                status = resp.getcode()
    except urllib.error.HTTPError as e:
        # 4xx / 5xx come back here.
        return (
            False,
            f"HTTP GET {url} returned {e.code}. "
            f"Wait and retry, or check the URL is correct.",
        )
    except urllib.error.URLError as e:
        return (
            False,
            f"HTTP GET {url} failed: URLError: {e.reason}. "
            f"Check network connectivity or the URL.",
        )
    except (TimeoutError, OSError) as e:
        return (
            False,
            f"HTTP GET {url} failed: {type(e).__name__}: {e}. "
            f"Check network connectivity or retry.",
        )
    except Exception as e:  # noqa: BLE001 — defensive, never propagate
        return (
            False,
            f"HTTP GET {url} failed: {type(e).__name__}: {e}.",
        )

    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return False, f"HTTP GET {url} returned non-numeric status {status!r}."

    if _HTTP_PASS_LO <= status_int <= _HTTP_PASS_HI:
        return True, ""

    return (
        False,
        f"HTTP GET {url} returned {status_int}. "
        f"Wait and retry, or check the URL is correct.",
    )
