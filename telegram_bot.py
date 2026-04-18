"""Telegram Bot integration — receive and send messages via Telegram Bot API.

Uses long polling (no webhook needed). Works behind NAT, WSL, etc.

Setup:
1. Create bot via @BotFather → get token
2. Set token via CLI (`/telegram token <TOKEN>`) or Settings → System
3. Generate activation code in web UI or CLI
4. Send the code to the bot in Telegram
5. If correct → you're the verified owner
6. If wrong → 3 attempts max, then permanent ban by Telegram user ID

Security:
- Activation codes are one-time, 6 digits, TTL 10 minutes
- After 3 failed attempts, Telegram user ID is permanently banned
- Only verified owner can chat; others are ignored

Features:
- Private chat with owner
- Group support: bot responds in allowed groups
- Supergroup topics: Telegram topic_id ↔ qwe-qwe thread
"""

import atexit
import os
import sys
import threading
import time
import json
import random
import string
import requests
from typing import Callable

import db
import logger
import config

_log = logger.get("telegram")

_thread: threading.Thread | None = None
_running = False
_on_message: Callable | None = None
_pending_code: str | None = None  # verification code awaiting confirmation
_lock_held = False                 # True while this process owns the lock


# ── Single-instance lock ───────────────────────────────────────────────
#
# Telegram allows exactly one long-poll client per bot token. If a second
# qwe-qwe process (or a stale background uvicorn from a previous session)
# also calls start(), both clients `getUpdates` and Telegram returns a
# "Conflict: terminated by other getUpdates request" error on whoever
# isn't the latest caller — spamming the log.
#
# To prevent this we take a process-level lock via a PID file at
# ~/.qwe-qwe/telegram.lock BEFORE entering the polling loop:
#
#   * if the file doesn't exist → create it atomically, write our PID
#   * if it exists and holds a LIVE PID → another instance already runs,
#     back off silently
#   * if it exists but the PID is DEAD → stale lock from a crashed process,
#     remove and retake
#
# The lock is released by stop() OR an atexit handler OR the next call to
# start() that detects a stale PID.


def _pid_alive(pid: int) -> bool:
    """Cross-platform check whether `pid` is a live process."""
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return sys.platform != "win32"


def _lock_file_path() -> "Path":
    from pathlib import Path
    return Path(config.DATA_DIR) / "telegram.lock"


# Rate-limit the "lock held" log line so a loop of repeated start() calls
# (from a buggy caller) doesn't spam the log. Only log once per minute.
_last_lock_held_log = 0.0


def _acquire_lock() -> bool:
    """Try to acquire the telegram single-instance PID lock.

    Returns True iff this process now owns the lock. Clears stale locks
    left by crashed processes. Safe to call repeatedly.
    """
    global _lock_held, _last_lock_held_log
    lock_path = _lock_file_path()

    # Stale-lock sweep
    if lock_path.exists():
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
            other_pid = int(raw) if raw else 0
        except (OSError, ValueError):
            other_pid = 0
        if other_pid and _pid_alive(other_pid):
            # Expected state — not a warning. Downgrade to INFO and
            # rate-limit to avoid spamming the log if some caller loops.
            now = time.time()
            if now - _last_lock_held_log > 60:
                _log.info(
                    f"telegram lock held by PID {other_pid}; "
                    f"not starting a second bot instance"
                )
                _last_lock_held_log = now
            return False
        # Stale → remove
        try:
            lock_path.unlink()
            _log.info(f"removed stale telegram lock (PID {other_pid or '?'} not alive)")
        except OSError:
            pass

    # Atomic create — O_EXCL wins the race even if two processes are
    # attempting to acquire simultaneously.
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Rare but legit race: another process beat us to the atomic create
        # between our stale-sweep check above and here. Same rate limit.
        now = time.time()
        if now - _last_lock_held_log > 60:
            _log.info("telegram lock race: another instance acquired it first")
            _last_lock_held_log = now
        return False
    except OSError as e:
        _log.error(f"failed to create telegram lock at {lock_path}: {e}")
        return False

    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        os.close(fd)
    _lock_held = True
    return True


def _release_lock() -> None:
    """Release the telegram lock if we own it. Idempotent + safe at exit."""
    global _lock_held
    if not _lock_held:
        return
    path = _lock_file_path()
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                path.unlink()
    except OSError:
        pass
    _lock_held = False


# ── Config ──

def get_token() -> str:
    return db.kv_get("telegram:bot_token") or ""


def set_token(token: str):
    db.kv_set("telegram:bot_token", token.strip())


def is_enabled() -> bool:
    return db.kv_get("telegram:enabled") == "1"


def set_enabled(enabled: bool):
    db.kv_set("telegram:enabled", "1" if enabled else "0")


def get_owner_id() -> int | None:
    """Get verified owner's Telegram user ID."""
    raw = db.kv_get("telegram:owner_id")
    return int(raw) if raw else None


def set_owner_id(user_id: int):
    db.kv_set("telegram:owner_id", str(user_id))


def get_owner_username() -> str:
    return db.kv_get("telegram:owner_username") or ""


def is_verified() -> bool:
    return get_owner_id() is not None


def get_allowed_groups() -> list[int]:
    """Get list of allowed group chat IDs."""
    raw = db.kv_get("telegram:allowed_groups")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def set_allowed_groups(group_ids: list[int]):
    db.kv_set("telegram:allowed_groups", json.dumps(group_ids))


def get_group_mode() -> str:
    """How bot responds in groups: 'mention' (only when mentioned) or 'all' (every message)."""
    return db.kv_get("telegram:group_mode") or "mention"


def set_group_mode(mode: str):
    db.kv_set("telegram:group_mode", mode)


def is_topics_enabled() -> bool:
    """Whether to map Telegram topics to qwe-qwe threads."""
    return db.kv_get("telegram:topics_enabled") != "0"  # default on


def set_topics_enabled(enabled: bool):
    db.kv_set("telegram:topics_enabled", "1" if enabled else "0")


# ── Verification ──

ACTIVATION_TTL = 600  # 10 minutes
MAX_ATTEMPTS = 3      # permanent ban after this


def generate_activation_code() -> str:
    """Generate a 6-digit activation code (called from web UI or CLI).
    
    Returns the code. User must send it to the bot in Telegram.
    Code expires after 10 minutes.
    """
    global _pending_code
    _pending_code = ''.join(random.choices(string.digits, k=6))
    db.kv_set("telegram:pending_code", _pending_code)
    db.kv_set("telegram:code_created_at", str(time.time()))
    _log.info(f"activation code generated: {_pending_code}")
    return _pending_code


def get_pending_code() -> str | None:
    """Get pending code if it exists and hasn't expired."""
    global _pending_code
    code = _pending_code or db.kv_get("telegram:pending_code")
    if not code:
        return None
    # Check TTL
    created = db.kv_get("telegram:code_created_at")
    if created:
        age = time.time() - float(created)
        if age > ACTIVATION_TTL:
            _log.info(f"activation code expired (age={int(age)}s)")
            clear_verification()
            return None
    return code


def clear_verification():
    """Clear pending activation code."""
    global _pending_code
    _pending_code = None
    db.kv_set("telegram:pending_code", "")
    db.kv_set("telegram:code_created_at", "")


def verify_code(code: str) -> bool:
    """Check if the provided code matches (and hasn't expired)."""
    pending = get_pending_code()
    return bool(pending and code.strip() == pending)


# ── Ban system ──

def _ban_key(user_id: int) -> str:
    return f"telegram:banned:{user_id}"


def _attempts_key(user_id: int) -> str:
    return f"telegram:attempts:{user_id}"


