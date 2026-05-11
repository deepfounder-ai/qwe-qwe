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
    window = src[wire_idx: wire_idx + 2000]
    assert "state.threadCanvases" in window
    assert "state.canvas = {" in window


def test_thread_switch_clears_threadCanvases():
    """When the user switches threads, the session-artifacts strip must
    reset — otherwise old canvases would bleed into the new thread."""
    src = _read_index_html()
    load_at = src.find("async function loadActiveMessages")
    assert load_at >= 0
    window = src[load_at: load_at + 3500]
    assert "state.threadCanvases = []" in window, (
        "loadActiveMessages doesn't reset state.threadCanvases on "
        "thread switch — the chip strip will leak across threads."
    )


# ── meta.canvas persistence round-trip ────────────────────────────


def test_track_canvas_render_appends_to_pending(fresh_server):
    """The skill's broadcast helpers call _track_canvas_render_for_
    history after each successful broadcast. Without this drain, the
    chip strip would be a session-only thing — close the tab and
    everything's gone. This is the load-bearing function for canvas
    persistence."""
    fresh_server._pending_canvas_renders.clear()
    fresh_server._track_canvas_render_for_history(
        title="Weekly sales", html="<h1>Sales</h1>", slug="weekly-sales",
    )
    assert len(fresh_server._pending_canvas_renders) == 1
    entry = fresh_server._pending_canvas_renders[0]
    assert entry["title"] == "Weekly sales"
    assert entry["slug"] == "weekly-sales"
    assert entry["html"] == "<h1>Sales</h1>"
    assert entry["html_size"] > 0
    assert entry.get("truncated") is not True
    assert "ts" in entry


def test_track_canvas_render_truncates_oversize_html(fresh_server):
    """HTML over 64KB shouldn't blow up the messages table — entry
    stores metadata + truncated:True marker and skips the actual html
    body. Chip still shows up so the user knows a canvas WAS rendered;
    clicking it then explains the truncation."""
    fresh_server._pending_canvas_renders.clear()
    big_html = "x" * (fresh_server._CANVAS_META_HTML_CAP + 100)
    fresh_server._track_canvas_render_for_history(
        title="Big dashboard", html=big_html, slug=None,
    )
    assert len(fresh_server._pending_canvas_renders) == 1
    entry = fresh_server._pending_canvas_renders[0]
    assert entry["title"] == "Big dashboard"
    assert entry["truncated"] is True
    assert entry["html"] == ""
    assert entry["html_size"] >= fresh_server._CANVAS_META_HTML_CAP


def test_track_canvas_render_swallows_bad_input(fresh_server):
    """Robustness: a misbehaving caller (passes a dict, an int, None)
    shouldn't crash the agent turn. Just no-op."""
    fresh_server._pending_canvas_renders.clear()
    fresh_server._track_canvas_render_for_history("t", None, None)
    fresh_server._track_canvas_render_for_history("t", 42, None)
    fresh_server._track_canvas_render_for_history("t", {"oops": 1}, None)
    assert len(fresh_server._pending_canvas_renders) == 0


def test_broadcast_helper_appends_render_to_pending(fresh_server, monkeypatch):
    """Integration: calling broadcast_canvas_render_sync (the public
    helper used by skills/canvas.py) appends to the pending list when
    a WS client is connected. Without this connection, neither the
    broadcast nor the tracking happens (no chip on reload — the user
    just sees nothing, which is correct for headless / CLI sessions)."""
    fresh_server._pending_canvas_renders.clear()
    # Simulate a connected WS client + event loop. The actual
    # asyncio.run_coroutine_threadsafe will be called against a fake
    # loop; we don't need it to actually run, just need the helper to
    # take the success path.
    fresh_server._ws_clients = {object()}
    fresh_server._ws_loop = type("L", (), {})()  # truthy

    # Replace asyncio.run_coroutine_threadsafe to no-op so we don't
    # actually try to schedule on the fake loop. Close the coroutine
    # the helper passes in so pytest doesn't warn "coroutine was
    # never awaited" — it's not a real bug, just test-side hygiene.
    import asyncio
    def _consume(coro, loop):
        try:
            coro.close()
        except Exception:
            pass
        return type("F", (), {"result": lambda *a, **k: None})()
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _consume)

    ok = fresh_server.broadcast_canvas_render_sync(
        html="<p>test</p>", title="Test", slug="test-slug",
    )
    assert ok is True
    assert len(fresh_server._pending_canvas_renders) == 1
    entry = fresh_server._pending_canvas_renders[0]
    assert entry["title"] == "Test"
    assert entry["slug"] == "test-slug"
    assert entry["html"] == "<p>test</p>"


