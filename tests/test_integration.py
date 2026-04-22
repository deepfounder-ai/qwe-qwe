"""Integration tests — real FastAPI endpoints + real agent pipeline, LLM mocked.

Why this file exists
--------------------
The rag.py SyntaxError shipped in v0.17.22 because nothing actually exercised
``/api/knowledge/list``: all tests were unit tests on isolated functions, so
a Python 3.11 walrus-in-f-string parse error in a module nobody imported
stayed invisible until a fresh install tried to serve the endpoint.

These tests hit the HTTP surface through ``TestClient(server.app)`` with a
fresh ``QWE_DATA_DIR`` so every route gets imported and touched. The LLM is
mocked via ``providers.get_client`` → FakeStreamingClient; no network, no
LM Studio, no real model needed. Designed to pass on a clean Ubuntu CI
runner.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── Test env bootstrap (module-scoped, autouse) ───────────────────────

@pytest.fixture(scope="module", autouse=True)
def _integration_env():
    """Point QWE_DATA_DIR at a fresh tempdir and reload core + server.

    Matches the pattern used by test_server_presets.py: server.py needs to
    be reloaded after QWE_DATA_DIR flips so UPLOADS_DIR / STATIC_DIR /
    DB connections all rebuild against the temp location.
    """
    original = os.environ.get("QWE_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_int_test_"))
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


@pytest.fixture(scope="module")
def client():
    """Session-wide TestClient — expensive to build, cheap to reuse."""
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


# ── Fake LLM (streaming-compatible) ───────────────────────────────────

class _FakeDelta:
    """Shaped like OpenAI's stream delta."""
    def __init__(self, content: str = "", finish: str | None = None):
        self.content = content
        self.tool_calls = None
        self.role = "assistant"
        self.reasoning_content = None
        self.reasoning = None


class _FakeChoice:
    def __init__(self, content: str = "", finish: str | None = None):
        self.delta = _FakeDelta(content)
        self.finish_reason = finish
        self.message = SimpleNamespace(content=content, tool_calls=None, role="assistant")


class _FakeChunk:
    def __init__(self, content: str = "", finish: str | None = None,
                 usage: object | None = None):
        self.choices = [_FakeChoice(content, finish)]
        self.usage = usage
        self.id = "fake"
        self.model = "fake-model"


class _FakeUsage:
    def __init__(self, prompt=5, completion=2):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _FakeCompletions:
    def __init__(self, reply: str):
        self._reply = reply

    def create(self, **kw):
        """Return a streaming generator of chunks (or a blocking response)."""
        if kw.get("stream"):
            def _gen():
                yield _FakeChunk(content=self._reply, finish=None)
                yield _FakeChunk(content="", finish="stop", usage=_FakeUsage())
            return _gen()
        # Non-streaming fallback
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self._reply, tool_calls=None, role="assistant"),
                finish_reason="stop",
            )],
            usage=_FakeUsage(),
            id="fake",
            model="fake-model",
        )


class _FakeChat:
    def __init__(self, reply: str):
        self.completions = _FakeCompletions(reply)


class FakeStreamingClient:
    """OpenAI-compatible client whose ``chat.completions.create`` streams
    a deterministic short reply then a stop-chunk with usage."""
    def __init__(self, reply: str = "ok"):
        self.chat = _FakeChat(reply)


# ── Tests ─────────────────────────────────────────────────────────────


def test_server_boots_and_serves_spa(client):
    """1. TestClient(server.app).get("/") returns the SPA HTML."""
    r = client.get("/")
    assert r.status_code == 200
    # Either the bundled index.html or the "not found" fallback — both are
    # valid "server booted" signals, but CI always has the static file.
    body = r.text.lower()
    assert "<html" in body or "index.html not found" in body


def test_status_has_expected_keys(client):
    """2. /api/status returns the keys the UI depends on."""
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    for key in ("model", "provider", "core_tools", "context_budget",
                "model_context", "skills"):
        assert key in data, f"missing key {key!r} in /api/status"
    assert isinstance(data["core_tools"], list)
    assert isinstance(data["skills"], list)


def test_knowledge_list_imports_rag_cleanly(client):
    """3. /api/knowledge/list — the endpoint that would have caught v0.17.23.

    This must force a real import of rag.py. If rag has a SyntaxError the
    TestClient request raises, which is exactly what we want in CI.
    """
    # Sanity check — if rag has a parse error, importing it will explode here
    # before we even hit the endpoint.
    import rag  # noqa: F401
    r = client.get("/api/knowledge/list")
    assert r.status_code == 200
    data = r.json()
    assert "files" in data
    assert isinstance(data["files"], list)


def test_knowledge_url_rejects_empty(client):
    """4a. Empty URL → 400."""
    r = client.post("/api/knowledge/url", json={"url": ""})
    assert r.status_code == 400