def is_banned(user_id: int) -> bool:
    """Check if a Telegram user ID is permanently banned."""
    return db.kv_get(_ban_key(user_id)) == "1"


def ban_user(user_id: int):
    """Permanently ban a Telegram user ID."""
    db.kv_set(_ban_key(user_id), "1")
    _log.warning(f"permanently banned user {user_id}")


def get_attempts(user_id: int) -> int:
    """Get number of failed activation attempts for a user."""
    raw = db.kv_get(_attempts_key(user_id))
    return int(raw) if raw else 0


def increment_attempts(user_id: int) -> int:
    """Increment failed attempts. Returns new count. Bans if >= MAX_ATTEMPTS."""
    count = get_attempts(user_id) + 1
    db.kv_set(_attempts_key(user_id), str(count))
    if count >= MAX_ATTEMPTS:
        ban_user(user_id)
    return count


def clear_attempts(user_id: int):
    """Clear failed attempt counter (after successful verification)."""
    db.kv_set(_attempts_key(user_id), "0")


# ── Topic ↔ Thread mapping ──

def _topic_thread_key(chat_id: int, topic_id: int) -> str:
    return f"telegram:topic_thread:{chat_id}:{topic_id}"


def get_thread_for_topic(chat_id: int, topic_id: int) -> str | None:
    """Get qwe-qwe thread_id for a Telegram topic."""
    return db.kv_get(_topic_thread_key(chat_id, topic_id))


def set_thread_for_topic(chat_id: int, topic_id: int, thread_id: str):
    """Map a Telegram topic to a qwe-qwe thread."""
    db.kv_set(_topic_thread_key(chat_id, topic_id), thread_id)


def _get_or_create_thread_for_topic(chat_id: int, topic_id: int, topic_name: str = "") -> str:
    """Get existing thread or create new one for a Telegram topic."""
    import threads
    tid = get_thread_for_topic(chat_id, topic_id)
    if tid:
        t = threads.get(tid)
        if t:
            return tid
    # Create new thread
    name = topic_name or f"TG Topic #{topic_id}"
    t = threads.create(name, meta={"telegram_chat_id": chat_id, "telegram_topic_id": topic_id})
    set_thread_for_topic(chat_id, topic_id, t["id"])
    _log.info(f"created thread '{name}' ({t['id']}) for topic {topic_id} in chat {chat_id}")
    return t["id"]


def _get_or_create_dm_thread(chat_id: int) -> str:
    """Get or create a dedicated thread for Telegram DM (private chat).
    This ensures Telegram private messages never mix with web UI threads."""
    import threads
    kv_key = f"telegram:dm_thread:{chat_id}"
    tid = db.kv_get(kv_key)
    if tid:
        t = threads.get(tid)
        if t:
            return tid
    # Create new thread for this DM
    t = threads.create("Telegram DM", meta={"telegram_chat_id": chat_id, "telegram_dm": True})
    db.kv_set(kv_key, t["id"])
    _log.info(f"created DM thread ({t['id']}) for chat {chat_id}")
    return t["id"]


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


# ── Slash commands (dynamic registry) ──

_command_registry: dict[str, dict] = {}  # cmd → {description, handler}


def register_command(cmd: str, description: str, handler=None):
    """Register a bot command. Called at module level or by skills/plugins.
    
    Args:
        cmd: command name without slash (e.g. "status")
        description: shown in Telegram predictive input (max 256 chars)
        handler: optional callable(args, chat_id, user_id, token, topic_id, thread_id) → str|None
    """
    _command_registry[cmd] = {"description": description[:256], "handler": handler}


def get_commands() -> list[dict]:
    """Get all registered commands for Telegram API."""
    return [{"command": k, "description": v["description"]} for k, v in _command_registry.items()]


def _register_commands(token: str):
    """Push registered commands to Telegram for predictive input."""
    commands = get_commands()
    result = _api("setMyCommands", token, commands=commands)
    if result.get("ok"):
        _log.info(f"registered {len(commands)} slash commands")
    else:
        _log.warning(f"failed to register commands: {result}")


# ── Built-in commands ──
register_command("chatid", "Show chat ID and topic ID")
register_command("status", "Agent status (model, provider, memory)")
register_command("soul", "Show personality traits")
register_command("model", "Show current model and provider")
register_command("skills", "List active skills")
register_command("memory", "Search agent memory")
register_command("threads", "List conversation threads")
register_command("stats", "Session statistics")
register_command("clear", "Clear conversation in this thread")
register_command("settings", "View/edit agent settings")
register_command("cron", "List scheduled tasks")
register_command("thinking", "Toggle thinking mode on/off")
register_command("doctor", "Run diagnostics on all components")
register_command("profile", "View/edit user profile")
register_command("heartbeat", "Manage periodic tasks checklist")
register_command("voice", "Toggle voice mode (TTS) for this chat")
register_command("help", "Show available commands")


