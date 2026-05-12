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
