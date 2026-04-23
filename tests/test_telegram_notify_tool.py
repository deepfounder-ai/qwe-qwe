"""telegram_notify_owner tool — high-level send for cron + ad-hoc use.

Before this tool existed, the agent had to discover the bot token from
secrets/settings + the owner chat_id from logs/memory + craft an HTTPS
POST to api.telegram.org — typically 3-5 wasted rounds on each cron
"send me X to telegram" task. The new tool collapses that to one call.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def fresh_tools(qwe_temp_data_dir, monkeypatch):
    """Reload tools against a clean DB so no leaked KV state matters."""
    import importlib
    import sys
    for m in ("config", "db", "telegram_bot", "tools"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    return sys.modules["tools"]


def test_telegram_notify_owner_is_a_core_tool(fresh_tools):
    """Must be in the core-always-loaded set so cron tasks don't need tool_search first."""
    tools = fresh_tools
    names = [t.get("function", {}).get("name") for t in tools.TOOLS]
    assert "telegram_notify_owner" in names, (
        f"tool missing from core TOOLS list; names={sorted(filter(None, names))}"
    )


def test_telegram_notify_without_owner_returns_clean_error(fresh_tools):
    """No verified owner → actionable error, not a traceback."""
    tools = fresh_tools
    result = tools.execute("telegram_notify_owner", {"text": "hello"})
    assert "Error" in result or "error" in result
    # Error message tells the user what to do
    assert "owner" in result.lower() or "verified" in result.lower() or "token" in result.lower()


def test_telegram_notify_without_text_rejected(fresh_tools):
    """Missing text → explicit error rather than sending an empty message."""
    tools = fresh_tools
    result = tools.execute("telegram_notify_owner", {})
    assert "Error" in result
    assert "text" in result.lower()


def test_telegram_notify_happy_path_sends_via_bot(fresh_tools, monkeypatch):
    """Owner + token set → send_message invoked, tool returns 'Sent.' confirmation."""
    tools = fresh_tools
    import db
    import telegram_bot

    # Simulate a verified owner + configured bot
    db.kv_set("telegram:owner_id", "12345")
    db.kv_set("telegram:bot_token", "test-token")

    sent = []

    def _fake_send(chat_id, text, token=None, reply_to=None, topic_id=None, reply_markup=None):
        sent.append({"chat_id": chat_id, "text": text, "token": token})

    monkeypatch.setattr(telegram_bot, "send_message", _fake_send)

    result = tools.execute("telegram_notify_owner", {"text": "hi there"})

    assert sent, "send_message must have been invoked"
    assert sent[0]["chat_id"] == 12345
    assert sent[0]["text"] == "hi there"
    assert sent[0]["token"] == "test-token"
    # Confirmation string must contain "Sent" so the dry-run validator passes
    assert "Sent" in result, f"result should confirm delivery, got {result!r}"