def _handle_bot_command(cmd: str, args: str, chat_id: int, user_id: int,
                        token: str, topic_id: int | None = None,
                        thread_id: str | None = None) -> bool:
    """Handle built-in bot commands. Returns True if handled."""
    import providers, soul, skills, threads as thr

    if cmd == "chatid":
        info = f"📋 Chat ID: `{chat_id}`"
        if topic_id:
            info += f"\nTopic ID: `{topic_id}`"
        send_message(chat_id, info, token, topic_id=topic_id)
        return True

    if cmd == "status":
        s = soul.load()
        model = providers.get_model()
        prov = providers.get_active_name()
        active_skills = skills.get_active()
        import memory as mem
        mem_count = 0
        try:
            mem_count = mem.count()
        except Exception:
            pass
        msg = (
            f"⚡ *{s['name']}*\n"
            f"Model: `{model}` @ {prov}\n"
            f"Skills: {', '.join(sorted(active_skills)) or 'none'}\n"
            f"Memories: {mem_count}"
        )
        send_message(chat_id, msg, token, topic_id=topic_id)
        return True

    if cmd == "soul":
        s = soul.load()
        lines = [f"🎭 *{s.get('name', 'Agent')}* ({s.get('language', '?')})"]
        for k, v in s.items():
            if k not in ("name", "language"):
                lines.append(f"  {k}: {v}")
        send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        return True

    if cmd == "model":
        model = providers.get_model()
        prov = providers.get_active_name()
        send_message(chat_id, f"🤖 Model: `{model}`\nProvider: {prov}", token, topic_id=topic_id)
        return True

    if cmd == "skills":
        active = skills.get_active()
        all_skills = skills.list_all()
        lines = []
        for s in all_skills:
            mark = "✅" if s["name"] in active else "◻️"
            lines.append(f"{mark} {s['name']}")
        send_message(chat_id, "🧩 *Skills*\n" + "\n".join(lines), token, topic_id=topic_id)
        return True

    if cmd == "memory":
        if not args:
            send_message(chat_id, "Usage: `/memory <query>`", token, topic_id=topic_id)
            return True
        import memory as mem
        results = mem.search(args, limit=3)
        if results:
            lines = ["🧠 *Memory search:*"]
            for r in results:
                text = r.get("text", "")[:200]
                score = r.get("score", 0)
                lines.append(f"• ({score:.2f}) {text}")
            send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        else:
            send_message(chat_id, "Nothing found.", token, topic_id=topic_id)
        return True

    if cmd == "threads":
        all_t = thr.list_all()
        active_id = thr.get_active_id()
        lines = ["🧵 *Threads:*"]
        for t in all_t[:10]:
            mark = "→" if t["id"] == (thread_id or active_id) else " "
            lines.append(f"{mark} {t['name']} ({t['messages']} msgs)")
        send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        return True

    if cmd == "stats":
        import db as _db
        turns = _db.kv_get("session_turns") or "0"
        prompt_t = int(_db.kv_get("session_prompt_tokens") or "0")
        compl_t = int(_db.kv_get("session_completion_tokens") or "0")
        msg = (
            f"📊 *Stats*\n"
            f"Turns: {turns}\n"
            f"Tokens: {prompt_t + compl_t} (prompt: {prompt_t}, completion: {compl_t})"
        )
        send_message(chat_id, msg, token, topic_id=topic_id)
        return True

    if cmd == "clear":
        import db as _db
        _db.clear_history(thread_id=thread_id)
        send_message(chat_id, "🗑 History cleared for this thread.", token, topic_id=topic_id)
        return True

    if cmd == "profile":
        if args and args.startswith("set "):
            # /profile set key value
            set_parts = args[4:].strip().split(None, 1)
            if len(set_parts) == 2:
                key = set_parts[0].strip().lower().replace(" ", "_")
                val = set_parts[1].strip()
                db.kv_set(f"user:{key}", val)
                send_message(chat_id, f"✅ Profile: `{key}` = {val}", token, topic_id=topic_id)
            else:
                send_message(chat_id, "Usage: `/profile set <key> <value>`", token, topic_id=topic_id)
        else:
            profile = db.kv_get_prefix("user:")
            if not profile:
                send_message(chat_id, "👤 No profile data yet.\n\nSet with: `/profile set name John`", token, topic_id=topic_id)
            else:
                lines = ["👤 *Profile:*"]
                for k, v in sorted(profile.items()):
                    lines.append(f"• {k.replace('user:', '')}: {v}")
                lines.append(f"\nEdit: `/profile set <key> <value>`")
                send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        return True

    if cmd == "heartbeat":
        import scheduler
        if not args:
            # Show status and items
            enabled = db.kv_get("heartbeat:enabled") != "0"  # on by default
            raw = db.kv_get("heartbeat:items")
            items = json.loads(raw) if raw else []
            status_icon = "🟢" if enabled else "⚪"
            lines = [f"{status_icon} *Heartbeat* {'ON' if enabled else 'OFF'}"]
            if items:
                for i, item in enumerate(items, 1):
                    lines.append(f"  {i}. {item}")
            else:
                lines.append("  No items yet.")
            lines.append(f"\n`/heartbeat add <task>`\n`/heartbeat remove <N>`\n`/heartbeat on` / `off`")
            send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        elif args.startswith("add "):
            task_text = args[4:].strip().strip('"').strip("'")
            if not task_text:
                send_message(chat_id, "Usage: `/heartbeat add <task>`", token, topic_id=topic_id)
            else:
                raw = db.kv_get("heartbeat:items")
                items = json.loads(raw) if raw else []
                items.append(task_text)
                db.kv_set("heartbeat:items", json.dumps(items))
                send_message(chat_id, f"✅ Added: {task_text}", token, topic_id=topic_id)
        elif args.startswith("remove "):
            try:
                idx = int(args[7:].strip()) - 1
                raw = db.kv_get("heartbeat:items")
                items = json.loads(raw) if raw else []
                if 0 <= idx < len(items):
                    removed = items.pop(idx)
                    db.kv_set("heartbeat:items", json.dumps(items))
                    send_message(chat_id, f"✅ Removed: {removed}", token, topic_id=topic_id)
                else:
                    send_message(chat_id, f"Invalid index. Use `/heartbeat` to see items.", token, topic_id=topic_id)
            except ValueError:
                send_message(chat_id, "Usage: `/heartbeat remove <number>`", token, topic_id=topic_id)
        elif args.strip() == "on":
            db.kv_set("heartbeat:enabled", "1")
            scheduler._register_heartbeat()
            interval = config.get("heartbeat_interval_min")
            send_message(chat_id, f"🟢 Heartbeat ON (every {interval}m)", token, topic_id=topic_id)
        elif args.strip() == "off":
            db.kv_set("heartbeat:enabled", "0")
            scheduler._unregister_heartbeat()
            send_message(chat_id, "⚪ Heartbeat OFF", token, topic_id=topic_id)
        else:
            send_message(chat_id, "Usage: `/heartbeat`, `add <task>`, `remove <N>`, `on`, `off`", token, topic_id=topic_id)
        return True

    if cmd == "settings":
        parts = args.split(None, 1) if args else []
        if len(parts) == 2:
            # /settings key value
            key, val = parts
            result = config.set(key, val)
            send_message(chat_id, result, token, topic_id=topic_id)
        else:
            # /settings — show all
            all_s = config.get_all()
            lines = ["⚙️ **Settings:**\n"]
            for k, info in all_s.items():
                v = info["value"]
                d = info["default"]
                marker = "" if v == d else " *(modified)*"
                lines.append(f"• `{k}` = **{v}**{marker}")
                lines.append(f"  _{info['description']}_ ({info['min']}-{info['max']})")
            lines.append(f"\nEdit: `/settings <key> <value>`")
            send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        return True

    if cmd == "cron":
        import scheduler
        tasks_list = scheduler.list_tasks()
        if not tasks_list:
            send_message(chat_id, "📋 No scheduled tasks.\n\nAsk me to schedule something, e.g.:\n_\"Remind me to stretch every 2h\"_", token, topic_id=topic_id)
            return True
        lines = ["📋 **Scheduled tasks:**\n"]
        for t in tasks_list:
            status = "🟢" if t["enabled"] else "⚪"
            repeat = "🔄" if t["repeat"] else "⏱"
            lines.append(f"{status} #{t['id']} {repeat} **{t['name']}**")
            lines.append(f"    → {t['next_run']} ({t['schedule']})")
            lines.append(f"    _{t['task'][:80]}_")
        send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        return True

    if cmd == "voice":
        key = f"voice_mode:{chat_id}"
        current = db.kv_get(key) == "1"
        new_val = not current
        import tts
        if new_val and not tts.is_available():
            send_message(chat_id, "TTS not available.\n\ntts_enabled = " + str(config.get("tts_enabled")) + "\ntts_api_url = " + (config.get("tts_api_url") or "(empty)") + "\n\nEnable TTS and set API URL in Settings → Voice.", token, topic_id=topic_id)
            return True
        db.kv_set(key, "1" if new_val else "0")
        if new_val:
            send_message(chat_id, "Voice mode ON\nAll responses will include voice.", token, topic_id=topic_id)
        else:
            send_message(chat_id, "Voice mode OFF\nText-only responses.", token, topic_id=topic_id)
        return True

    if cmd == "thinking":
        current = db.kv_get("thinking_enabled") == "true"
        new_val = not current
        db.kv_set("thinking_enabled", "true" if new_val else "false")
        status_text = "💭 Thinking: **ON**\nModel will reason before responding" if new_val else "💭 Thinking: **OFF**\nFast responses without reasoning"
        send_message(chat_id, status_text, token, topic_id=topic_id)
        return True

    if cmd == "doctor":
        send_message(chat_id, "🔍 Running diagnostics...", token, topic_id=topic_id)
        import threading

        def _run_doctor():
            results = _run_doctor_checks()
            send_message(chat_id, results, token, topic_id=topic_id)

        threading.Thread(target=_run_doctor, daemon=True).start()
        return True

    if cmd == "help":
        lines = ["📖 *Commands:*"]
        for c, info in _command_registry.items():
            lines.append(f"/{c} — {info['description']}")
        send_message(chat_id, "\n".join(lines), token, topic_id=topic_id)
        return True

    # Check dynamic handlers (from skills/plugins)
    if cmd in _command_registry and _command_registry[cmd].get("handler"):
        try:
            result = _command_registry[cmd]["handler"](
                args, chat_id, user_id, token, topic_id, thread_id
            )
            if result:
                send_message(chat_id, result, token, topic_id=topic_id)
            return True
        except Exception as e:
            _log.error(f"command handler error /{cmd}: {e}")
            send_message(chat_id, f"⚠️ Error: {str(e)[:200]}", token, topic_id=topic_id)
            return True

    return False


