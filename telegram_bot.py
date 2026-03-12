"""Telegram Bot integration — receive and send messages via Telegram Bot API.

Uses long polling (no webhook needed). Works behind NAT, WSL, etc.

Setup:
1. Create bot via @BotFather → get token
2. Save token in Settings → System → Telegram Bot
3. Toggle on → bot starts polling

Architecture:
    Telegram ←→ Long Poll ←→ agent.run() ←→ LLM + Tools
"""

import threading
import time
import json
import re
import requests
from typing import Callable

import db
import logger
import config

_log = logger.get("telegram")

_thread: threading.Thread | None = None
_running = False
_on_message: Callable | None = None  # callback(chat_id, text) → response


# ── Config ──

def get_token() -> str:
    return db.kv_get("telegram:bot_token") or ""


def set_token(token: str):
    db.kv_set("telegram:bot_token", token.strip())


def is_enabled() -> bool:
    return db.kv_get("telegram:enabled") == "1"


def set_enabled(enabled: bool):
    db.kv_set("telegram:enabled", "1" if enabled else "0")


def get_allowed_users() -> list[int]:
    """Get list of allowed Telegram user IDs. Empty = allow all."""
    raw = db.kv_get("telegram:allowed_users")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def set_allowed_users(user_ids: list[int]):
    db.kv_set("telegram:allowed_users", json.dumps(user_ids))


# ── API calls ──

def _api(method: str, token: str, **kwargs) -> dict:
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=60)
        return r.json()
    except Exception as e:
        _log.error(f"API error: {method} → {e}")
        return {"ok": False, "description": str(e)}


def send_message(chat_id: int, text: str, token: str | None = None):
    """Send a message to a Telegram chat."""
    token = token or get_token()
    if not token:
        return
    # Split long messages (Telegram limit: 4096 chars)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        _api("sendMessage", token, chat_id=chat_id, text=chunk, parse_mode="Markdown")


def get_me(token: str | None = None) -> dict:
    """Get bot info."""
    token = token or get_token()
    if not token:
        return {}
    result = _api("getMe", token)
    return result.get("result", {})


# ── Polling loop ──

def start(on_message: Callable | None = None):
    """Start the Telegram bot polling loop."""
    global _thread, _running, _on_message

    if _running:
        _log.info("already running")
        return

    token = get_token()
    if not token:
        _log.warning("no bot token configured")
        return

    _on_message = on_message
    _running = True
    _thread = threading.Thread(target=_poll_loop, args=(token,), daemon=True)
    _thread.start()
    _log.info("telegram bot started")


def stop():
    """Stop the polling loop."""
    global _running
    _running = False
    _log.info("telegram bot stopped")


def _poll_loop(token: str):
    """Long polling loop."""
    global _running
    offset = 0

    # Verify bot
    me = get_me(token)
    if not me:
        _log.error("failed to connect to Telegram — invalid token?")
        _running = False
        return
    _log.info(f"connected as @{me.get('username', '?')}")
    db.kv_set("telegram:bot_username", me.get("username", ""))

    while _running:
        try:
            result = _api("getUpdates", token, offset=offset, timeout=30)
            if not result.get("ok"):
                _log.warning(f"getUpdates failed: {result.get('description')}")
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                _handle_update(update, token)

        except Exception as e:
            _log.error(f"poll error: {e}", exc_info=True)
            time.sleep(5)


def _handle_update(update: dict, token: str):
    """Process a single Telegram update."""
    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username", "")
    text = msg.get("text", "")

    if not text:
        return

    # Check allowed users
    allowed = get_allowed_users()
    if allowed and user_id not in allowed:
        _log.warning(f"blocked message from user {user_id} (@{username})")
        return

    _log.info(f"message from @{username} ({user_id}): {text[:100]}")

    # Auto-save first user
    if not allowed:
        set_allowed_users([user_id])
        _log.info(f"auto-allowed first user: {user_id} (@{username})")

    # Send typing indicator
    _api("sendChatAction", token, chat_id=chat_id, action="typing")

    # Route to agent
    if _on_message:
        try:
            response = _on_message(chat_id, text, user_id, username)
            if response:
                send_message(chat_id, response, token)
        except Exception as e:
            _log.error(f"handler error: {e}", exc_info=True)
            send_message(chat_id, f"⚠️ Error: {str(e)[:200]}", token)
    else:
        send_message(chat_id, "Bot is running but no handler configured.", token)


# ── Status ──

def status() -> dict:
    """Get bot status."""
    token = get_token()
    return {
        "enabled": is_enabled(),
        "running": _running,
        "has_token": bool(token),
        "username": db.kv_get("telegram:bot_username") or "",
        "allowed_users": get_allowed_users(),
    }
