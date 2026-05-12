"""Unit tests for db.py helpers added in v0.19.0 cost-tracking work."""
import time
import pytest


def test_insert_agent_run_returns_id(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(
        thread_id="t1", source="web", started_at=time.time(),
        status="running", model="gpt-4o-mini", provider="openai",
    )
    assert isinstance(rid, int) and rid > 0


def test_insert_agent_run_row_visible(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    row = db._get_conn().execute(
        "SELECT thread_id, source, started_at, status FROM agent_runs WHERE id=?",
        (rid,)).fetchone()
    assert row == ("t1", "web", 1000.0, "running")


def test_finalize_agent_run_updates_metrics(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    db.finalize_agent_run(rid, finished_at=1001.5, duration_ms=1500,
                          status="ok", result_preview="reply",
                          input_tokens=100, output_tokens=50, cost_usd=0.001)
    row = db._get_conn().execute(
        "SELECT finished_at, duration_ms, status, input_tokens, output_tokens, cost_usd "
        "FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row == (1001.5, 1500, "ok", 100, 50, 0.001)


def test_finalize_handles_null_finished_at(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None,
                          status="aborted", input_tokens=80, output_tokens=20)
    row = db._get_conn().execute(
        "SELECT finished_at, duration_ms, status FROM agent_runs WHERE id=?",
        (rid,)).fetchone()
    assert row == (None, None, "aborted")


def test_insert_skipped_run_writes_zero_tokens(qwe_temp_data_dir):
    import db
    rid = db.insert_skipped_run(cron_id=5, thread_id="t1",
                                scheduled_at=1000.0, reason="missed")
    row = db._get_conn().execute(
        "SELECT status, started_at, input_tokens, output_tokens "
        "FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row == ("missed", 1000.0, 0, 0)


def test_get_thread_totals_sums_correctly(qwe_temp_data_dir):
    import db
    for (i, o, c) in [(100, 50, 0.01), (200, 80, 0.02), (50, 30, None)]:
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=100,
                              status="ok", input_tokens=i, output_tokens=o,
                              cost_usd=c)
    totals = db.get_thread_totals("t1")
    assert totals["input_tokens"] == 350
    assert totals["output_tokens"] == 160
    # COALESCE on cost_usd treats NULL as 0 in the sum
    assert abs(totals["cost_usd"] - 0.03) < 1e-9
    assert totals["run_count"] == 3


def test_get_thread_totals_empty(qwe_temp_data_dir):
    import db
    totals = db.get_thread_totals("ghost")
    assert totals == {"input_tokens": 0, "output_tokens": 0,
                      "cost_usd": 0.0, "run_count": 0}


def test_get_runs_for_thread_ordering_and_limit(qwe_temp_data_dir):
    import db
    ids = []
    for t in [1000.0, 2000.0, 3000.0]:
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=t, status="running")
        ids.append(rid)
    rows = db.get_runs_for_thread("t1", limit=2)
    assert [r["id"] for r in rows] == [ids[2], ids[1]]


def test_get_period_totals_filters_by_source(qwe_temp_data_dir):
    import db
    for src, tok in [("web", 100), ("routine", 200), ("synthesis", 50)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=1500.0, status="running")
        db.finalize_agent_run(rid, finished_at=1501.0, duration_ms=1000,
                              status="ok", input_tokens=tok, output_tokens=0)
    t_routine = db.get_period_totals(1000.0, 2000.0, source="routine")
    assert t_routine["total_input_tokens"] == 200
    t_all = db.get_period_totals(1000.0, 2000.0)
    assert t_all["total_input_tokens"] == 350
    assert "by_source" in t_all
    assert t_all["by_source"]["synthesis"]["input_tokens"] == 50