def _to_html(text: str) -> str:
    """Convert standard Markdown to Telegram HTML format.
    
    More reliable than MarkdownV2 for complex content with code blocks.
    """
    import re as _re
    import html as _html

    # Escape HTML entities first
    # But protect code blocks
    protected = []
    counter = [0]

    def _protect(match):
        idx = counter[0]
        counter[0] += 1
        protected.append(match.group(0))
        return f"\x00HTML{idx}\x00"

    # Protect ```...``` and `...`
    result = _re.sub(r'```(\w*)\n([\s\S]*?)```', _protect, text)
    result = _re.sub(r'`([^`]+)`', _protect, result)

    # Escape HTML in remaining text
    result = _html.escape(result)

    # Convert ~~strikethrough~~ → <s>
    result = _re.sub(r'~~(.+?)~~', r'<s>\1</s>', result)
    # Convert **bold** → <b>bold</b>
    result = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', result)
    # Convert *italic* and _italic_ → <i>italic</i>
    result = _re.sub(r'\*(.+?)\*', r'<i>\1</i>', result)
    result = _re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', result)
    # Convert [text](url) → <a href="url">text</a>
    result = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', result)
    # Convert > blockquote
    result = _re.sub(r'^&gt;\s*(.+)$', r'<blockquote>\1</blockquote>', result, flags=_re.MULTILINE)

    # Restore protected blocks
    for i, p in enumerate(protected):
        if p.startswith('```'):
            # Code block → <pre><code>
            m = _re.match(r'```(\w*)\n([\s\S]*?)```', p)
            if m:
                lang = m.group(1)
                code = _html.escape(m.group(2).rstrip())
                if lang:
                    replacement = f'<pre><code class="language-{lang}">{code}</code></pre>'
                else:
                    replacement = f'<pre><code>{code}</code></pre>'
            else:
                replacement = _html.escape(p)
        elif p.startswith('`'):
            # Inline code → <code>
            code = _html.escape(p.strip('`'))
            replacement = f'<code>{code}</code>'
        else:
            replacement = _html.escape(p)
        result = result.replace(f"\x00HTML{i}\x00", replacement)

    return result


