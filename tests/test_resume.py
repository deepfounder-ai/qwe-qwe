"""Unit tests for auto-resume after interrupt (v0.20.0)."""
import pytest


def test_resume_settings_have_defaults(qwe_temp_data_dir):
    import config
    assert config.get("resume_ttl_web_sec") == 604800
    assert config.get("resume_ttl_telegram_sec") == 86400
    assert config.get("resume_ttl_routine_sec") == 300
    assert config.get("resume_routine_auto") is True