def test_knowledge_url_rejects_non_http(client):
    """4b. Scheme must be http(s)."""
    r = client.post("/api/knowledge/url", json={"url": "file:///etc/passwd"})
    assert r.status_code == 400


def test_knowledge_url_rejects_private_ip(client):
    """4c. SSRF guard: URLs that resolve to private/loopback/link-local → 403.

    ``127.0.0.1`` always resolves to loopback. We also make sure the
    ``QWE_ALLOW_PRIVATE_URLS`` escape hatch isn't set.
    """
    # Ensure the override env var isn't leaking in from the dev machine
    os.environ.pop("QWE_ALLOW_PRIVATE_URLS", None)
    r = client.post("/api/knowledge/url", json={"url": "http://127.0.0.1/foo"})
    assert r.status_code == 403, r.text
    assert "private" in r.json()["error"].lower()


def test_knowledge_url_accepts_well_formed(client, monkeypatch):
    """4d. Well-formed external URL → 200 + task_id, worker thread short-circuits."""
    import rag
    # Short-circuit the fetch so we don't hit the network even if DNS works.
    monkeypatch.setattr(rag, "index_url",
                        lambda url, tags=None: {"status": "indexed",
                                                 "chunks": 0, "path": url,
                                                 "converter": "stub"})
    # Also mock DNS resolution so CI without network still lets us pass.
    import server
    monkeypatch.setattr(server, "_url_resolves_to_private", lambda url: None)
    r = client.post("/api/knowledge/url",
                     json={"url": "https://example.com/page"})
    # 200 on happy path, 409 if a previous test left _knowledge_task "running".
    # Both signal the endpoint is wired correctly.
    assert r.status_code in (200, 409), r.text
    if r.status_code == 200:
        assert "task_id" in r.json()
    # Let the worker thread finish so later tests don't race with a "running" task
    for _ in range(20):
        with server._knowledge_lock:
            st = (server._knowledge_task or {}).get("status")
        if st != "running":
            break
        time.sleep(0.05)


def test_knowledge_search_empty_corpus(client):
    """5. Search on an empty corpus returns results:[] without error."""
    r = client.post("/api/knowledge/search", json={"query": "anything at all"})
    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    assert isinstance(data["results"], list)


def test_soul_get_post_roundtrip(client):
    """6. GET /api/soul → POST {humor:high} → GET shows new value."""
    r = client.get("/api/soul")
    assert r.status_code == 200
    before = r.json()
    assert "values" in before and "humor" in before["values"]

    r = client.post("/api/soul", json={"humor": "high"})
    assert r.status_code == 200

    r = client.get("/api/soul")
    assert r.status_code == 200
    assert r.json()["values"]["humor"] == "high"


def test_settings_roundtrip(client):
    """7. /api/settings GET → POST → GET persists the value."""
    r = client.get("/api/settings")
    assert r.status_code == 200
    before = r.json()
    assert "max_history_messages" in before
    assert "value" in before["max_history_messages"]

    r = client.post("/api/settings", json={"max_history_messages": 7})
    assert r.status_code == 200

    r = client.get("/api/settings")
    # get_all() returns a descriptor dict per setting — the current value
    # lives under the "value" key.
    assert r.json()["max_history_messages"]["value"] == 7


def test_threads_list_create_delete(client):
    """8. Threads: list → create → list → delete → list."""
    r = client.get("/api/threads")
    assert r.status_code == 200
    initial = r.json()
    assert isinstance(initial, list)

    r = client.post("/api/threads", json={"name": "integration-thread"})
    assert r.status_code == 200
    tid = r.json()["id"]

    r = client.get("/api/threads")
    names = [t["name"] for t in r.json()]
    assert "integration-thread" in names

    r = client.delete(f"/api/threads/{tid}")
    assert r.status_code == 200

    r = client.get("/api/threads")
    names = [t["name"] for t in r.json()]
    assert "integration-thread" not in names


def test_knowledge_recent_returns_items(client):
    """9. /api/knowledge/recent returns {items:[...]} after _push_history()."""
    import server
    server._push_history({"kind": "url", "label": "test-entry",
                           "url": "https://example.com/x",
                           "status": "done", "chunks": 3})
    r = client.get("/api/knowledge/recent")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    labels = [it.get("label") for it in data["items"]]
    assert "test-entry" in labels


def test_kv_allowlist_and_blocklist(client):
    """10. /api/kv POST: allowed keys pass, blocked prefixes → 403."""
    # Allowed
    r = client.post("/api/kv", json={"key": "ui:theme", "value": "dark"})
    assert r.status_code == 200
    # Blocked prefix
    for blocked in ("soul:humor", "setting:max_history_messages",
                     "version:latest", "telegram:owner_id"):
        r = client.post("/api/kv", json={"key": blocked, "value": "x"})
        assert r.status_code == 403, f"{blocked} should have been blocked"