def _to_markdownv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2.
    
    Handles: **bold**, *italic*, `code`, ```codeblocks```, [links](url)
    Escapes special chars outside of formatted regions.
    """
    import re as _re

    # Protect code blocks and inline code first
    protected = []
    counter = [0]

    def _protect(match):
        idx = counter[0]
        counter[0] += 1
        protected.append(match.group(0))
        return f"\x00PROTECTED{idx}\x00"

    # Protect ```...``` and `...`
    result = _re.sub(r'```[\s\S]*?```', _protect, text)
    result = _re.sub(r'`[^`]+`', _protect, result)

    # Convert **bold** → *bold* (MarkdownV2 bold)
    # But first protect existing *italic*
    # Strategy: **text** → \x01bold\x01, then *text* → \x02italic\x02
    bold_parts = []
    def _protect_bold(m):
        idx = len(bold_parts)
        bold_parts.append(m.group(1))
        return f"\x01BOLD{idx}\x01"

    italic_parts = []
    def _protect_italic(m):
        idx = len(italic_parts)
        italic_parts.append(m.group(1))
        return f"\x02ITALIC{idx}\x02"

    # Protect [text](url) links
    link_parts = []
    def _protect_link(m):
        idx = len(link_parts)
        link_parts.append((m.group(1), m.group(2)))
        return f"\x03LINK{idx}\x03"

    result = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _protect_link, result)

    # Protect ~~strikethrough~~
    strike_parts = []
    def _protect_strike(m):
        idx = len(strike_parts)
        strike_parts.append(m.group(1))
        return f"\x04STRIKE{idx}\x04"

    result = _re.sub(r'~~(.+?)~~', _protect_strike, result)

    result = _re.sub(r'\*\*(.+?)\*\*', _protect_bold, result)
    # Handle both *italic* and _italic_
    result = _re.sub(r'\*(.+?)\*', _protect_italic, result)
    result = _re.sub(r'(?<!\w)_(.+?)_(?!\w)', _protect_italic, result)

    # Escape MarkdownV2 special chars in plain text
    special = r'_[]()~>#+-=|{}.!'
    escaped = ""
    for ch in result:
        if ch in special:
            escaped += "\\" + ch
        else:
            escaped += ch
    result = escaped

    # Restore bold → *escaped_text*
    for i, b in enumerate(bold_parts):
        esc_b = ""
        for ch in b:
            if ch in special:
                esc_b += "\\" + ch
            else:
                esc_b += ch
        result = result.replace(f"\x01BOLD{i}\x01", f"*{esc_b}*")

    # Restore italic → _escaped_text_
    for i, it in enumerate(italic_parts):
        esc_it = ""
        for ch in it:
            if ch in special:
                esc_it += "\\" + ch
            else:
                esc_it += ch
        result = result.replace(f"\x02ITALIC{i}\x02", f"_{esc_it}_")

    # Restore strikethrough → ~escaped_text~
    for i, s in enumerate(strike_parts):
        esc_s = ""
        for ch in s:
            if ch in special:
                esc_s += "\\" + ch
            else:
                esc_s += ch
        result = result.replace(f"\x04STRIKE{i}\x04", f"~{esc_s}~")

    # Restore links → [escaped_text](url)
    for i, (link_text, link_url) in enumerate(link_parts):
        esc_lt = ""
        for ch in link_text:
            if ch in special:
                esc_lt += "\\" + ch
            else:
                esc_lt += ch
        # URL: escape only ) and \
        esc_url = link_url.replace("\\", "\\\\").replace(")", "\\)")
        result = result.replace(f"\x03LINK{i}\x03", f"[{esc_lt}]({esc_url})")

    # Restore protected code blocks (no escaping inside)
    for i, p in enumerate(protected):
        result = result.replace(f"\x00PROTECTED{i}\x00", p)

    return result


def _run_doctor_checks() -> str:
    """Run all diagnostics, return formatted string for Telegram."""
    import time as _time
    import config
    import providers
    import memory as mem
    import soul
    import skills
    import threads
    import db as _db

    lines = ["⚡ *qwe-qwe doctor*\n"]
    passed = failed = warns = 0

    def ok(name, msg=""):
        nonlocal passed; passed += 1
        lines.append(f"✅ {name}: {msg}" if msg else f"✅ {name}")
    def warn(name, msg=""):
        nonlocal warns; warns += 1
        lines.append(f"⚠️ {name}: {msg}" if msg else f"⚠️ {name}")
    def fail(name, msg=""):
        nonlocal failed; failed += 1
        lines.append(f"❌ {name}: {msg}" if msg else f"❌ {name}")

    # SQLite
    try:
        msg_count = _db.fetchone("SELECT COUNT(*) FROM messages")[0]
        ok("SQLite", f"{msg_count} messages")
    except Exception as e:
        fail("SQLite", str(e)[:60])

    # Qdrant
    try:
        count = mem.count()
        ok("Qdrant", f"{count} memories")
    except Exception as e:
        fail("Qdrant", str(e)[:60])

    # LLM connection
    try:
        import requests as _req
        url = providers.get_url().rstrip("/")
        r = _req.get(f"{url}/models", timeout=5)
        if r.ok:
            models = [m["id"] for m in r.json().get("data", [])]
            ok("LLM API", f"{len(models)} models")
        else:
            fail("LLM API", f"HTTP {r.status_code}")
    except Exception as e:
        fail("LLM API", str(e)[:60])

    # Model loaded
    try:
        active = providers.get_active_name()
        model = providers.get_model()
        if active in ("lmstudio", "ollama"):
            import requests as _req
            api_base = providers.get_url().rstrip("/").replace("/v1", "")
            r = _req.get(f"{api_base}/api/v1/models", timeout=5)
            if r.ok:
                loaded = any(
                    m.get("key") == model and m.get("loaded_instances")
                    for m in r.json().get("models", [])
                )
                if loaded:
                    ok("Model", f"`{model}` loaded")
                else:
                    warn("Model", f"`{model}` not loaded (auto-loads on use)")
            else:
                warn("Model", "could not check")
        else:
            ok("Model", f"`{model}` @ {active}")
    except Exception as e:
        warn("Model", str(e)[:60])

    # Embeddings (FastEmbed, local ONNX)
    try:
        import memory
        vec = memory.embed("test")
        ok("Embeddings", f"FastEmbed `{memory.DENSE_MODEL_NAME}` ({len(vec)}d)")
    except Exception as e:
        fail("Embeddings", str(e)[:60])

    # Inference
    try:
        providers.ensure_model_loaded()
        client = providers.get_client()
        t0 = _time.time()
        resp = client.chat.completions.create(
            model=providers.get_model(),
            messages=[{"role": "user", "content": "Say 'ok'"}],
            max_tokens=10, temperature=0,
        )
        elapsed = _time.time() - t0
        reply = (resp.choices[0].message.content or "").strip()[:20]
        ok("Inference", f"'{reply}' in {elapsed:.1f}s")
    except Exception as e:
        fail("Inference", str(e)[:60])

    # Telegram
    s = status()
    if s["verified"]:
        ok("Telegram", f"@{s['username']}")
    elif s["running"]:
        warn("Telegram", "running but not verified")
    elif s["has_token"]:
        warn("Telegram", "not running")
    else:
        warn("Telegram", "no token")

    # Threads & Skills
    all_t = threads.list_all()
    ok("Threads", f"{len(all_t)}")
    active_skills = skills.get_active()
    ok("Skills", f"{len(active_skills)} active")

    # Scheduler & Heartbeat
    try:
        import scheduler
        tasks = scheduler.list_tasks()
        ok("Cron", f"{len(tasks)} tasks")
    except Exception:
        warn("Cron", "scheduler not available")

    hb_enabled = _db.kv_get("heartbeat:enabled") != "0"
    raw = _db.kv_get("heartbeat:items")
    import json as _json
    hb_items = _json.loads(raw) if raw else []
    ok("Heartbeat", f"{'ON' if hb_enabled else 'OFF'}, {len(hb_items)} items")

    # Voice (STT / TTS)
    try:
        import stt
        if stt._check_faster_whisper():
            import shutil
            if shutil.which("ffmpeg"):
                ok("STT", f"whisper + ffmpeg (model: {config.get('stt_model')})")
            else:
                warn("STT", "faster-whisper OK, ffmpeg missing")
        else:
            warn("STT", "faster-whisper not installed")
    except Exception:
        warn("STT", "module not available")

    try:
        import tts
        if tts.is_available():
            url = config.get("tts_api_url") or ""
            import requests as _req2
            try:
                _req2.get(url, timeout=3)
                ok("TTS", f"server reachable at {url}")
            except Exception:
                warn("TTS", f"server unreachable at {url}")
        elif str(config.get("tts_enabled")) != "1":
            warn("TTS", "disabled")
        else:
            warn("TTS", "no API URL configured")
    except Exception:
        warn("TTS", "module not available")

    # BM25 / FTS5
    try:
        rag_count = _db.fetchone("SELECT COUNT(*) FROM fts_rag")[0]
        mem_count = _db.fetchone("SELECT COUNT(*) FROM fts_memory")[0]
        ok("BM25", f"{rag_count} rag + {mem_count} memory entries")
    except Exception:
        warn("BM25", "FTS5 tables not found")

    # RAG / Knowledge
    try:
        import rag
        stats = rag.stats()
        ok("RAG", f"{stats.get('total_chunks', 0)} chunks, {stats.get('total_files', 0)} files")
    except Exception:
        warn("RAG", "not available")

    # Disk
    try:
        import shutil
        _, _, free = shutil.disk_usage(".")
        free_gb = free / (1024**3)
        if free_gb < 1:
            warn("Disk", f"{free_gb:.1f}GB free")
        else:
            ok("Disk", f"{free_gb:.1f}GB free")
    except Exception:
        warn("Disk", "unknown")

    # Summary
    total = passed + failed + warns
    lines.append("")
    if failed == 0 and warns == 0:
        lines.append(f"*All {total} checks passed!* ⚡")
    else:
        lines.append(f"*{passed}* passed, *{failed}* failed, *{warns}* warnings")

    return "\n".join(lines)


# ── Inline keyboards ──

def _build_reply_keyboard(tool_names: list[str] | None = None) -> dict:
    """Build an inline keyboard to attach to bot responses."""
    buttons = [[{"text": "🔄 Retry", "callback_data": "retry"}]]
    return {"inline_keyboard": buttons}


def _handle_callback_query(query: dict, token: str):
    """Handle inline keyboard button presses."""
    query_id = query.get("id")
    data = query.get("data", "")
    msg = query.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    user_id = query.get("from", {}).get("id")
    topic_id = msg.get("message_thread_id")
    thread_id = None
    if topic_id:
        thread_id = get_thread_for_topic(chat_id, topic_id)

    if not chat_id:
        _api("answerCallbackQuery", token, callback_query_id=query_id, text="⚠️ Error")
        return

    # Only owner can press buttons
    owner_id = get_owner_id()
    if owner_id and user_id != owner_id:
        _api("answerCallbackQuery", token, callback_query_id=query_id,
             text="⛔ Only the owner can use this")
        return

    if data == "retry":
        # Re-send the last user message
        tid = thread_id or db.kv_get("telegram:thread_id") or None
        recent = db.get_recent_messages(limit=4, thread_id=tid)
        last_user = None
        for m in reversed(recent):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        if last_user:
            _api("answerCallbackQuery", token, callback_query_id=query_id, text="🔄 Retrying...")
            _process_message(chat_id, last_user, user_id, "",
                             msg.get("message_id", 0), token,
                             topic_id=topic_id, thread_id=thread_id)
        else:
            _api("answerCallbackQuery", token, callback_query_id=query_id,
                 text="No message to retry")

    elif data == "clear":
        tid = thread_id or db.kv_get("telegram:thread_id") or None
        db.clear_history(thread_id=tid)
        _api("answerCallbackQuery", token, callback_query_id=query_id,
             text="🗑 History cleared")

    elif data == "toggle_thinking":
        current = db.kv_get("thinking_enabled") == "true"
        new_val = not current
        db.kv_set("thinking_enabled", str(new_val).lower())
        state = "ON ✅" if new_val else "OFF"
        _api("answerCallbackQuery", token, callback_query_id=query_id,
             text=f"🧠 Thinking: {state}")

    elif data == "toggle_voice":
        current = db.kv_get(f"voice_mode:{chat_id}") == "1"
        new_val = not current
        db.kv_set(f"voice_mode:{chat_id}", "1" if new_val else "0")
        state = "ON ✅" if new_val else "OFF"
        _api("answerCallbackQuery", token, callback_query_id=query_id,
             text=f"🔊 Voice: {state}")

    else:
        _api("answerCallbackQuery", token, callback_query_id=query_id, text="Unknown action")


def send_message(chat_id: int, text: str, token: str | None = None,
                 reply_to: int | None = None, topic_id: int | None = None,
                 reply_markup: dict | None = None):
    """Send a message to a Telegram chat with MarkdownV2 formatting."""
    token = token or get_token()
    if not token:
        return
    # Split long messages (Telegram limit: 4096 chars)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for ci, chunk in enumerate(chunks):
        base_kwargs = {"chat_id": chat_id}
        if reply_to:
            base_kwargs["reply_to_message_id"] = reply_to
        if topic_id:
            base_kwargs["message_thread_id"] = topic_id
        # Attach keyboard only to last chunk
        if reply_markup and ci == len(chunks) - 1:
            base_kwargs["reply_markup"] = reply_markup

        # Try MarkdownV2 → HTML → Markdown → plain text
        sent = False

        # 1. MarkdownV2
        md2_text = _to_markdownv2(chunk)
        result = _api("sendMessage", token, **base_kwargs, text=md2_text, parse_mode="MarkdownV2")
        if result.get("ok"):
            sent = True

        # 2. HTML fallback
        if not sent:
            html_text = _to_html(chunk)
            result = _api("sendMessage", token, **base_kwargs, text=html_text, parse_mode="HTML")
            if result.get("ok"):
                sent = True
                _log.info("sent as HTML fallback")

        # 3. Legacy Markdown
        if not sent:
            result = _api("sendMessage", token, **base_kwargs, text=chunk, parse_mode="Markdown")
            if result.get("ok"):
                sent = True
                _log.info("sent as legacy Markdown fallback")

        # 4. Plain text (last resort)
        if not sent:
            _api("sendMessage", token, **base_kwargs, text=chunk)
            _log.warning("sent as plain text (all formatting failed)")


def send_draft(chat_id: int, text: str, token: str | None = None,
               topic_id: int | None = None, reply_to: int | None = None) -> dict:
    """Send a message draft (streaming) via sendMessageDraft (Bot API 9.3+).

    The draft is shown to the user in real-time and gets replaced when
    a final send_message() is sent. Falls back silently on older API.
    """
    token = token or get_token()
    if not token or not text:
        return {"ok": False}
    kwargs: dict = {"chat_id": chat_id, "text": text[:4000]}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    if reply_to:
        kwargs["reply_parameters"] = {"message_id": reply_to}
    return _api("sendMessageDraft", token, **kwargs)


_draft_supported: bool | None = None  # cached: does bot API support sendMessageDraft?


def _send_draft_safe(chat_id: int, text: str, token: str,
                     topic_id: int | None = None, reply_to: int | None = None) -> bool:
    """Send draft, return True if supported. Caches API support check."""
    global _draft_supported
    if _draft_supported is False:
        return False
    result = send_draft(chat_id, text, token, topic_id=topic_id, reply_to=reply_to)
    if result.get("ok"):
        _draft_supported = True
        return True
    # Check if method is not supported (older Bot API)
    desc = result.get("description", "").lower()
    if "not found" in desc or "unknown method" in desc or "bad request" in desc:
        _draft_supported = False
        _log.info("sendMessageDraft not supported — falling back to final-only mode")
        return False
    return False


def _send_audio(chat_id: int, audio_bytes: bytes, token: str,
                reply_to: int | None = None, topic_id: int | None = None):
    """Send an audio file (mp3) to a Telegram chat via multipart upload."""
    import io
    url = f"https://api.telegram.org/bot{token}/sendAudio"
    data = {"chat_id": chat_id, "title": "Voice reply"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    if topic_id:
        data["message_thread_id"] = topic_id
    files = {"audio": ("voice.mp3", io.BytesIO(audio_bytes), "audio/mpeg")}
    try:
        r = requests.post(url, data=data, files=files, timeout=60)
        result = r.json()
        if not result.get("ok"):
            _log.warning(f"sendAudio failed: {result.get('description', '')}")
    except Exception as e:
        _log.error(f"sendAudio error: {e}")


def get_me(token: str | None = None) -> dict:
    """Get bot info."""
    token = token or get_token()
    if not token:
        return {}
    result = _api("getMe", token)
    return result.get("result", {})


# ── Polling loop ──

def start(on_message: Callable | None = None):
    """Start the Telegram bot polling loop.

    Acquires a process-level lock first so a second qwe-qwe instance
    (e.g. a leftover uvicorn) never ends up fighting the first one
    over getUpdates.
    """
    global _thread, _running, _on_message

    if _running:
        _log.info("already running")
        return

    token = get_token()
    if not token:
        _log.warning("no bot token configured")
        return

    if not _acquire_lock():
        # Another live qwe-qwe instance already owns the lock. Don't start
        # polling — bail silently to avoid log spam.
        return

    _on_message = on_message
    _running = True
    # Ensure the lock is released even if the process exits uncleanly.
    atexit.register(_release_lock)
    _thread = threading.Thread(target=_poll_loop, args=(token,), daemon=True)
    _thread.start()
    _log.info("telegram bot started")


def stop():
    """Stop the polling loop and release the single-instance lock."""
    global _running
    _running = False
    _release_lock()
    _log.info("telegram bot stopped")


def _poll_loop(token: str):
    """Long polling loop."""
    global _running
    offset = 0

    me = get_me(token)
    if not me:
        _log.error("failed to connect to Telegram — invalid token?")
        _running = False
        return

    # Delete any existing webhook and flush pending updates to kill stale long-poll
    _api("deleteWebhook", token, drop_pending_updates=True)
    # Flush stale getUpdates connection by requesting with short timeout
    _api("getUpdates", token, offset=-1, timeout=1)
    _log.info("webhook cleared, pending updates flushed")

    bot_username = me.get("username", "")
    _log.info(f"connected as @{bot_username}")
    db.kv_set("telegram:bot_username", bot_username)

    # Register slash commands for predictive input
    _register_commands(token)

    while _running:
        try:
            result = _api("getUpdates", token, offset=offset, timeout=30,
                          allowed_updates=["message"])
            if not result.get("ok"):
                _log.warning(f"getUpdates failed: {result.get('description')}")
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                _handle_update(update, token, bot_username)

        except Exception as e:
            _log.error(f"poll error: {e}", exc_info=True)
            time.sleep(5)


def _handle_update(update: dict, token: str, bot_username: str):
    """Process a single Telegram update."""
    # Handle inline keyboard callback queries
    callback_query = update.get("callback_query")
    if callback_query:
        _handle_callback_query(callback_query, token)
        return

    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"].get("type", "private")  # private, group, supergroup
    user_id = msg["from"]["id"]
    username = msg["from"].get("username", "")
    text = msg.get("text", "") or msg.get("caption", "")
    message_id = msg.get("message_id")
    topic_id = msg.get("message_thread_id")  # supergroup topic

    # Handle photos — download and encode as base64
    image_b64 = None
    photo = msg.get("photo")
    if photo:
        try:
            import base64
            file_id = photo[-1]["file_id"]  # highest resolution
            file_info = _api("getFile", get_token(), file_id=file_id)
            if file_info and file_info.get("ok"):
                file_path = file_info["result"]["file_path"]
                url = f"https://api.telegram.org/file/bot{get_token()}/{file_path}"
                image_data = requests.get(url, timeout=30).content
                image_b64 = base64.b64encode(image_data).decode()
                if not text:
                    text = "What's in this image?"
                _log.info(f"photo received from @{username} ({len(image_data)} bytes)")
        except Exception as e:
            _log.error(f"photo download failed: {e}")

    # Handle document files — save to uploads dir and reference by PATH.
    # Agent uses read_file(path) or rag_index_file(path) on demand.
    doc = msg.get("document")
    if doc:
        try:
            import re as _re
            from pathlib import Path
            from server import UPLOADS_DIR
            fname_raw = doc.get("file_name", "file.txt")
            file_id = doc["file_id"]
            file_info = _api("getFile", get_token(), file_id=file_id)
            if file_info and file_info.get("ok"):
                file_path_remote = file_info["result"]["file_path"]
                url = f"https://api.telegram.org/file/bot{get_token()}/{file_path_remote}"
                file_data = requests.get(url, timeout=30).content
                # Sanitize filename and save under a uuid-prefixed name
                import uuid as _uuid
                fname_safe = _re.sub(r'[^\w.\-]+', '_', Path(fname_raw).name)[:80] or "file.txt"
                stem = Path(fname_safe).stem
                ext = Path(fname_safe).suffix or ".txt"
                doc_id = str(_uuid.uuid4())[:8]
                saved = UPLOADS_DIR / f"{doc_id}_{stem}{ext}"
                saved.write_bytes(file_data)
                abs_path = str(saved.resolve())
                size_kb = len(file_data) / 1024
                ref = (
                    f"[File attached: {fname_raw} ({size_kb:.1f} KB) — saved at {abs_path}. "
                    f"To view contents call read_file(path). "
                    f"To add to the knowledge base call tool_search('rag') then rag_index(path).]"
                )
                text = (text + "\n\n" if text else "") + ref
                _log.info(f"document from @{username}: {fname_raw} → {abs_path} ({len(file_data)}b)")
        except Exception as e:
            _log.error(f"document download failed: {e}")

    # Handle voice messages — download, transcribe to text
    is_voice = False
    voice = msg.get("voice") or msg.get("audio")
    if voice and not text:
        try:
            import stt
            if not stt.is_available():
                send_message(chat_id, "🎤 Voice not supported — install faster-whisper:\n`pip install faster-whisper`", token, topic_id=topic_id)
                return
            file_id = voice["file_id"]
            file_info = _api("getFile", get_token(), file_id=file_id)
            if file_info and file_info.get("ok"):
                file_path = file_info["result"]["file_path"]
                url = f"https://api.telegram.org/file/bot{get_token()}/{file_path}"
                audio_data = requests.get(url, timeout=30).content
                fmt = file_path.rsplit(".", 1)[-1] if "." in file_path else "ogg"
                text = stt.transcribe(audio_data, format=fmt)
                if text.startswith("[STT Error]"):
                    send_message(chat_id, text, token, topic_id=topic_id)
                    return
                is_voice = True
                _log.info(f"voice transcribed from @{username} ({len(audio_data)}b): {text[:100]}")
        except Exception as e:
            _log.error(f"voice transcription failed: {e}")
            send_message(chat_id, "⚠️ Failed to transcribe voice message.", token, topic_id=topic_id)
            return

    if not text and not image_b64:
        return

    # ── Verification flow ──
    if not is_verified():
        # Check if user is banned
        if is_banned(user_id):
            _log.warning(f"banned user {user_id} (@{username}) tried to message")
            send_message(chat_id, "🚫 Access denied.", token)
            return

        pending = get_pending_code()
        if not pending:
            # No activation code generated yet — tell user to generate one
            send_message(chat_id,
                "🔐 Activation required.\n\n"
                "Generate an activation code in qwe-qwe:\n"
                "• Web UI → Settings → Telegram\n"
                "• CLI → `/telegram activate`\n\n"
                "Then send the 6-digit code here.",
                token)
            _log.info(f"no pending code, told @{username} ({user_id}) to generate one")
            return

        if text.strip() == pending:
            # Code matches — verify this user as owner
            set_owner_id(user_id)
            db.kv_set("telegram:owner_username", username)
            clear_verification()
            clear_attempts(user_id)
            send_message(chat_id,
                "✅ Verified! You are now the owner.\n\n"
                "I'll only respond to you from now on.",
                token)
            _log.info(f"owner verified: @{username} ({user_id})")
            return
        else:
            # Wrong code
            attempts = increment_attempts(user_id)
            remaining = MAX_ATTEMPTS - attempts
            if remaining <= 0:
                send_message(chat_id, "🚫 Too many failed attempts. Access permanently denied.", token)
                _log.warning(f"user {user_id} (@{username}) banned after {MAX_ATTEMPTS} failed attempts")
            else:
                send_message(chat_id,
                    f"❌ Wrong code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
                    token)
                _log.info(f"wrong code from @{username} ({user_id}), attempt {attempts}/{MAX_ATTEMPTS}")
            return

    # ── Slash commands (work for owner in any chat, bypass group mode) ──
    owner_id = get_owner_id()
    if text.strip().startswith("/") and user_id == owner_id:
        # Parse: "/cmd@botname args" → cmd, args
        parts = text.strip().split(None, 1)
        cmd_part = parts[0].lstrip("/").split("@")[0].lower()
        cmd_args = parts[1] if len(parts) > 1 else ""

        # Determine thread_id for this context
        cmd_thread = None
        if topic_id and chat_type in ("group", "supergroup") and is_topics_enabled():
            cmd_thread = get_thread_for_topic(chat_id, topic_id)
        elif chat_type == "private":
            cmd_thread = _get_or_create_dm_thread(chat_id)
        elif chat_type in ("group", "supergroup"):
            cmd_thread = _get_or_create_dm_thread(chat_id)

        if _handle_bot_command(cmd_part, cmd_args, chat_id, user_id, token,
                               topic_id=topic_id, thread_id=cmd_thread):
            return

    # ── Private chat ──
    if chat_type == "private":
        if user_id != owner_id:
            _log.warning(f"blocked DM from non-owner {user_id} (@{username})")
            return
        # Use dedicated Telegram DM thread (never share with web UI active thread)
        dm_thread = _get_or_create_dm_thread(chat_id)
        _process_message(chat_id, text, user_id, username, message_id, token,
                         topic_id=None, thread_id=dm_thread, image_b64=image_b64,
                         is_voice=is_voice)
        return

    # ── Group/supergroup ──
    if chat_type in ("group", "supergroup"):
        allowed_groups = get_allowed_groups()
        if allowed_groups and chat_id not in allowed_groups:
            return  # silently ignore non-allowed groups

        # Check if bot should respond
        group_mode = get_group_mode()
        should_respond = False

        if group_mode == "all":
            should_respond = True
        elif group_mode == "mention":
            # Respond only if mentioned or replied to
            if f"@{bot_username}" in text:
                text = text.replace(f"@{bot_username}", "").strip()
                should_respond = True
            elif msg.get("reply_to_message", {}).get("from", {}).get("username") == bot_username:
                should_respond = True

        if not should_respond:
            return

        # Only owner can trigger in groups (unless explicitly allowed)
        if user_id != owner_id:
            _log.debug(f"ignored group msg from non-owner {user_id}")
            return

        # Topic → Thread mapping
        thread_id = None
        if topic_id and is_topics_enabled():
            topic_name = ""
            # Try to get topic name from reply
            thread_id = _get_or_create_thread_for_topic(chat_id, topic_id, topic_name)
        else:
            # Group without topics — use dedicated group thread (never share with web UI)
            thread_id = _get_or_create_dm_thread(chat_id)

        _process_message(chat_id, text, user_id, username, message_id, token,
                         topic_id=topic_id, thread_id=thread_id,
                         image_b64=image_b64, is_voice=is_voice)


def _process_message(chat_id: int, text: str, user_id: int, username: str,
                     message_id: int, token: str, topic_id: int | None = None,
                     thread_id: str | None = None, image_b64: str | None = None,
                     is_voice: bool = False):
    """Route message to agent and send response."""
    _log.info(f"processing: @{username} in {chat_id}" +
              (f" topic={topic_id}" if topic_id else "") +
              (f" thread={thread_id}" if thread_id else "") +
              (" [+image]" if image_b64 else "") +
              f": {text[:100]}")

    # Check if model needs loading (notify user about delay)
    import providers
    loading_notified = False
    if providers.get_active_name() in ("lmstudio", "ollama"):
        import requests as _req
        try:
            p = providers.get_provider()
            api_base = p.get("url", "").rstrip("/").replace("/v1", "")
            model = providers.get_model()
            r = _req.get(f"{api_base}/api/v1/models", timeout=5)
            if r.ok:
                models = r.json().get("models", [])
                model_loaded = any(
                    m.get("key") == model and m.get("loaded_instances")
                    for m in models
                )
                if not model_loaded:
                    send_message(chat_id, f"⏳ Loading model `{model}`...", token, topic_id=topic_id)
                    loading_notified = True
        except Exception:
            pass

    # Continuous typing indicator (Telegram expires after 5s)
    typing_active = threading.Event()
    typing_active.set()

    def _keep_typing():
        while typing_active.is_set():
            kwargs = {"chat_id": chat_id, "action": "typing"}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            _api("sendChatAction", token, **kwargs)
            typing_active.wait(4)
            if not typing_active.is_set():
                break

    typing_thread = threading.Thread(target=_keep_typing, daemon=True)
    typing_thread.start()

    # ── Streaming via sendMessageDraft / editMessageText + agent callbacks ──
    import agent

    _stream_buf = ""          # accumulated content for streaming
    _stream_lock = threading.Lock()
    _last_update_ts = [0.0]   # last time we updated the stream message
    _STREAM_INTERVAL = 1.5    # update at most every 1.5s (Telegram rate limits)
    _stream_msg_id = [None]   # message_id of the streaming placeholder (for editMessageText)
    _thinking_buf = []        # accumulated thinking chunks
    _status_text = [""]       # latest tool status

    def _update_stream():
        """Send or update the streaming message with current buffer."""
        with _stream_lock:
            text = _stream_buf.strip()
            if not text:
                return
            # Append status if any
            status = _status_text[0]
            display = text + (f"\n\n_{status}_" if status else "") + " ▍"

        # Try sendMessageDraft first
        if _send_draft_safe(chat_id, display, token,
                            topic_id=topic_id, reply_to=message_id):
            return

        # Fallback: sendMessage + editMessageText
        if _stream_msg_id[0] is None:
            # Send initial placeholder
            kwargs = {"chat_id": chat_id, "text": display}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            if message_id:
                kwargs["reply_to_message_id"] = message_id
            result = _api("sendMessage", token, **kwargs)
            if result.get("ok"):
                _stream_msg_id[0] = result["result"]["message_id"]
                _log.info(f"streaming placeholder sent: msg_id={_stream_msg_id[0]}")
        else:
            # Edit existing message
            result = _api("editMessageText", token,
                 chat_id=chat_id, message_id=_stream_msg_id[0], text=display)
            if not result.get("ok"):
                _log.debug(f"editMessageText: {result.get('description', '')}")

    def _tg_content_cb(text_chunk: str):
        nonlocal _stream_buf
        with _stream_lock:
            _stream_buf += text_chunk
        now = time.time()
        if now - _last_update_ts[0] >= _STREAM_INTERVAL:
            _update_stream()
            _last_update_ts[0] = now

    def _tg_thinking_cb(text_chunk: str):
        _thinking_buf.append(text_chunk)

    def _tg_status_cb(status_text: str):
        _status_text[0] = status_text
        # Immediately update stream with tool progress
        now = time.time()
        if now - _last_update_ts[0] >= _STREAM_INTERVAL:
            _update_stream()
            _last_update_ts[0] = now

    # Set agent callbacks for this request
    streaming_on = db.kv_get("streaming_enabled") != "false"
    agent._content_callback = _tg_content_cb if streaming_on else None
    agent._thinking_callback = _tg_thinking_cb
    agent._status_callback = _tg_status_cb if streaming_on else None

    if _on_message:
        try:
            response = _on_message(chat_id, text, user_id, username, thread_id,
                                    image_b64=image_b64)
            if response:
                typing_active.clear()

                # Get thinking and tool info from agent result
                thinking_text = "".join(_thinking_buf).strip()
                tool_names = getattr(agent, '_last_tools', []) or []

                # Build enriched response (no thinking — too noisy for Telegram)
                parts = []
                parts.append(response)
                if tool_names:
                    tools_str = ", ".join(f"`{t}`" for t in tool_names)
                    parts.append(f"\n🔧 {tools_str}")

                enriched = "\n\n".join(parts)

                # Build inline keyboard
                keyboard = _build_reply_keyboard(tool_names)

                if _stream_msg_id[0]:
                    # Edit the streaming message into the final formatted version
                    # Try MarkdownV2 → HTML → plain text
                    md2 = _to_markdownv2(enriched)
                    result = _api("editMessageText", token,
                                  chat_id=chat_id, message_id=_stream_msg_id[0],
                                  text=md2, parse_mode="MarkdownV2",
                                  reply_markup=keyboard)
                    if not result.get("ok"):
                        html = _to_html(enriched)
                        result = _api("editMessageText", token,
                                      chat_id=chat_id, message_id=_stream_msg_id[0],
                                      text=html, parse_mode="HTML",
                                      reply_markup=keyboard)
                    if not result.get("ok"):
                        _api("editMessageText", token,
                             chat_id=chat_id, message_id=_stream_msg_id[0],
                             text=enriched, reply_markup=keyboard)
                else:
                    # No streaming message was sent — send fresh
                    send_message(chat_id, enriched, token, reply_to=message_id,
                                 topic_id=topic_id, reply_markup=keyboard)

                # TTS: send voice reply if voice mode or incoming was voice
                voice_mode = db.kv_get(f"voice_mode:{chat_id}") == "1"
                if is_voice or voice_mode:
                    try:
                        import tts
                        if tts.is_available():
                            audio = tts.synthesize(response, format="mp3")
                            if audio:
                                _send_audio(chat_id, audio, token, reply_to=message_id, topic_id=topic_id)
                                _log.info(f"TTS voice sent to {chat_id} ({len(audio)} bytes)")
                            else:
                                _log.warning("TTS returned None — server may be down")
                        else:
                            _log.warning(f"TTS not available for voice mode in {chat_id}")
                    except Exception as e:
                        _log.error(f"TTS failed: {e}")
        except Exception as e:
            _log.error(f"handler error: {e}", exc_info=True)
            typing_active.clear()
            send_message(chat_id, f"⚠️ Error: {str(e)[:200]}", token, topic_id=topic_id)
        finally:
            typing_active.clear()
            agent._content_callback = None
            agent._thinking_callback = None
            agent._status_callback = None


# ── Status ──

def status() -> dict:
    """Get bot status."""
    pending = get_pending_code()
    return {
        "enabled": is_enabled(),
        "running": _running,
        "has_token": bool(get_token()),
        "verified": is_verified(),
        "username": db.kv_get("telegram:bot_username") or "",
        "owner_id": get_owner_id(),
        "owner_username": get_owner_username(),
        "has_pending_code": bool(pending),
        "pending_code": pending or "",
        "allowed_groups": get_allowed_groups(),
        "group_mode": get_group_mode(),
        "topics_enabled": is_topics_enabled(),
    }
