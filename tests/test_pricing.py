"""Unit tests for the pricing module (cost tracking)."""
import pytest


def test_bundled_fallback_has_gpt4o_mini():
    import pricing
    assert "gpt-4o-mini" in pricing._BUNDLED_FALLBACK
    assert pricing._BUNDLED_FALLBACK["gpt-4o-mini"]["input"] > 0


def test_local_provider_zero_cost(qwe_temp_data_dir):
    import pricing
    assert pricing.get_price("lmstudio:llama-3", "input") == 0.0
    assert pricing.get_price("ollama:qwen2.5", "output") == 0.0
    assert pricing.get_price("local:any-model", "input") == 0.0


def test_compute_cost_local_zero(qwe_temp_data_dir):
    import pricing
    assert pricing.compute_cost("ollama:llama-3", 1000, 500) == 0.0


def test_get_price_unknown_model_returns_none(qwe_temp_data_dir):
    import pricing
    assert pricing.get_price("totally-fake-model-9000", "input") is None


def test_compute_cost_unknown_returns_none(qwe_temp_data_dir):
    import pricing
    assert pricing.compute_cost("totally-fake-model-9000", 1000, 500) is None


def test_kv_override_beats_bundled(qwe_temp_data_dir):
    import json
    import pricing
    import db
    db.kv_set("pricing_override_gpt-4o-mini",
              json.dumps({"input": 9.99e-7, "output": 1.23e-6}))
    # Force reload to make sure no memory cache shadows
    pricing._pricing_cache = None
    assert pricing.get_price("gpt-4o-mini", "input") == 9.99e-7
    assert pricing.get_price("gpt-4o-mini", "output") == 1.23e-6


def test_kv_override_invalid_json_warns_and_continues(qwe_temp_data_dir, caplog):
    import pricing
    import db
    db.kv_set("pricing_override_gpt-4o-mini", "{not json")
    pricing._pricing_cache = None
    with caplog.at_level("WARNING"):
        v = pricing.get_price("gpt-4o-mini", "input")
    # falls through to bundled
    assert v == pricing._BUNDLED_FALLBACK["gpt-4o-mini"]["input"]
    assert any("invalid pricing_override" in r.message for r in caplog.records)


def test_disk_cache_loaded_on_first_call(qwe_temp_data_dir):
    import json
    import pricing
    pricing._pricing_cache = None  # force reload
    pricing._cache_fetched_at = None
    payload = {
        "fetched_at": 1700000000.0,
        "source_url": "test",
        "models": {"my-custom-model": {"input": 1e-6, "output": 2e-6}},
    }
    pricing._cache_path().write_text(json.dumps(payload))
    assert pricing.get_price("my-custom-model", "input") == 1e-6
    assert pricing.last_updated() == 1700000000.0


def test_corrupt_cache_file_falls_back_gracefully(qwe_temp_data_dir):
    import pricing
    pricing._pricing_cache = None
    pricing._cache_path().write_text("{ malformed ")
    # Should not raise; falls back to bundled
    assert pricing.get_price("gpt-4o-mini", "input") > 0


import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "litellm_sample.json"


def test_normalize_litellm_keeps_chat_models():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "gpt-4o-mini" in out
    assert out["gpt-4o-mini"] == {"input": 0.00000015, "output": 0.00000060}
    assert "claude-3-5-sonnet-20241022" in out


def test_normalize_litellm_skips_sample_spec():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "sample_spec" not in out


def test_normalize_litellm_skips_non_chat_modes():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "text-embedding-3-small" not in out
    assert "dall-e-3" not in out
    assert "whisper-1" not in out


def test_normalize_litellm_skips_entries_missing_prices():
    import pricing
    raw = json.loads(FIXTURE.read_text())
    out = pricing._normalize_litellm(raw)
    assert "broken-entry" not in out
