"""WebSocket attachment handling — document + image upload round-trip.

The v2 composer ships files via the WS ``document`` field (``pendingAttachments``
array in ``static/index.html`` → ``ws.send({text, document: {filename, file_b64}})``).
The server decodes the base64 payload, writes it to ``UPLOADS_DIR``, and
injects a ``[File attached: …]`` reference into ``user_input`` so the agent
can pull the contents on demand with ``read_file``.

These tests lock in that plumbing end-to-end (WS client → server bytes on
disk → agent invocation args) without running the LLM. ``_run_agent_sync``
is monkeypatched to capture its kwargs and return a canned result, so the
test is deterministic and fast.
"""
from __future__ import annotations

import base64
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


# ── Test env bootstrap (mirrors test_integration.py) ───────────────────


@pytest.fixture(scope="module", autouse=True)
def _ws_attach_env():
    """Point QWE_DATA_DIR at a fresh tempdir and reload server."""
    original = os.environ.get("QWE_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_ws_attach_"))
    os.environ["QWE_DATA_DIR"] = str(tmp_root)
    _reload_core()
    try:
        yield tmp_root
    finally:
        _close_db()
        if original is not None:
            os.environ["QWE_DATA_DIR"] = original
        else:
            os.environ.pop("QWE_DATA_DIR", None)
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


@pytest.fixture
def client(monkeypatch):
    """Fresh TestClient per test — WS state is per-connection anyway.

    The WS handler synchronously probes ``{lmstudio|ollama}/api/v1/models``
    with a 5s ``requests.get`` timeout before kicking off the agent. Under
    test we force the active provider to something neither local nor
    cloud, so that branch skips and we don't sit on a 5s DNS failure.
    """
    from fastapi.testclient import TestClient
    import providers
    import server

    # Disable auth so the WS handshake doesn't require a cookie dance
    server._AUTH_PASSWORD = ""
    monkeypatch.setattr(providers, "get_active_name", lambda: "fake", raising=False)
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def captured_agent_call(monkeypatch):
    """Replace server._run_agent_sync with a stub that captures its args.

    Returns a list that each WS turn appends to: ``{user_input, thread_id,
    image_b64, image_path, file_meta, abort_event, ctx}``. The stub returns
    a minimal valid result dict so the WS reply assembly doesn't crash.
    """
    import server

    captured: list[dict] = []

    def _fake_run(user_input, thread_id=None, image_b64=None, image_path=None,
                   file_meta=None, abort_event=None, ctx=None):
        captured.append({
            "user_input": user_input,
            "thread_id": thread_id,
            "image_b64": image_b64,
            "image_path": image_path,
            "file_meta": file_meta,
            "abort_event": abort_event,
            "ctx": ctx,
        })
        return {
            "reply": "ok",
            "thinking": "",
            "tools": [],
            "duration_ms": 1,
            "context_hits": 0,
            "thread_id": thread_id or "t-test",
            "tokens": 0,
            "prompt_tokens": 0,
            "tok_per_sec": 0,
        }

    monkeypatch.setattr(server, "_run_agent_sync", _fake_run, raising=True)
    return captured


