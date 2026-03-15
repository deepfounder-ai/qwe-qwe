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
                lines.append(f"  {k}: {'█' * v}{'░' * (10-v)} {v}/10")
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
                send_message(chat_id, "👤 No profile data yet.\n\nSet with: `/profile set name Кирилл`", token, topic_id=topic_id)
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
            enabled = db.kv_get("heartbeat:enabled") == "1"
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
        conn = _db._get_conn()
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
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

    # Embeddings
    try:
        import requests as _req
        r = _req.get(f"{config.EMBED_BASE_URL}/models", timeout=5)
        if r.ok:
            ids = [m["id"] for m in r.json().get("data", [])]
            if config.EMBED_MODEL in ids:
                ok("Embeddings", f"`{config.EMBED_MODEL}`")
            else:
                warn("Embeddings", f"`{config.EMBED_MODEL}` not found")
        else:
            fail("Embeddings", f"HTTP {r.status_code}")
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


def send_message(chat_id: int, text: str, token: str | None = None,
                 reply_to: int | None = None, topic_id: int | None = None):
    """Send a message to a Telegram chat with MarkdownV2 formatting."""
    token = token or get_token()
    if not token:
        return
    # Split long messages (Telegram limit: 4096 chars)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        base_kwargs = {"chat_id": chat_id}
        if reply_to:
            base_kwargs["reply_to_message_id"] = reply_to
        if topic_id:
            base_kwargs["message_thread_id"] = topic_id

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

    me = get_me(token)
    if not me:
        _log.error("failed to connect to Telegram — invalid token?")
        _running = False
        return

    # Delete any existing webhook (required for long polling)
    _api("deleteWebhook", token, drop_pending_updates=False)
    _log.info("webhook cleared")

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

        if _handle_bot_command(cmd_part, cmd_args, chat_id, user_id, token,
                               topic_id=topic_id, thread_id=cmd_thread):
            return

    # ── Private chat ──
    if chat_type == "private":
        if user_id != owner_id:
            _log.warning(f"blocked DM from non-owner {user_id} (@{username})")
            return
        _process_message(chat_id, text, user_id, username, message_id, token,
                         topic_id=None, thread_id=None, image_b64=image_b64)
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

        _process_message(chat_id, text, user_id, username, message_id, token,
                         topic_id=topic_id, thread_id=thread_id,
                         image_b64=image_b64)


def _process_message(chat_id: int, text: str, user_id: int, username: str,
                     message_id: int, token: str, topic_id: int | None = None,
                     thread_id: str | None = None, image_b64: str | None = None):
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
            # Wait 4s before next (typing lasts 5s, refresh before expiry)
            typing_active.wait(4)
            if not typing_active.is_set():
                break

    typing_thread = threading.Thread(target=_keep_typing, daemon=True)
    typing_thread.start()

    if _on_message:
        try:
            response = _on_message(chat_id, text, user_id, username, thread_id,
                                    image_b64=image_b64)
            if response:
                typing_active.clear()  # stop typing before sending
                send_message(chat_id, response, token, reply_to=message_id, topic_id=topic_id)
        except Exception as e:
            _log.error(f"handler error: {e}", exc_info=True)
            typing_active.clear()
            send_message(chat_id, f"⚠️ Error: {str(e)[:200]}", token, topic_id=topic_id)
        finally:
            typing_active.clear()


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
