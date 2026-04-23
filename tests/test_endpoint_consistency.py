"""Endpoint consistency: every /api/... URL the Web UI calls must exist on the server.

Why: ``static/index.html`` is a ~6 000-line single-file SPA; a typo in a path
(``/api/threds`` instead of ``/api/threads``) would only surface at runtime as a
404 toast. This test parses every ``api('/api/…')`` / ``fetch('/api/…')`` call
site out of the HTML and confirms each matches a route mounted on ``server.app``.

How matching works:
- UI literal paths are extracted with a regex that grabs ``/api/`` followed by
  word / slash / hyphen characters. Trailing slashes are trimmed (they
  typically sit right before a ``' + var + '/…`` concat).
- Server routes come from ``server.app.routes``; FastAPI templated segments
  like ``{thread_id}`` are treated as single-segment wildcards.
- A UI path matches a server route if either (a) they are equal, or (b) the
  UI path equals the server route's static prefix up to the first ``{…}``
  segment (covers the ``/api/threads/`` concatenation case).

Failures print every unmatched UI path — run ``pytest tests/test_endpoint_consistency.py -v``
to see which call sites are broken.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import server

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INDEX_HTML = _REPO_ROOT / "static" / "index.html"

# /api/ followed by word/slash/hyphen chars. Stops at query string, quote,
# closing paren, whitespace, ``+`` concat, ``{`` / ``}``, ``?``, ``#``.
_UI_PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-/]+")

# Known false positives: strings the regex picks up that aren't real UI calls.
# Keep this list empty when possible; add with an explanation when needed.
_IGNORE_UI_PATHS: set[str] = set()


def _extract_ui_paths(text: str) -> set[str]:
    """Return the set of normalized UI API paths referenced in ``text``."""
    found: set[str] = set()
    for raw in _UI_PATH_RE.findall(text):
        # Trim trailing slash (concat prefix like ``/api/threads/' + id``).
        path = raw.rstrip("/")
        if path:
            found.add(path)
    return found - _IGNORE_UI_PATHS


def _static_prefix(server_path: str) -> str:
    """Return everything before the first ``{…}`` segment in a FastAPI path.

    ``/api/threads/{thread_id}/switch`` → ``/api/threads``.
    ``/api/threads`` → ``/api/threads`` (no templating, unchanged).
    """
    idx = server_path.find("/{")
    if idx == -1:
        return server_path
    return server_path[:idx]


def _full_segment_match(ui_path: str, server_path: str) -> bool:
    """True if every server segment either equals the UI segment or is a ``{…}`` wildcard.

    Handles the case where a UI literal fills a templated slot, e.g.
    ``/api/kv/spicy_duck`` matching server template ``/api/kv/{key}``.
    """
    ui_segs = ui_path.split("/")
    srv_segs = server_path.split("/")
    if len(ui_segs) != len(srv_segs):
        return False
    for u, s in zip(ui_segs, srv_segs, strict=True):
        if s.startswith("{") and s.endswith("}"):
            continue  # wildcard segment
        if u != s:
            return False
    return True


@pytest.fixture(scope="module")
def server_api_routes() -> list[str]:
    """All /api/... route templates registered on the FastAPI app."""
    seen: set[str] = set()
    for route in server.app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path.startswith("/api/"):
            seen.add(path)
    assert seen, "no /api/ routes found on server.app — did the import break?"
    return sorted(seen)


@pytest.fixture(scope="module")
def ui_api_paths() -> set[str]:
    """All /api/... paths extracted from static/index.html."""
    text = _INDEX_HTML.read_text(encoding="utf-8")
    paths = _extract_ui_paths(text)
    assert paths, "no /api/ calls found in static/index.html — extraction regex likely broke"
    return paths


def _matches_any(ui_path: str, server_paths: list[str]) -> bool:
    for sp in server_paths:
        if ui_path == sp:
            return True
        # UI path fills a templated route segment-for-segment
        # (e.g. /api/kv/spicy_duck ↔ /api/kv/{key}).
        if _full_segment_match(ui_path, sp):
            return True
        # UI path is the literal prefix of a templated route, used in
        # concatenations like ``'/api/threads/' + id + '/switch'`` where
        # the regex only captures the static leading portion.
        if ui_path == _static_prefix(sp):
            return True
    return False


def test_every_ui_api_call_has_a_server_route(
    ui_api_paths: set[str], server_api_routes: list[str]
) -> None:
    """Every /api/... referenced by the Web UI must map to a mounted route."""
    missing = sorted(p for p in ui_api_paths if not _matches_any(p, server_api_routes))
    assert not missing, (
        f"UI calls {len(missing)} endpoint(s) the server does not expose:\n  - "
        + "\n  - ".join(missing)
        + "\n\nFix: add the route to server.py, or fix the typo in static/index.html."
    )


def test_extraction_finds_a_reasonable_number_of_calls(ui_api_paths: set[str]) -> None:
    """Guard against the regex silently breaking and extracting ~nothing.

    The UI today references ~60 distinct API paths. If this drops below 40,
    the regex probably broke — fail loudly so someone investigates rather
    than letting the consistency check pass vacuously.
    """
    assert len(ui_api_paths) >= 40, (
        f"only {len(ui_api_paths)} UI API paths extracted; regex may be broken. "
        f"Found: {sorted(ui_api_paths)}"
    )


def test_server_has_expected_route_floor(server_api_routes: list[str]) -> None:
    """Guard against an import-time failure that silently loses routes."""
    assert len(server_api_routes) >= 60, (
        f"only {len(server_api_routes)} /api/ routes found on server.app; "
        "something likely failed to register."
    )
