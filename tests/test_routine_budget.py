"""Tests for per-routine budget caps (v0.21.0)."""
import time
import pytest


def test_period_spend_empty(qwe_temp_data_dir):
    import db
    assert db.get_routine_period_spend(99, 86400) == 0.0


def test_period_spend_sums_within_window(qwe_temp_data_dir):
    import db
    for cost in (0.10, 0.25, 0.05):
        rid = db.insert_agent_run(thread_id="t1", cron_id=42, source="routine",
                                   started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                               status="ok", input_tokens=100, output_tokens=50,
                               cost_usd=cost)
    spend = db.get_routine_period_spend(42, 86400)
    assert abs(spend - 0.40) < 1e-9


def test_period_spend_excludes_outside_window(qwe_temp_data_dir):
    import db
    long_ago = time.time() - 86400 - 100
    rid = db.insert_agent_run(thread_id="t1", cron_id=42, source="routine",
                               started_at=long_ago, status="running")
    db.finalize_agent_run(rid, finished_at=long_ago, duration_ms=10,
                           status="ok", input_tokens=100, output_tokens=50,
                           cost_usd=10.0)
    assert db.get_routine_period_spend(42, 86400) == 0.0


def test_period_spend_null_cost_treated_as_zero(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", cron_id=42, source="routine",
                               started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                           status="ok", input_tokens=100, output_tokens=50,
                           cost_usd=None)  # unknown pricing
    assert db.get_routine_period_spend(42, 86400) == 0.0


def test_get_routine_budget_unset(qwe_temp_data_dir):
    import db
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test", "do something", "0 9 * * *", time.time() + 3600, 1),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    assert db.get_routine_budget(cron_id) is None


def test_get_routine_budget_set(qwe_temp_data_dir):
    import db
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled, "
        " budget_usd_cap, budget_period_sec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test", "do something", "0 9 * * *", time.time() + 3600, 1, 1.50, 3600),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    b = db.get_routine_budget(cron_id)
    assert b == {"cap": 1.50, "period_sec": 3600}