def test_loadActiveMessages_rebuilds_threadCanvases_from_meta():
    """The frontend reload path walks `meta.canvas` arrays on each
    message and populates state.threadCanvases. Without this, the
    composer's chip strip + "re-open last canvas" button would only
    work within a single session — F5 / tab close / thread switch
    would silently lose history. Source-grep contract."""
    src = _read_index_html()
    # The .map() walk must check meta.canvas
    idx = src.find("if (Array.isArray(meta.canvas))")
    assert idx >= 0, (
        "loadActiveMessages doesn't walk meta.canvas — chip strip "
        "won't survive reload."
    )
    window = src[idx: idx + 800]
    assert "state.threadCanvases" in window
    # Both saved-with-slug and inline html entries must be supported
    assert "title" in window and "slug" in window and "html" in window


def test_truncated_canvas_chip_explains_or_falls_back_to_artifacts():
    """If a chip's source render was truncated (HTML > 64KB) AND no
    slug was supplied, clicking should toast a helpful message rather
    than open a blank panel. If a slug WAS supplied, fetch the full
    html from /api/canvas/artifacts as fallback. Source-grep."""
    src = _read_index_html()
    # Find the reopen handler
    handler_at = src.find("data-act=\"reopen-canvas\"]').forEach")
    assert handler_at >= 0
    window = src[handler_at: handler_at + 1500]
    # Truncated-aware branch
    assert "truncated" in window
    # Fetches from artifacts API when slug exists
    assert "/api/canvas/artifacts/" in window or "canvas/artifacts/" in window
    # Friendly toast when no slug
    assert "too large to persist inline" in window or "ask the agent to re-render" in window


def test_ws_reply_assembly_drains_pending_renders():
    """The WS reply assembly in server.py must drain
    _pending_canvas_renders into the assistant message's meta.canvas.
    Without this drain step, the per-turn captures would leak into
    the next turn (until cleared) AND nothing would land in the DB
    for reload restoration. Source-grep contract."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    # The reply assembly must read + clear the list AND assign to
    # meta_dict["canvas"]
    assert "_pending_canvas_renders.clear()" in src or "pending_canvases" in src, (
        "WS reply handler doesn't drain _pending_canvas_renders — "
        "either entries leak to next turn or never reach DB."
    )
    assert 'meta_dict["canvas"] = pending_canvases' in src, (
        "WS reply handler doesn't write _pending_canvas_renders into "
        "meta_dict[\"canvas\"] — reload restoration is broken."
    )


# ── Code-review follow-up fixes ────────────────────────────────────


def test_save_request_postMessage_path_is_not_handled():
    """Code review caught: the iframe is sandboxed but can postMessage
    arbitrary {slug, title, html} to the parent. If we accepted
    'canvas_save_request' and wrote to canvas_artifacts, model-generated
    HTML could overwrite any saved slug (including one the user trusts
    by name). Saving must remain parent-chrome-only via the
    authenticated REST POST. This test pins that the postMessage
    listener does NOT relay save requests to the WS layer."""
    src = _read_index_html()
    # Find the postMessage listener
    listener_at = src.find("window.addEventListener('message'")
    assert listener_at >= 0
    # Walk to the next top-level statement (window.addEventListener body)
    listener_block = src[listener_at: listener_at + 3000]
    # Must handle submit + close requests
    assert "canvas_submit" in listener_block
    assert "canvas_close_request" in listener_block
    # Must NOT handle save requests — that path lets model HTML
    # overwrite arbitrary artifacts.
    assert "canvas_save_request" not in listener_block, (
        "Iframe → parent postMessage listener is relaying "
        "canvas_save_request. This re-opens the model-can-overwrite-"
        "arbitrary-slug bug. Saving is parent-chrome-only via the "
        "authenticated REST POST /api/canvas/artifacts."
    )


def test_ws_canvas_event_save_branch_removed():
    """Mirror of the JS test on the server side. The WS handler used to
    accept event='save' from canvas_event and write to the DB
    untrusted. Removed in the code-review follow-up."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    # Find the canvas_event handler
    handler_at = src.find('msg.get("type") == "canvas_event"')
    assert handler_at >= 0
    # Look for the save branch
    window = src[handler_at: handler_at + 3000]
    # The 'save' event_kind MUST NOT trigger _canvas_save_artifact in
    # this handler. We allow a comment referencing 'save' (the rationale)
    # but no executable branch.
    assert 'elif event_kind == "save"' not in window, (
        "WS canvas_event handler still has an executable 'save' branch — "
        "this lets model HTML overwrite any slug in canvas_artifacts. "
        "Remove the branch; saves go through POST /api/canvas/artifacts."
    )


