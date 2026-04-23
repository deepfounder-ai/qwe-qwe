"""Provider listing + ping cache + switch behavior.

Backfill for the v0.17.x provider-picker bug: ``list_all()`` used to ping local
providers serially with a 1s timeout each, so a single unreachable LM Studio
could stall the whole Settings → Model page for 2-3 seconds. The fix (commit
0c41b54) parallelizes the pings and caches results for 30s. These tests lock
in the expected latency + shape so future refactors don't regress it.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fresh_providers(qwe_temp_data_dir, monkeypatch):
    """Reload the providers module against a fresh temp DB and clear ping cache.

    ``qwe_temp_data_dir`` already reloads ``config`` and ``db``; we reload
    ``providers`` on top so its module-level ``_init()`` runs against the
    clean DB. The ping cache is a module global — clear it explicitly.
    """
    import importlib
    import sys

    if "providers" not in sys.modules:
        importlib.import_module("providers")
    providers = importlib.reload(sys.modules["providers"])

    providers._ping_cache.clear()
    providers._CTX_CACHE.clear()
    return providers


def _fake_urlopen_slow(delay: float):
    """Return a urlopen stub that sleeps for ``delay`` seconds then raises."""
    def _open(*_a, **_kw):
        time.sleep(delay)
        raise TimeoutError("simulated timeout")
    return _open


def _fake_urlopen_ok():
    """urlopen stub that returns a fake 200 response."""
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _open(*_a, **_kw):
        return _Resp()

    return _open


# ── list_all() shape ────────────────────────────────────────────────────────


def test_list_all_returns_expected_shape_for_every_provider(fresh_providers, monkeypatch):
    """Each entry has the keys the Web UI reads + correct local/has_key flags."""
    # Stub pings so the test doesn't depend on whether LM Studio is running.
    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen_ok(), raising=True
    )

    providers = fresh_providers
    entries = providers.list_all()

    assert entries, "list_all() returned nothing"
    required_keys = {"name", "display", "url", "has_key", "online", "local", "models", "active"}
    for e in entries:
        missing = required_keys - e.keys()
        assert not missing, f"entry {e.get('name')!r} missing keys: {missing}"

    # Local providers (lmstudio, ollama) are flagged local=True, others False.
    by_name = {e["name"]: e for e in entries}
    assert by_name["lmstudio"]["local"] is True
    assert by_name["ollama"]["local"] is True
    # Cloud providers without a saved key have has_key=False.
    assert by_name["openai"]["local"] is False
    assert by_name["openai"]["has_key"] is False
    # Exactly one provider is active (defaults to lmstudio).
    actives = [e["name"] for e in entries if e["active"]]
    assert actives == ["lmstudio"], f"expected single active=lmstudio, got {actives}"


# ── list_all() parallelism — the freshly fixed bug ──────────────────────────


def test_list_all_pings_locals_in_parallel_not_serial(fresh_providers, monkeypatch):
    """Two unreachable local providers must not take 2 × timeout — they ping in parallel.

    We simulate each ping sleeping 0.7s before failing. Serial would take
    ~1.4s total; parallel stays near ~0.7s + thread-pool overhead. Guard at
    1.2s leaves room for CI jitter while still catching a regression to
    serial pings (fault-injection confirmed: forcing synchronous execution
    lands at ~1.4s, well above the guard).
    """
    per_ping_delay = 0.7
    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen_slow(per_ping_delay), raising=True
    )

    providers = fresh_providers
    t0 = time.time()
    entries = providers.list_all()
    elapsed = time.time() - t0

    # Both local providers timed out → online=False for both
    by_name = {e["name"]: e for e in entries}
    assert by_name["lmstudio"]["online"] is False
    assert by_name["ollama"]["online"] is False

    # Parallel budget: max(sleeps) + pool overhead. Serial = 2 × 0.7s = 1.4s.
    assert elapsed < 1.2, (
        f"list_all() took {elapsed:.2f}s for 2 local pings — "
        "parallelism likely regressed to serial."
    )


# ── ping() cache ────────────────────────────────────────────────────────────


def test_ping_cache_avoids_second_network_call(fresh_providers, monkeypatch):
    """Two ``ping()`` calls within TTL → urlopen hit once."""
    calls = {"n": 0}

    def _counting_urlopen(*_a, **_kw):
        calls["n"] += 1
        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_a): return False
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _counting_urlopen, raising=True)

    providers = fresh_providers
    assert providers.ping("lmstudio") is True
    assert providers.ping("lmstudio") is True  # served from cache
    assert calls["n"] == 1, f"expected 1 network call, got {calls['n']}"


def test_ping_cache_invalidation_forces_new_call(fresh_providers, monkeypatch):
    calls = {"n": 0}

    def _counting_urlopen(*_a, **_kw):
        calls["n"] += 1
        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_a): return False
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _counting_urlopen, raising=True)

    providers = fresh_providers
    providers.ping("lmstudio")
    providers._invalidate_ping_cache("lmstudio")
    providers.ping("lmstudio")
    assert calls["n"] == 2


# ── set_model() embedding block ─────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "text-embedding-3-small",
    "nomic-embed-text-v1.5",
    "some-embedding-model",
])
def test_set_model_blocks_embedding_models(fresh_providers, bad):
    """Embedding models crash LM Studio chat completions; ``set_model`` rejects them."""
    providers = fresh_providers
    result = providers.set_model(bad)
    assert result.startswith("✗"), f"expected rejection, got {result!r}"
    assert "embedding" in result.lower()


def test_set_model_accepts_chat_model(fresh_providers):
    providers = fresh_providers
    result = providers.set_model("qwen/qwen3.5-9b")
    assert result.startswith("✓")
    assert providers.get_model() == "qwen/qwen3.5-9b"


# ── switch() guards ─────────────────────────────────────────────────────────


def test_switch_to_unknown_provider_returns_error(fresh_providers):
    providers = fresh_providers
    result = providers.switch("not-a-real-provider")
    assert result.startswith("✗")
    assert "unknown" in result.lower()


def test_switch_to_keyless_cloud_provider_returns_error(fresh_providers):
    """Cloud providers without a saved API key cannot be activated."""
    providers = fresh_providers
    # openai is a preset with empty key — must be rejected
    result = providers.switch("openai")
    assert result.startswith("✗")
    assert "key" in result.lower()


def test_switch_to_local_provider_succeeds_without_key(fresh_providers):
    providers = fresh_providers
    result = providers.switch("ollama")
    assert result.startswith("✓")
    assert providers.get_active_name() == "ollama"
