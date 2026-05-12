"""Unit tests for analytics-related HTTP endpoints (cost tracking)."""
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import server
    return TestClient(server.app)


def test_threads_endpoint_includes_token_fields(client, qwe_temp_data_dir):
    import db
    import threads
    threads.create("Test Thread T1")
    # Find the created thread id
    all_t = threads.list_all()
    tid = all_t[0]["id"]
    rid = db.insert_agent_run(thread_id=tid, source="web",
                              started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                          status="ok", input_tokens=100, output_tokens=50,
                          cost_usd=0.001)
    r = client.get("/api/threads")
    assert r.status_code == 200
    sess = [s for s in r.json() if s.get("thread_id") == tid or s.get("id") == tid]
    assert sess and sess[0]["input_tokens"] == 100
    assert sess[0]["cost_usd"] == 0.001
    assert sess[0]["run_count"] == 1


def test_thread_runs_endpoint(client, qwe_temp_data_dir):
    import db
    import time
    for tok in (100, 200, 300):
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok,
                              cost_usd=tok * 1e-6)
    r = client.get("/api/threads/t1/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 3
    assert runs[0]["input_tokens"] == 300  # newest first
    assert runs[2]["input_tokens"] == 100


def test_thread_runs_empty_thread_returns_empty_list(client, qwe_temp_data_dir):
    r = client.get("/api/threads/never-existed/runs")
    assert r.status_code == 200
    assert r.json() == []


def test_analytics_period_aggregates(client, qwe_temp_data_dir):
    import db
    import time
    for src, tok in [("web", 100), ("routine", 200), ("synthesis", 50)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok)
    r = client.get("/api/analytics/period?days=30")
    j = r.json()
    assert j["total_input_tokens"] == 350
    assert "by_source" in j and "synthesis" in j["by_source"]


def test_analytics_period_source_filter(client, qwe_temp_data_dir):
    import db
    import time
    for src, tok in [("web", 100), ("routine", 200)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok)
    r = client.get("/api/analytics/period?days=30&source=routine")
    assert r.json()["total_input_tokens"] == 200


def test_pricing_status(client, qwe_temp_data_dir):
    r = client.get("/api/pricing/status")
    j = r.json()
    assert "model_count" in j
    assert "source_url" in j
    assert "auto_update" in j


def test_pricing_refresh_success(client, qwe_temp_data_dir, monkeypatch):
    import pricing
    monkeypatch.setattr(pricing, "refresh_pricing", lambda force=False: True)
    monkeypatch.setattr(pricing, "all_known_models", lambda: ["x", "y"])
    r = client.post("/api/pricing/refresh")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_pricing_refresh_failure(client, qwe_temp_data_dir, monkeypatch):
    import pricing
    monkeypatch.setattr(pricing, "refresh_pricing", lambda force=False: False)
    r = client.post("/api/pricing/refresh")
    assert r.status_code == 502
    assert r.json()["ok"] is False


def test_routine_runs_endpoint(client, qwe_temp_data_dir):
    import db
    import time
    rid = db.insert_agent_run(thread_id="t1", cron_id=42, source="routine",
                              started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                          status="ok", input_tokens=300, output_tokens=80,
                          cost_usd=0.005)
    r = client.get("/api/routines/42/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    # field names may be wrapped/formatted; just check the cost made it through
    flat = str(runs).lower()
    assert "300" in flat and "0.005" in flat
