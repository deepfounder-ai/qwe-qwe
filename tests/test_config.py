"""Tests for config.py — env variable overrides.

These tests must reload ``config`` because it reads env vars at module import
time. To keep the reload from leaking a mutated ``config`` into sibling test
files, an autouse fixture restores the module after each test.
"""

import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_config():
    """Reload config after every test so later files see a pristine module."""
    yield
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])


def test_default_llm_url(monkeypatch):
    monkeypatch.delenv("QWE_LLM_URL", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    import config
    importlib.reload(config)
    assert config.LLM_BASE_URL == "http://localhost:1234/v1"


def test_env_override_llm_url(monkeypatch):
    monkeypatch.setenv("QWE_LLM_URL", "http://myserver:5555/v1")
    monkeypatch.delitem(sys.modules, "config", raising=False)
    import config
    importlib.reload(config)
    assert config.LLM_BASE_URL == "http://myserver:5555/v1"


def test_embed_handled_by_fastembed(monkeypatch):
    """Embeddings are now handled by FastEmbed — no EMBED_* config needed."""
    monkeypatch.delitem(sys.modules, "config", raising=False)
    import config
    importlib.reload(config)
    assert not hasattr(config, "EMBED_BASE_URL")
    assert not hasattr(config, "EMBED_MODEL")


def test_default_model(monkeypatch):
    monkeypatch.delenv("QWE_LLM_MODEL", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    import config
    importlib.reload(config)
    assert config.LLM_MODEL == "qwen/qwen3.5-9b"
