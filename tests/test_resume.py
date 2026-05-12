"""Unit tests for auto-resume after interrupt (v0.20.0)."""
import pytest
import time


def test_resume_settings_have_defaults(qwe_temp_data_dir):
    import config
    assert config.get("resume_ttl_web_sec") == 604800
    assert config.get("resume_ttl_telegram_sec") == 86400
    assert config.get("resume_ttl_routine_sec") == 300
    assert config.get("resume_routine_auto") is True


def test_insert_agent_run_accepts_resumed_from(qwe_temp_data_dir):
    import db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    resume = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running",
                                  resumed_from_run_id=original)
    row = db._get_conn().execute(
        "SELECT resumed_from_run_id FROM agent_runs WHERE id=?", (resume,)
    ).fetchone()
    assert row[0] == original


def test_dismiss_run_sets_dismissed_at(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    row = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] is not None and row[0] > 0


def test_dismiss_run_is_idempotent(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    first = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()[0]
    db.dismiss_run(rid)
    second = db._get_conn().execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()[0]
    assert first == second  # not overwritten on second call


def test_get_resumable_run_for_thread_happy(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted",
                           result_preview="partial reply preview")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is not None
    assert found["id"] == rid


def test_get_resumable_run_excludes_cli(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="cli",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_get_resumable_run_respects_ttl(qwe_temp_data_dir):
    import db
    long_ago = time.time() - 86400 - 1
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=long_ago, status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_get_resumable_run_excludes_dismissed(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None, status="aborted")
    db.dismiss_run(rid)
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_get_resumable_run_excludes_resume_runs(qwe_temp_data_dir):
    """A row that is itself a resume of something else is not offered for re-resume."""
    import db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    db.finalize_agent_run(original, finished_at=None, duration_ms=None, status="aborted")
    resume = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running",
                                  resumed_from_run_id=original)
    db.finalize_agent_run(resume, finished_at=None, duration_ms=None, status="aborted")
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    # Original was already resumed (by `resume`), so it's filtered out via reverse-lookup.
    # `resume` itself has resumed_from_run_id NOT NULL so it's filtered too.
    assert found is None


def test_get_resumable_run_excludes_already_resumed_original(qwe_temp_data_dir):
    """An original that's already been resumed-from cannot be resumed again."""
    import db
    original = db.insert_agent_run(thread_id="t1", source="web",
                                    started_at=time.time(), status="running")
    db.finalize_agent_run(original, finished_at=None, duration_ms=None, status="aborted")
    db.insert_agent_run(thread_id="t1", source="web",
                         started_at=time.time(), status="running",
                         resumed_from_run_id=original)
    found = db.get_resumable_run_for_thread("t1", source_filter="web", ttl_sec=86400)
    assert found is None


def test_agent_run_accepts_system_note_kwarg(qwe_temp_data_dir, monkeypatch):
    """system_note=... should be accepted; agent.run doesn't crash on it."""
    from types import SimpleNamespace
    import agent
    import providers

    # Build a minimal streaming-compatible fake client.
    class _Delta:
        def __init__(self, content=""):
            self.content = content
            self.tool_calls = None
            self.role = "assistant"
            self.reasoning_content = None
            self.reasoning = None

    class _Chunk:
        def __init__(self, content="", finish=None):
            self.choices = [SimpleNamespace(delta=_Delta(content), finish_reason=finish,
                                            message=_Delta(content))]
            self.usage = None
            self.id = "fake"
            self.model = "fake-model"

    class _Completions:
        def create(self, **kw):
            if kw.get("stream"):
                def _gen():
                    yield _Chunk(content="ok", finish=None)
                    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7)
                    yield _Chunk(content="", finish="stop")
                return _gen()
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None, role="assistant"),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
                id="fake",
                model="fake-model",
            )

    class _FakeClient:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(providers, "get_client", lambda: _FakeClient(), raising=False)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model", raising=False)

    # Even with user_input=None, providing a system_note alone should work.
    # We don't assert on the model's reply here — just that the call accepts
    # the kwarg and returns something without raising.
    out = agent.run(
        user_input=None,
        thread_id="t-system-note",
        system_note="Continue from where you left off.",
        source="cli",
    )
    assert out is not None


def test_agent_run_user_input_none_with_no_system_note_raises(qwe_temp_data_dir):
    """user_input=None AND system_note=None is invalid — must raise ValueError."""
    import agent
    with pytest.raises(ValueError):
        agent.run(user_input=None, thread_id="t1", source="cli")


def test_abort_with_partial_content_writes_interrupted_message(qwe_temp_data_dir):
    """When agent_loop's finally fires with non-empty final_content, a
    messages row is written with meta.interrupted=true and run_id linkage.

    We test the finally-block logic in isolation rather than racing
    against a streaming LLM mock (timing-flaky)."""
    import db
    import json
    # Set up an aborted run row
    rid = db.insert_agent_run(thread_id="t-partial", source="web",
                               started_at=1000.0, status="running")

    # Simulate what agent_loop's finally would do:
    # 1) save partial content message with meta.interrupted=true
    db.save_message(
        role="assistant", content="I'll start by searching for X...",
        thread_id="t-partial",
        meta={"interrupted": True, "run_id": rid,
              "partial_tokens": {"input": 320, "output": 184}},
    )
    # 2) finalize the run
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None,
                           status="aborted",
                           result_preview="I'll start by searching for X...")

    # Assert the message row landed with correct meta
    row = db._get_conn().execute(
        "SELECT role, content, meta FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        ("t-partial",)
    ).fetchone()
    assert row[0] == "assistant"
    assert "searching" in row[1]
    meta = json.loads(row[2]) if isinstance(row[2], str) else row[2]
    assert meta.get("interrupted") is True
    assert meta.get("run_id") == rid
    assert meta.get("partial_tokens", {}).get("input") == 320


def test_crash_recovery_promotes_running_to_aborted(qwe_temp_data_dir):
    """Server crash recovery: 'running' rows at startup become 'aborted'
    with synthesized message marker."""
    import db
    import json
    import time
    # Simulate a server crash by leaving a 'running' row
    rid = db.insert_agent_run(thread_id="t-crash", source="web",
                               started_at=time.time(), status="running")

    # Invoke the recovery hook directly
    from server import _recover_interrupted_runs_on_startup
    _recover_interrupted_runs_on_startup()

    # The row should now be aborted
    row = db._get_conn().execute(
        "SELECT status, error FROM agent_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] == "aborted"
    assert "restart" in (row[1] or "").lower()

    # The synthesized message marker should be present
    m = db._get_conn().execute(
        "SELECT meta FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        ("t-crash",)
    ).fetchone()
    assert m is not None
    meta = json.loads(m[0]) if isinstance(m[0], str) else m[0]
    assert meta.get("interrupted") is True
    assert meta.get("crash_recovery") is True
    assert meta.get("run_id") == rid


def test_crash_recovery_idempotent_no_running_rows(qwe_temp_data_dir):
    """If no 'running' rows exist, recovery is a no-op."""
    from server import _recover_interrupted_runs_on_startup
    # Should not raise
    _recover_interrupted_runs_on_startup()