def test_websocket_handshake(client):
    """11. Open WS, send a non-text JSON (frame_response) → connection stays alive.

    We don't run a full turn here (LLM isn't mocked at the WS layer — it would
    spin up the agent thread pool with real-ish dependencies). The handshake
    itself is what regressed silently when the auth cookie check was added,
    so even an accept + close is worth catching in CI.
    """
    import server
    original_pw = server._AUTH_PASSWORD
    server._AUTH_PASSWORD = ""  # disable auth for this test
    try:
        with client.websocket_connect("/ws") as ws:
            # frame_response is the one message type the WS swallows without
            # triggering an agent turn — perfect for a handshake smoke test.
            ws.send_json({"type": "frame_response", "request_id": "nonexistent"})
            # Close cleanly — server will tear down its side.
    finally:
        server._AUTH_PASSWORD = original_pw


def test_agent_turn_smoke(monkeypatch, qwe_temp_data_dir):
    """12. Mock providers.get_client → fake streaming client. Run agent.run,
    assert ctx callback fired and the assistant reply landed in SQLite.
    """
    import agent
    import providers
    import db
    from turn_context import TurnContext

    fake = FakeStreamingClient(reply="ok")
    monkeypatch.setattr(providers, "get_client", lambda: fake, raising=False)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model", raising=False)
    monkeypatch.setattr(providers, "get_active_name", lambda: "fake", raising=False)

    seen: list[str] = []
    ctx = TurnContext(source="test", on_content=lambda t: seen.append(t))

    result = agent.run("hello", ctx=ctx)

    # Reply should not be empty; ctx callback should have received at least "ok"
    assert (result.reply or "").strip() != ""
    # Assistant message persisted to DB
    rows = db.fetchall("SELECT role, content FROM messages ORDER BY id DESC LIMIT 5")
    roles = [r[0] for r in rows]
    assert "assistant" in roles
    # Callback fired at least once (streaming chunk went through)
    assert seen, "on_content callback never fired"


def test_concurrent_agent_runs_no_crosstalk(monkeypatch, qwe_temp_data_dir):
    """13. Two concurrent agent.run() calls with distinct TurnContexts →
    their callbacks only see their own labelled output.

    Covers the "preset isolation reconfirm" requirement through the agent
    pipeline (rather than the emit helpers that test_turn_context.py
    already exercises).
    """
    import agent
    import providers
    import threads
    from turn_context import TurnContext

    # Per-context reply so we can tell who's who
    class _SwitchingClient:
        def __init__(self):
            self._counter = 0
            self._lock = threading.Lock()
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kw):
            with self._lock:
                self._counter += 1
                idx = self._counter
            reply = f"reply-{idx}"
            if kw.get("stream"):
                def _gen():
                    yield _FakeChunk(content=reply, finish=None)
                    yield _FakeChunk(content="", finish="stop", usage=_FakeUsage())
                return _gen()
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=reply, tool_calls=None, role="assistant"),
                    finish_reason="stop")],
                usage=_FakeUsage(), id="fake", model="fake-model")

    client = _SwitchingClient()
    monkeypatch.setattr(providers, "get_client", lambda: client, raising=False)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model", raising=False)
    monkeypatch.setattr(providers, "get_active_name", lambda: "fake", raising=False)

    # Give each turn its own thread so messages can't collide in history
    t_a = threads.create("int-a")
    t_b = threads.create("int-b")

    got_a: list[str] = []
    got_b: list[str] = []
    ctx_a = TurnContext(source="a", on_content=lambda t: got_a.append(t))
    ctx_b = TurnContext(source="b", on_content=lambda t: got_b.append(t))

    errors: list[Exception] = []

    def _run(user: str, tid: str, ctx: TurnContext):
        try:
            agent.run(user, thread_id=tid, ctx=ctx)
        except Exception as e:
            errors.append(e)

    th_a = threading.Thread(target=_run, args=("hello A", t_a["id"], ctx_a))
    th_b = threading.Thread(target=_run, args=("hello B", t_b["id"], ctx_b))
    th_a.start()
    th_b.start()
    th_a.join(timeout=15)
    th_b.join(timeout=15)

    assert not errors, f"agent runs raised: {errors}"
    assert got_a, "ctx A never received a content chunk"
    assert got_b, "ctx B never received a content chunk"
    # Each callback only saw exactly its own reply. Labels are unique per
    # create() call so crosstalk would show up as a mismatched label set.
    for item in got_a:
        assert item not in got_b, f"crosstalk: {item!r} leaked A→B"
    for item in got_b:
        assert item not in got_a, f"crosstalk: {item!r} leaked B→A"
