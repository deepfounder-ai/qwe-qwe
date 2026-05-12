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


def test_scheduler_skips_fire_when_budget_exceeded(qwe_temp_data_dir, mock_llm):
    import db
    import scheduler
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled, "
        " budget_usd_cap, budget_period_sec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test", "do something", "0 9 * * *", time.time() + 3600, 1, 0.50, 86400),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Pre-populate $0.60 of spend — over the $0.50 cap
    for _ in range(2):
        rid = db.insert_agent_run(thread_id="t1", cron_id=cron_id, source="routine",
                                   started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                               status="ok", input_tokens=100, output_tokens=50,
                               cost_usd=0.30)

    scheduler._execute_routine(
        task_desc="do something", routine_name="test", cron_id=cron_id,
        thread_id="t1", scheduled_at=time.time(),
    )

    # Most recent agent_runs row should be a skipped row with budget_exceeded
    row = db._get_conn().execute(
        "SELECT status, error FROM agent_runs WHERE cron_id=? "
        "ORDER BY id DESC LIMIT 1",
        (cron_id,)
    ).fetchone()
    assert row[0] == "skipped"
    assert "budget" in (row[1] or "").lower()


def test_scheduler_fires_normally_when_under_budget(qwe_temp_data_dir, mock_llm):
    import db
    import scheduler
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled, "
        " budget_usd_cap, budget_period_sec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test", "do something", "0 9 * * *", time.time() + 3600, 1, 10.00, 86400),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # No spend yet — fire should proceed
    scheduler._execute_routine(
        task_desc="do something", routine_name="test", cron_id=cron_id,
        thread_id="t1", scheduled_at=time.time(),
    )

    row = db._get_conn().execute(
        "SELECT status FROM agent_runs WHERE cron_id=? ORDER BY id DESC LIMIT 1",
        (cron_id,)
    ).fetchone()
    # Should NOT be skipped — actual run happens (status='ok' or 'err' via mock)
    assert row[0] != "skipped"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

def _client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def test_get_budget_endpoint_unset(qwe_temp_data_dir):
    import db
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test", "x", "0 9 * * *", time.time() + 3600, 1),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    client = _client()
    r = client.get(f"/api/routines/{cron_id}/budget")
    j = r.json()
    assert j["cap"] is None


def test_set_budget_endpoint_happy(qwe_temp_data_dir):
    import db
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test", "x", "0 9 * * *", time.time() + 3600, 1),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    client = _client()
    r = client.post(f"/api/routines/{cron_id}/budget",
                    json={"cap": 1.50, "period_sec": 3600})
    assert r.status_code == 200

    # Verify it stuck
    r2 = client.get(f"/api/routines/{cron_id}/budget")
    j = r2.json()
    assert j["cap"] == 1.50
    assert j["period_sec"] == 3600


def test_set_budget_endpoint_clear(qwe_temp_data_dir):
    import db
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled, budget_usd_cap) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("test", "x", "0 9 * * *", time.time() + 3600, 1, 5.00),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    client = _client()
    r = client.post(f"/api/routines/{cron_id}/budget",
                    json={"cap": None, "period_sec": 86400})
    assert r.status_code == 200

    r2 = client.get(f"/api/routines/{cron_id}/budget")
    assert r2.json()["cap"] is None


def test_set_budget_endpoint_rejects_negative(qwe_temp_data_dir):
    import db
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test", "x", "0 9 * * *", time.time() + 3600, 1),
    )
    conn.commit()
    cron_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    client = _client()
    r = client.post(f"/api/routines/{cron_id}/budget",
                    json={"cap": -1.0, "period_sec": 86400})
    assert r.status_code == 400
