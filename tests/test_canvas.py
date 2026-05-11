"""Tests for the built-in `canvas` skill + its server-side plumbing.

Coverage:
- Schema migration applied (canvas_artifacts table exists on fresh DB)
- REST artifacts round-trip (POST → GET list → GET single → DELETE)
- Size cap rejection (256 KB)
- Slug auto-generation from title
- Slug overwrite semantics (idempotent upsert)
- Tool dispatch happy paths (render / save / load / list) with mocked
  server module
- canvas_render returns helpful message when no WS client connected
- JS contract pins for static/index.html — iframe sandbox attrs +
  canvas_render/close branches positioned BEFORE the streaming-message
  creation gate (same v0.18.3 ghost-message regression discipline as
  task_update)
- Skill is in `_DEFAULT_SKILLS` so it's auto-active on every install
- tool_search keywords activate the canvas tools

`pyserial`/`opencv`-style mocking pattern: any test that touches
server._broadcast monkeypatches it inline to avoid running a live
event loop.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


# ── Skill module loader (matches the pattern used by other skill tests) ──


def _load_canvas_skill():
    spec = importlib.util.spec_from_file_location(
        "_canvas_under_test",
        Path(__file__).resolve().parent.parent / "skills" / "canvas.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def canvas_mod():
    return _load_canvas_skill()


@pytest.fixture
def fresh_server(qwe_temp_data_dir, monkeypatch):
    """Reload server against a fresh QWE_DATA_DIR — guarantees the
    migrations apply against an empty kv table, the canvas_artifacts
    table is created, and we get a fresh TestClient."""
    for mod in ("config", "db", "soul", "threads", "presets", "server"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)
    import server
    return server


@pytest.fixture
def http_client(fresh_server):
    from fastapi.testclient import TestClient
    with TestClient(fresh_server.app) as c:
        yield c


# ── Schema ─────────────────────────────────────────────────────────


def test_migration_creates_canvas_artifacts_table(fresh_server):
    import db
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='canvas_artifacts'"
    )
    assert row is not None, "canvas_artifacts table not created by migration"


# ── REST artifacts ─────────────────────────────────────────────────


def test_artifact_post_get_delete_round_trip(http_client):
    payload = {
        "slug": "weekly-sales",
        "title": "Weekly sales dashboard",
        "html": "<!doctype html><h1>Sales</h1>",
    }
    r = http_client.post("/api/canvas/artifacts", json=payload)
    assert r.status_code == 200
    assert r.json()["slug"] == "weekly-sales"

    r = http_client.get("/api/canvas/artifacts")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["slug"] == "weekly-sales"
    assert items[0]["title"] == "Weekly sales dashboard"
    # List view never sends html — keeps payloads small
    assert "html" not in items[0]

    r = http_client.get("/api/canvas/artifacts/weekly-sales")
    assert r.status_code == 200
    got = r.json()
    assert got["slug"] == "weekly-sales"
    assert "<h1>Sales</h1>" in got["html"]

    r = http_client.delete("/api/canvas/artifacts/weekly-sales")
    assert r.status_code == 200

    r = http_client.get("/api/canvas/artifacts/weekly-sales")
    assert r.status_code == 404


def test_post_rejects_oversize_html(http_client):
    big = "x" * (256 * 1024 + 1)  # 1 byte past the 256 KB cap
    r = http_client.post("/api/canvas/artifacts",
                          json={"slug": "big", "title": "Big", "html": big})
    assert r.status_code == 413
    assert "256" in r.text or "cap" in r.text.lower()


def test_post_requires_html(http_client):
    r = http_client.post("/api/canvas/artifacts",
                          json={"slug": "empty", "title": "Empty"})
    assert r.status_code == 400


def test_post_autogenerates_slug_from_title(http_client):
    r = http_client.post("/api/canvas/artifacts",
                          json={"title": "Weekly Sales Q3 2026!",
                                "html": "<h1>x</h1>"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    # Slugified: lowercased, non-alphanumeric → '-', trimmed
    assert slug == "weekly-sales-q3-2026"
    # Re-fetch by the generated slug
    r2 = http_client.get(f"/api/canvas/artifacts/{slug}")
    assert r2.status_code == 200


def test_post_is_idempotent_upsert(http_client):
    """Re-POSTing the same slug updates the row (and bumps updated_at)
    without creating a duplicate."""
    http_client.post("/api/canvas/artifacts",
                      json={"slug": "a", "title": "First", "html": "<p>1</p>"})
    http_client.post("/api/canvas/artifacts",
                      json={"slug": "a", "title": "Second", "html": "<p>2</p>"})
    items = http_client.get("/api/canvas/artifacts").json()["items"]
    assert len(items) == 1
    full = http_client.get("/api/canvas/artifacts/a").json()
    assert full["title"] == "Second"
    assert "<p>2</p>" in full["html"]


def test_post_with_empty_title_slugifies_to_random(http_client):
    """No title, no slug → random short uuid slug so we never collide."""
    r = http_client.post("/api/canvas/artifacts",
                          json={"html": "<p>1</p>"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert slug and slug != ""
    assert len(slug) >= 4


# ── Skill module dispatch ──────────────────────────────────────────


def test_skill_exposes_five_tools(canvas_mod):
    names = [t["function"]["name"] for t in canvas_mod.TOOLS]
    assert names == [
        "canvas_render", "canvas_prompt",
        "canvas_save", "canvas_load", "canvas_list",
    ]


def test_skill_has_description_and_instruction(canvas_mod):
    assert isinstance(canvas_mod.DESCRIPTION, str) and len(canvas_mod.DESCRIPTION) > 30
    assert isinstance(canvas_mod.INSTRUCTION, str)
    # Must teach the model the postMessage protocol
    assert "postMessage" in canvas_mod.INSTRUCTION
    assert "canvas_submit" in canvas_mod.INSTRUCTION
    # Must teach the blocking semantics
    assert "canvas_prompt" in canvas_mod.INSTRUCTION
    assert "BLOCKS" in canvas_mod.INSTRUCTION


def test_unknown_tool_returns_friendly_error(canvas_mod):
    out = canvas_mod.execute("canvas_nope", {})
    assert "Unknown tool" in out


def test_canvas_render_validates_html(canvas_mod):
    assert "html" in canvas_mod.execute("canvas_render", {}).lower()
    assert "256 KB" in canvas_mod.execute(
        "canvas_render", {"html": "x" * (256 * 1024 + 1)}
    )


def test_canvas_prompt_validates_html(canvas_mod):
    assert "html" in canvas_mod.execute("canvas_prompt", {}).lower()


def test_canvas_tools_report_stale_server_with_restart_hint(canvas_mod, monkeypatch):
    """Reported bug: user pulled the canvas commit but didn't restart
    `qwe-qwe --web`. The running server process is from BEFORE the
    canvas-helper additions, so `server.broadcast_canvas_render_sync`
    doesn't exist and the agent saw a raw AttributeError.

    Fix: skills/canvas.py uses _check_server_compat() to detect the
    missing helpers up front and return a clear "restart qwe-qwe"
    message instead of crashing.
    """
    import types
    stale = types.ModuleType("server")
    # Pretend it's a live process — has _ws_loop and a client — but
    # missing every canvas helper. This is exactly what happens when
    # the running server.py is older than the canvas.py on disk.
    stale._ws_loop = object()
    stale._ws_clients = {object()}
    # Intentionally no broadcast_canvas_render_sync,
    # request_canvas_prompt_sync, _canvas_save_artifact.

    monkeypatch.setattr(canvas_mod, "_server_module", lambda: stale)

    for name, args in [
        ("canvas_render", {"html": "<p>x</p>"}),
        ("canvas_prompt", {"html": "<p>x</p>"}),
        ("canvas_save", {"slug": "x", "html": "<p>x</p>"}),
        ("canvas_load", {"slug": "missing-but-irrelevant"}),
    ]:
        out = canvas_mod.execute(name, args)
        assert "older than the canvas skill" in out, (
            f"{name} didn't surface a restart hint when server is stale; got: {out!r}"
        )
        # Must enumerate at least one missing helper so the user knows
        # what's actually wrong, not just "something's broken".
        assert "broadcast_canvas_render_sync" in out or "request_canvas_prompt_sync" in out or "_canvas_save_artifact" in out


def test_canvas_render_without_ws_returns_helpful_message(canvas_mod):
    """No WS client connected → tool returns a message the agent can
    relay to the user, not a stacktrace."""
    # The lazy server lookup returns sys.modules.get('server') which
    # may be None or have no _ws_clients attribute — either way the
    # skill should report cleanly.
    if "server" in sys.modules:
        srv = sys.modules["server"]
        # Force the no-client path
        old_clients = getattr(srv, "_ws_clients", None)
        srv._ws_clients = set()
        try:
            out = canvas_mod.execute("canvas_render", {"html": "<p>x</p>"})
            assert "no Web UI client" in out.lower() or "no web ui client" in out.lower()
        finally:
            if old_clients is not None:
                srv._ws_clients = old_clients
    else:
        out = canvas_mod.execute("canvas_render", {"html": "<p>x</p>"})
        assert "no Web UI client" in out.lower() or "server module not loaded" in out.lower()


def test_canvas_save_persists_via_skill(canvas_mod, fresh_server):
    """canvas_save routes through server._canvas_save_artifact, which
    writes to canvas_artifacts. Verify by SELECT."""
    out = canvas_mod.execute("canvas_save", {
        "slug": "from-skill",
        "title": "From the skill",
        "html": "<p>from skill</p>",
    })
    assert "saved" in out.lower()
    import db
    row = db.fetchone("SELECT slug, title, html FROM canvas_artifacts WHERE slug=?",
                       ("from-skill",))
    assert row is not None
    assert row[0] == "from-skill"
    assert row[1] == "From the skill"
    assert "<p>from skill</p>" in row[2]


def test_canvas_save_rejects_oversize_at_skill_layer(canvas_mod):
    """The skill enforces the cap before even calling the server
    helper — keeps tool errors clean even when REST is bypassed."""
    out = canvas_mod.execute("canvas_save", {
        "slug": "big", "title": "Big",
        "html": "x" * (256 * 1024 + 1),
    })
    assert "256 KB" in out


def test_canvas_list_renders_markdown_table(canvas_mod, fresh_server):
    import db
    import time
    db.execute(
        "INSERT INTO canvas_artifacts (slug,title,html,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        ("alpha", "Alpha dashboard", "<p>a</p>", time.time(), time.time()),
    )
    db.execute(
        "INSERT INTO canvas_artifacts (slug,title,html,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        ("beta", "Beta form", "<form></form>", time.time(), time.time()),
    )
    out = canvas_mod.execute("canvas_list", {})
    assert "alpha" in out
    assert "Alpha dashboard" in out
    assert "beta" in out
    assert "| Slug | Title" in out  # markdown header row present


def test_canvas_list_empty(canvas_mod, fresh_server):
    out = canvas_mod.execute("canvas_list", {})
    assert "No saved canvas artifacts" in out


def test_canvas_load_missing_slug_friendly_error(canvas_mod, fresh_server):
    out = canvas_mod.execute("canvas_load", {"slug": "nonexistent"})
    assert "not found" in out.lower()


def test_canvas_load_requires_slug(canvas_mod):
    out = canvas_mod.execute("canvas_load", {})
    assert "slug" in out.lower()


# ── Wiring: skill auto-active + tool_search keywords ───────────────


def test_skill_is_in_default_active_set():
    if "skills" in sys.modules:
        importlib.reload(sys.modules["skills"])
    import skills as skills_mod
    assert "canvas" in skills_mod._DEFAULT_SKILLS


def test_tool_search_keywords_resolve():
    if "tools" in sys.modules:
        importlib.reload(sys.modules["tools"])
    import tools
    expected_full = {"canvas_render", "canvas_prompt",
                     "canvas_save", "canvas_load", "canvas_list"}
    assert set(tools._TOOL_SEARCH_INDEX["canvas"]) == expected_full
    # Synonyms map to relevant subsets
    for kw, expected_subset in [
        ("dashboard", {"canvas_render", "canvas_save", "canvas_list", "canvas_load"}),
        ("form", {"canvas_prompt", "canvas_render"}),
        ("mockup", {"canvas_render", "canvas_save"}),
        ("survey", {"canvas_prompt"}),
        ("artifact", {"canvas_save", "canvas_list", "canvas_load"}),
    ]:
        got = set(tools._TOOL_SEARCH_INDEX[kw])
        assert got == expected_subset, f"{kw!r}: got {got}, expected {expected_subset}"


# ── JS contract pins (static/index.html) ───────────────────────────


def _read_index_html() -> str:
    return (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


def test_canvas_iframe_has_sandbox_no_same_origin():
    """The iframe MUST be sandboxed without `allow-same-origin`.
    Otherwise model-generated HTML could read parent cookies /
    localStorage / DOM — defeating the whole security boundary."""
    src = _read_index_html()
    # Find the canvas iframe declaration
    idx = src.find('data-canvas-iframe')
    assert idx >= 0, "data-canvas-iframe attribute not found in static/index.html"
    # Look a small window around it for the iframe tag
    window = src[max(0, idx - 400): idx + 100]
    assert 'sandbox="allow-scripts allow-forms"' in window, (
        "canvas iframe missing sandbox=\"allow-scripts allow-forms\" — "
        "this is the load-bearing security attribute. Without it the "
        "iframe can run unrestricted scripts in the parent's origin."
    )
    # Critical: NOT same-origin. Greedily check the iframe tag region.
    assert "allow-same-origin" not in window, (
        "canvas iframe MUST NOT have allow-same-origin in its sandbox. "
        "Adding it defeats the boundary — model HTML could read "
        "parent cookies / localStorage / DOM."
    )


def test_canvas_handlers_short_circuit_before_streaming_gate():
    """Same v0.18.3 ghost-message discipline as task_update / get_frame:
    canvas_render / canvas_close MUST be handled BEFORE the
    `!state.streaming && t !== 'status'` gate that creates a pending
    assistant message. Otherwise rendering a dashboard would also pop
    a blank streaming message into the chat."""
    src = _read_index_html()
    render_at = src.find("t === 'canvas_render'")
    close_at = src.find("t === 'canvas_close'")
    gate_at = src.find("!state.streaming && t !== 'status'")
    assert render_at >= 0, "canvas_render WS branch not found"
    assert close_at >= 0, "canvas_close WS branch not found"
    assert gate_at >= 0, "streaming-message gate not found — was its wording changed?"
    assert render_at < gate_at, "canvas_render must short-circuit BEFORE the streaming-message gate"
    assert close_at < gate_at, "canvas_close must short-circuit BEFORE the streaming-message gate"


def test_postmessage_listener_filters_by_iframe_identity():
    """The parent listener MUST verify `event.source === canvasIframe.
    contentWindow` rather than trusting `event.origin` — sandboxed-no-
    same-origin iframes report origin "null", so origin is useless for
    filtering. The identity check is the load-bearing trust step."""
    src = _read_index_html()
    idx = src.find("window.addEventListener('message'")
    assert idx >= 0, "postMessage listener not found"
    window = src[idx: idx + 2000]
    assert "e.source !== iframe.contentWindow" in window, (
        "postMessage listener must filter by iframe identity "
        "(e.source !== iframe.contentWindow). Origin-based filtering "
        "doesn't work because sandboxed-no-same-origin iframes have "
        "origin \"null\"."
    )


def test_canvases_view_wired_in_router():
    src = _read_index_html()
    assert "case 'canvases':" in src
    assert "renderCanvasesView" in src
    assert "{ id: 'canvases'" in src  # left rail entry


def test_composer_has_canvas_toggle_button():
    """Reported gap: 'нет кнопки включить canvas'. Fix added an icon
    button in the chat composer (next to attach / image / camera /
    voice) that opens the canvas panel manually — three-mode toggle:
    close if open, reopen-last if there's a session artifact, or
    route to the Canvases gallery if neither. This test pins the
    affordance is wired so a future refactor that drops the button
    fails loud."""
    src = _read_index_html()
    assert 'data-act="canvas-toggle-composer"' in src, (
        "Composer-side canvas toggle button missing — users have no "
        "way to manually open the canvas panel without an agent-driven "
        "render. Restore the button in the composer .left action group."
    )


def test_state_tracks_thread_canvases():
    """The threadCanvases array survives the panel close, so a user
    who renders 3 dashboards in a thread and closes them can still
    re-open any of them via the chip strip. Pinned at the WS handler
    and the state declaration so neither can quietly disappear."""
    src = _read_index_html()
    assert "threadCanvases: []" in src or "threadCanvases:[]" in src, (
        "state.threadCanvases missing from initial state."
    )
    # Push entry on canvas_render must happen
    handler_at = src.find("t === 'canvas_render'")
    assert handler_at >= 0
    window = src[handler_at: handler_at + 1500]
    assert "state.threadCanvases" in window and "push(" in window, (
        "canvas_render handler doesn't push to threadCanvases — the "
        "session-artifacts chip strip will stay empty even after the "
        "agent renders."
    )


def test_canvas_strip_chips_have_reopen_handler():
    src = _read_index_html()
    assert 'data-act="reopen-canvas"' in src, (
        "canvas-strip chips missing the reopen handler hook."
    )
    # Reopen handler must restore state.canvas from threadCanvases by index
    handler_at = src.find('data-act="reopen-canvas"')
    # Search globally for the click handler block
    wire_idx = src.find('data-act="reopen-canvas"]\').forEach')
    assert wire_idx >= 0, "reopen-canvas click handler not wired in wireEvents"
    window = src[wire_idx: wire_idx + 800]
    assert "state.threadCanvases" in window
    assert "state.canvas = {" in window


def test_thread_switch_clears_threadCanvases():
    """When the user switches threads, the session-artifacts strip must
    reset — otherwise old canvases would bleed into the new thread."""
    src = _read_index_html()
    load_at = src.find("async function loadActiveMessages")
    assert load_at >= 0
    window = src[load_at: load_at + 2000]
    assert "state.threadCanvases = []" in window, (
        "loadActiveMessages doesn't reset state.threadCanvases on "
        "thread switch — the chip strip will leak across threads."
    )
