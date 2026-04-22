"""Tests for memory._scrub_secrets — redact common key shapes before persisting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_plain_text_unchanged():
    from memory import _scrub_secrets
    out, hit = _scrub_secrets("just a plain note about the project")
    assert not hit
    assert out == "just a plain note about the project"


def test_empty_string():
    from memory import _scrub_secrets
    out, hit = _scrub_secrets("")
    assert not hit
    assert out == ""


def test_openai_key():
    from memory import _scrub_secrets
    out, hit = _scrub_secrets("my key is sk-abcdef1234567890ABCDEF and here")
    assert hit
    assert "sk-abcdef" not in out
    assert "[REDACTED:openai_key]" in out


def test_anthropic_key_matched_before_generic_sk():
    from memory import _scrub_secrets
    token = "sk-ant-" + "a" * 40
    out, hit = _scrub_secrets(f"ANTHROPIC_API_KEY={token}")
    assert hit
    # The anthropic pattern must beat the generic sk- pattern
    assert "[REDACTED:anthropic_key]" in out or "[REDACTED]" in out
    assert token not in out


def test_groq_key():
    from memory import _scrub_secrets
    out, hit = _scrub_secrets("gsk_" + "x" * 40)
    assert hit
    assert "[REDACTED:groq_key]" in out


def test_github_pat_vs_ghp():
    from memory import _scrub_secrets
    out1, h1 = _scrub_secrets("ghp_" + "a" * 36)
    out2, h2 = _scrub_secrets("github_pat_" + "b" * 60)
    assert h1 and "[REDACTED:github_token]" in out1
    assert h2 and "[REDACTED:github_pat]" in out2


def test_aws_access_key():
    from memory import _scrub_secrets
    out, hit = _scrub_secrets("AKIAABCDEFGHIJKLMNOP")
    assert hit
    assert "[REDACTED:aws_access_key]" in out


def test_slack_token():
    from memory import _scrub_secrets
    out, hit = _scrub_secrets("xoxb-1234567890-abcdefghij")
    assert hit
    assert "[REDACTED:slack_token]" in out


def test_jwt():
    from memory import _scrub_secrets
    jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3ODkw.SflKxwRJSMeKKF2QT4"
    out, hit = _scrub_secrets(f"auth: {jwt}")
    assert hit
    assert "[REDACTED:jwt]" in out
    assert jwt not in out


def test_dotenv_line():
    from memory import _scrub_secrets
    src = "OPENAI_API_KEY=hunter2\nDATABASE_PASSWORD=sekret\nOTHER=plain"
    out, hit = _scrub_secrets(src)
    assert hit
    assert "OPENAI_API_KEY=[REDACTED]" in out
    assert "DATABASE_PASSWORD=[REDACTED]" in out
    assert "OTHER=plain" in out
    assert "hunter2" not in out
    assert "sekret" not in out


def test_multiple_matches_all_scrubbed():
    from memory import _scrub_secrets
    src = "sk-" + "A" * 30 + " and later " + "ghp_" + "B" * 36
    out, hit = _scrub_secrets(src)
    assert hit
    assert "[REDACTED:openai_key]" in out
    assert "[REDACTED:github_token]" in out