def test_canvas_prompt_does_not_track_failed_renders():
    """Code review caught: _track_canvas_render_for_history was called
    BEFORE await event.wait(), so a form that timed out or was closed
    without submission still landed in meta.canvas. The chip strip
    would then surface dead forms the user never answered.

    Fix: track ONLY after a successful submission. Verify by reading
    server.py source and confirming the track call lives AFTER the
    `if entry.get("closed"): return {}` early-return.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    fn_at = src.find("async def request_canvas_prompt")
    assert fn_at >= 0
    body = src[fn_at: fn_at + 4000]
    track_pos = body.find("_track_canvas_render_for_history")
    closed_check_pos = body.find('entry.get("closed")')
    assert track_pos >= 0 and closed_check_pos >= 0
    assert track_pos > closed_check_pos, (
        "_track_canvas_render_for_history is being called before the "
        "submit/close branching — failed prompts will leak into "
        "meta.canvas as 'ghost' chips. Track only on successful submit."
    )


def test_scheduler_normalizes_tool_call_arguments():
    """Code review caught: scheduler.py:1082 builds function.arguments
    raw from tc.function.arguments. Scheduled tasks hit the same
    Alibaba DashScope 400 ("function.arguments must be in JSON format")
    that agent_loop + agent already fix via normalize_args_for_api.

    Pin the contract: scheduler.py must reference normalize_args_for_api
    in its assistant_msg construction. Source-grep matches the existing
    JS-contract test pattern for agent_loop/agent."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scheduler.py").read_text(encoding="utf-8")
    assert "normalize_args_for_api" in src, (
        "scheduler.py no longer routes tool_call arguments through "
        "normalize_args_for_api. Scheduled tasks against Alibaba "
        "DashScope will 400 with the InvalidParameter bug fixed in "
        "agent_loop+agent — this is the same fix at the third call site."
    )


def test_canvas_hash_avoids_prefix_collision():
    """The previous dedup key was `length + ':' + html.slice(0,32)`.
    Two different dashboards both starting with `<!doctype html>` and
    happening to be the same length would collide. New `canvasHash`
    helper folds the whole string into a 32-bit number, so collisions
    are exponentially rare. Pin the function exists + the call sites
    use it."""
    src = _read_index_html()
    # Helper exists
    assert "const canvasHash = (s) =>" in src, "canvasHash helper missing"
    # patchCanvasIframe uses it
    patch_at = src.find("function patchCanvasIframe")
    assert patch_at >= 0
    patch_window = src[patch_at: patch_at + 1000]
    assert "canvasHash(html)" in patch_window
    # Live WS canvas_render handler dedup uses it
    handler_at = src.find("t === 'canvas_render'")
    assert handler_at >= 0
    handler_window = src[handler_at: handler_at + 1500]
    assert "canvasHash(" in handler_window


def test_gallery_open_pushes_to_threadCanvases():
    """Code review caught: opening a canvas from the gallery view set
    state.canvas but did NOT push into state.threadCanvases. That left
    the composer's chip strip empty even though a canvas was currently
    open — inconsistent with the agent-rendered flow. Pin the fix."""
    src = _read_index_html()
    handler_at = src.find('data-act="canvas-open"]\').forEach')
    assert handler_at >= 0
    window = src[handler_at: handler_at + 1500]
    assert "state.threadCanvases" in window and "push(" in window, (
        "Gallery canvas-open handler doesn't push the opened artifact "
        "into state.threadCanvases — chip strip will silently miss it "
        "compared to agent-rendered canvases."
    )