def _recv_reply(ws, timeout: float = 3.0) -> dict:
    """Drain WS messages until we see a ``reply`` (or timeout)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = ws.receive_json()
        except Exception:
            break
        if isinstance(msg, dict) and msg.get("type") == "reply":
            return msg
    raise AssertionError("never received a reply frame within timeout")


# ── Document attachment ────────────────────────────────────────────────


def test_ws_document_written_to_uploads_and_referenced_in_user_input(
    client, captured_agent_call
):
    """Send {text, document} → file lands on disk + user_input gets the [File attached: ...] ref."""
    import server

    body = b"hello from the test"
    payload = {
        "text": "summarize this please",
        "document": {
            "filename": "notes.txt",
            "file_b64": base64.b64encode(body).decode("ascii"),
        },
    }

    with client.websocket_connect("/ws") as ws:
        ws.send_json(payload)
        _recv_reply(ws)

    assert len(captured_agent_call) == 1, "agent should have been invoked exactly once"
    call = captured_agent_call[0]

    # user_input got the reference injected after the user's typed text
    assert "summarize this please" in call["user_input"]
    assert "[File attached: notes.txt" in call["user_input"]
    assert "read_file(path)" in call["user_input"], (
        "reference should tell the agent how to read the contents"
    )

    # file_meta was assembled and handed to the agent
    fm = call["file_meta"]
    assert fm is not None
    assert fm["name"] == "notes.txt"
    assert fm["size"] == len(body)

    # The referenced path actually exists and contains the original bytes
    path = Path(fm["path"])
    assert path.exists(), f"upload file not written at {path}"
    assert path.read_bytes() == body
    # And it lives inside UPLOADS_DIR (not somewhere the filename coerced it to)
    assert server.UPLOADS_DIR in path.parents


def test_ws_document_filename_sanitized_against_path_traversal(
    client, captured_agent_call
):
    """A crafted filename with .. / slashes / null bytes cannot escape UPLOADS_DIR."""
    import server

    payload = {
        "text": "",
        "document": {
            "filename": "../../../etc/passwd\x00.txt",
            "file_b64": base64.b64encode(b"evil").decode("ascii"),
        },
    }

    with client.websocket_connect("/ws") as ws:
        ws.send_json(payload)
        _recv_reply(ws)

    call = captured_agent_call[0]
    fm = call["file_meta"]
    assert fm is not None
    path = Path(fm["path"])
    # The resolved path must still be inside UPLOADS_DIR
    assert server.UPLOADS_DIR in path.parents, (
        f"sanitizer failed — upload landed at {path}, outside {server.UPLOADS_DIR}"
    )
    # And no ``..`` or slash survived in the on-disk stem
    assert ".." not in path.name
    assert "\x00" not in path.name


def test_ws_empty_text_plus_document_still_runs_agent(client, captured_agent_call):
    """Dragging a file in with no typed text should still invoke the agent."""
    payload = {
        "text": "",
        "document": {
            "filename": "plain.md",
            "file_b64": base64.b64encode(b"# heading").decode("ascii"),
        },
    }
    with client.websocket_connect("/ws") as ws:
        ws.send_json(payload)
        _recv_reply(ws)

    assert captured_agent_call, "agent not invoked for doc-only message"
    # The reference is still injected even though user_input was empty
    assert "[File attached: plain.md" in captured_agent_call[0]["user_input"]


def test_ws_empty_payload_is_ignored(client, captured_agent_call):
    """No text + no attachment → server silently skips, no agent call."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"text": "", "thread_id": None})
        # Send a throwaway follow-up with a real text to confirm the socket is
        # still healthy AFTER the empty message was ignored.
        ws.send_json({"text": "second try"})
        _recv_reply(ws)

    assert len(captured_agent_call) == 1, (
        f"expected the empty msg to be skipped, got {len(captured_agent_call)} calls"
    )
    assert captured_agent_call[0]["user_input"] == "second try"


# ── Image attachment ───────────────────────────────────────────────────


def test_ws_image_b64_written_as_png_to_uploads(client, captured_agent_call):
    """image_b64 decodes to disk as /uploads/{id}.png and the path is passed to the agent."""
    import server

    # Tiny valid PNG header bytes; agent side doesn't parse it — we just check
    # the file contents match what we sent.
    raw = bytes.fromhex("89504e470d0a1a0a0000000d49484452")
    payload = {
        "text": "what's in this image?",
        "image_b64": base64.b64encode(raw).decode("ascii"),
    }
    with client.websocket_connect("/ws") as ws:
        ws.send_json(payload)
        _recv_reply(ws)

    call = captured_agent_call[0]
    assert call["image_b64"] is not None, "image_b64 should be forwarded to agent"
    assert call["image_path"] is not None
    assert call["image_path"].startswith("/uploads/")
    assert call["image_path"].endswith(".png")

    # Confirm the file actually landed on disk
    fname = call["image_path"].rsplit("/", 1)[-1]
    disk_path = server.UPLOADS_DIR / fname
    assert disk_path.exists()
    assert disk_path.read_bytes() == raw


def test_ws_document_and_image_can_coexist(client, captured_agent_call):
    """A single turn carrying both image_b64 AND document → both arrive intact."""
    raw_img = bytes.fromhex("89504e470d0a1a0a")
    raw_doc = b"readme body"
    payload = {
        "text": "look at these",
        "image_b64": base64.b64encode(raw_img).decode("ascii"),
        "document": {
            "filename": "readme.txt",
            "file_b64": base64.b64encode(raw_doc).decode("ascii"),
        },
    }
    with client.websocket_connect("/ws") as ws:
        ws.send_json(payload)
        _recv_reply(ws)

    call = captured_agent_call[0]
    assert call["image_path"] and call["image_path"].startswith("/uploads/")
    assert call["file_meta"] and call["file_meta"]["name"] == "readme.txt"
    assert "[File attached: readme.txt" in call["user_input"]
    assert "look at these" in call["user_input"]
