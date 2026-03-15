"""Tests for config.py — env variable overrides."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_default_llm_url():
    # Reimport with clean state
    import importlib
    if "config" in sys.modules:
        del sys.modules["config"]
    # Ensure no env override
    old = os.environ.pop("QWE_LLM_URL", None)
    try:
        import config
        importlib.reload(config)
        assert config.LLM_BASE_URL == "http://localhost:1234/v1"
    finally:
        if old:
            os.environ["QWE_LLM_URL"] = old


def test_env_override_llm_url():
    import importlib
    if "config" in sys.modules:
        del sys.modules["config"]
    os.environ["QWE_LLM_URL"] = "http://myserver:5555/v1"
    try:
        import config
        importlib.reload(config)
        assert config.LLM_BASE_URL == "http://myserver:5555/v1"
    finally:
        del os.environ["QWE_LLM_URL"]


def test_embed_defaults_to_llm():
    import importlib
    if "config" in sys.modules:
        del sys.modules["config"]
    os.environ.pop("QWE_EMBED_URL", None)
    os.environ.pop("QWE_LLM_URL", None)
    try:
        import config
        importlib.reload(config)
        assert config.EMBED_BASE_URL == config.LLM_BASE_URL
    finally:
        pass


def test_default_model():
    import importlib
    if "config" in sys.modules:
        del sys.modules["config"]
    os.environ.pop("QWE_LLM_MODEL", None)
    try:
        import config
        importlib.reload(config)
        assert config.LLM_MODEL == "qwen/qwen3.5-9b"
    finally:
        pass
