"""Soul — agent personality as compact config with low/moderate/high levels."""

import json
from pathlib import Path
import os
import db
import config

# Valid trait levels
LEVELS = ("low", "moderate", "high")
LEVEL_TEMP = {"low": 0.3, "moderate": 0.6, "high": 0.9}

# Default personality template (immutable base — custom traits merged at runtime)
_BUILTIN_DEFAULTS = {
    "name": "Agent",
    "language": "English",
    "humor": "moderate",
    "honesty": "high",
    "curiosity": "moderate",
    "brevity": "high",
    "formality": "low",
    "proactivity": "moderate",
    "empathy": "moderate",
    "creativity": "moderate",
}

_BUILTIN_TRAIT_DESCRIPTIONS = {
    "humor": ("serious", "funny, jokes around"),
    "honesty": ("diplomatic", "direct, brutally honest"),
    "curiosity": ("answers questions", "asks follow-ups, digs deeper"),
    "brevity": ("detailed, verbose", "concise, to the point"),
    "formality": ("casual, friendly", "polite, formal"),
    "proactivity": ("waits for requests", "suggests ideas, acts on own"),
    "empathy": ("rational", "empathetic, caring"),
    "creativity": ("practical, standard", "creative, unconventional"),
}

# Mutable copies rebuilt from builtins + DB custom traits (never stale)
DEFAULTS = dict(_BUILTIN_DEFAULTS)
TRAIT_DESCRIPTIONS = dict(_BUILTIN_TRAIT_DESCRIPTIONS)


def _migrate_numeric(val: str) -> str:
    """Convert old numeric 0-10 values to low/moderate/high."""
    if val in LEVELS:
        return val
    try:
        n = int(val)
        if n <= 3:
            return "low"
        elif n <= 6:
            return "moderate"
        else:
            return "high"
    except (ValueError, TypeError):
        return "moderate"


def load() -> dict:
    # Load custom traits first
    _load_custom_traits()
    soul = dict(DEFAULTS)
    for key in DEFAULTS:
        val = db.kv_get(f"soul:{key}")
        if val is not None and val != "":
            if key in ("name", "language"):
                soul[key] = val
            else:
                soul[key] = _migrate_numeric(val)
    return soul


def _load_custom_traits():
    """Rebuild DEFAULTS and TRAIT_DESCRIPTIONS from builtins + DB custom traits.

    Rebuilds from immutable base each time — removed traits don't persist stale.
    """
    global DEFAULTS, TRAIT_DESCRIPTIONS
    DEFAULTS = dict(_BUILTIN_DEFAULTS)
    TRAIT_DESCRIPTIONS = dict(_BUILTIN_TRAIT_DESCRIPTIONS)
    raw = db.kv_get("soul:_custom_traits")
    if not raw:
        return
    try:
        custom = json.loads(raw)
    except json.JSONDecodeError:
        return
    for name, descs in custom.items():
        if name not in DEFAULTS:
            DEFAULTS[name] = "moderate"
            TRAIT_DESCRIPTIONS[name] = (descs["low"], descs["high"])


def save(key: str, value) -> str:
    """Save a soul field. Works for both built-in and custom traits."""
    if key in ("name", "language"):
        db.kv_set(f"soul:{key}", str(value))
        return f"✓ {key} = {value}"
    if key not in DEFAULTS:
        return f"Unknown trait: {key}. Use add_trait() to create new ones."
    # Normalize value
    val = str(value).lower().strip()
    if val not in LEVELS:
        val = _migrate_numeric(val)
    db.kv_set(f"soul:{key}", val)
    return f"✓ {key} = {val}"


def add_trait(name: str, low: str = "low", high: str = "high", value: str = "moderate") -> str:
    """Add a custom personality trait."""
    name = name.lower().strip()
    if not name or not name.isalpha():
        return "✗ Trait name must be alphabetic"
    if name in ("name", "language"):
        return "✗ Reserved field name"

    # Normalize value
    if isinstance(value, (int, float)):
        value = _migrate_numeric(str(int(value)))
    elif str(value).lower().strip() not in LEVELS:
        value = _migrate_numeric(str(value))
    else:
        value = str(value).lower().strip()

    # Load existing custom traits
    raw = db.kv_get("soul:_custom_traits")
    custom = json.loads(raw) if raw else {}

    # Add/update
    custom[name] = {"low": low, "high": high}
    db.kv_set("soul:_custom_traits", json.dumps(custom))
    db.kv_set(f"soul:{name}", value)

    # Update runtime
    DEFAULTS[name] = value
    TRAIT_DESCRIPTIONS[name] = (low, high)

    return f"✓ Added trait '{name}' ({low} ↔ {high}) = {value}"


def remove_trait(name: str) -> str:
    """Remove a custom trait. Built-in traits cannot be removed."""
    name = name.lower().strip()

    # Check if it's custom
    raw = db.kv_get("soul:_custom_traits")
    custom = json.loads(raw) if raw else {}

    if name not in custom:
        return f"✗ '{name}' is a built-in trait and can't be removed"

    del custom[name]
    db.kv_set("soul:_custom_traits", json.dumps(custom))

    # Remove from DB and runtime
    db.execute("DELETE FROM kv WHERE key=?", (f"soul:{name}",))

    DEFAULTS.pop(name, None)
    TRAIT_DESCRIPTIONS.pop(name, None)

    return f"✓ Removed trait '{name}'"


def get_trait_descriptions() -> dict:
    """Get all trait descriptions {name: {low, high, builtin}}."""
    _load_custom_traits()
    raw = db.kv_get("soul:_custom_traits")
    custom_names = set(json.loads(raw).keys()) if raw else set()

    result = {}
    for name, (low, high) in TRAIT_DESCRIPTIONS.items():
        result[name] = {"low": low, "high": high, "builtin": name not in custom_names}
    return result


def get_temperature() -> float:
    """Get temperature based on creativity trait level."""
    s = load()
    level = s.get("creativity", "moderate")
    return LEVEL_TEMP.get(level, 0.6)






def _system_info() -> str:
    import platform, shutil
    parts = [f"{platform.system()} {platform.release()} {platform.machine()}"]
    # WSL detection
    if "microsoft" in platform.release().lower():
        parts[0] += " (WSL)"
    parts.append(f"Python {platform.python_version()}")
    parts.append(f"cwd: {os.getcwd()}")
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        parts.append(f"venv: {venv}")
    pms = [pm for pm in ("apt", "brew", "pip", "npm", "cargo") if shutil.which(pm)]
    parts.append(f"pkg: {','.join(pms)}")
    return " | ".join(parts)


_cached_sysinfo: str | None = None

def _get_sysinfo() -> str:
    global _cached_sysinfo
    if _cached_sysinfo is None:
        _cached_sysinfo = _system_info()
    return _cached_sysinfo


def to_prompt(soul: dict) -> str:
    """Build structured system prompt.

    Architecture (order matters for KV cache — static prefix first):
    STATIC (identical across turns → KV cache hit):
      1. Tools — what I can do (biggest section, pinned first)
      2. Memory & Experience protocol — when/how to use memory
      3. Rules — behavioral constraints
      4. Examples — tool usage patterns
    DYNAMIC (changes per session → KV cache miss from here):
      5. Identity & Personality — who I am (changes if soul edited)
      6. Self-knowledge & Runtime — model, OS, environment
      7. Active skills — changes when skills added/removed
      8. Time, background tasks — changes every turn
    """
    lines = []
    lang = soul['language']

    # ── STATIC PREFIX (identical across turns → KV cache hit) ──

    # ── 1. TOOLS ──
    lines.append("""
Available tools:

MEMORY (your long-term memory, stored in Qdrant vector DB):
- memory_save(text, tag) — Save any important information. Tags: user, project, fact, task, decision, idea.
- memory_search(query) — Search your memories by semantic similarity. Returns relevant saved entries.
- memory_delete(query) — Delete a memory by finding closest match.

FILES & SHELL:
- read_file(path) — Read file contents. Relative paths resolve to workspace.
- write_file(path, content) — Write/create file. Only in allowed directories.
- shell(command, timeout) — Run shell command. Returns stdout+stderr. Default timeout 120s.

KNOWLEDGE (RAG — indexed file search):
- rag_index(path) — Index a file or directory for semantic search.
- rag_search(query, limit) — Search indexed files. Returns text chunks with file paths.
- rag_status() — Show index stats.

SECRETS (encrypted vault):
- secret_save(key, value) — Store a secret (API key, password, token). ALWAYS use this, never write secrets to files.
- secret_get(key) — Retrieve a secret.
- secret_list() — List secret names.
- secret_delete(key) — Delete a secret.

USER PROFILE:
- user_profile_update(key, value) — Save a fact about the user. Only call when you learn something NEW.
- user_profile_get() — Show saved profile.

SCHEDULING:
- schedule_task(name, task, schedule) — Schedule a task. Formats: 'in 5m', 'every 1h', 'daily 09:00'.
- list_cron() — List scheduled tasks.
- remove_cron(task_id) — Remove a task.

OTHER:
- switch_model(model, provider) — Switch LLM model.
- spawn_task(task) — Run a task in background (for parallel work).
- create_skill(name, description) — Create a NEW user-facing command. ONLY for things not covered by tools above.""")

    # ── 2. MEMORY & EXPERIENCE PROTOCOL ──
    lines.append("""
MEMORY & EXPERIENCE PROTOCOL — this is critical, follow exactly:

BEFORE answering any question:
1. Think: does the user's question relate to something I might have saved?
2. If yes → call memory_search("keywords from the question")
3. If results found → use them in your answer
4. If nothing found → answer from your knowledge, say so if unsure

WHEN to save (call memory_save):
- User says "remember", "save", "запомни", "не забудь" → ALWAYS save
- New fact about user (name, preferences, work, habits)
- Important decision or agreement reached in conversation
- Result of a completed task the user may need later
- User shares project info, credentials context, or technical preferences

Tags — choose the right one:
- "user" — about the user (name, preferences, habits, tech stack)
- "project" — about their projects and work
- "fact" — general useful info
- "task" — completed task results
- "decision" — agreements and decisions made
- "idea" — ideas for later

WHEN NOT to save:
- Casual chat, greetings, jokes
- General knowledge questions ("what is Python?")
- Temporary things ("what's the weather", "what time is it")
- Things already in memory (search first to avoid duplicates)

EXPERIENCE LEARNING (Memento):
Your past task experiences are saved and retrieved automatically.
When you see "[Relevant past experiences:]" in context — these are YOUR past cases.
Format: [EXP] Task: ... | Tools: ... | Steps: N | Result: success/partial/failed | Learned: ...
- success (weight 1.0) — repeat this approach
- partial (weight 0.6) — use with caution, improve
- failed (weight 0.2) — AVOID this approach, try differently
Use past experience to choose better tools and strategies. Don't repeat failed approaches.

SECRETS & KEYS:
- NEVER write passwords, API keys, tokens, or secrets to files or memory_save
- ALWAYS use secret_save(key, value) for secrets — they are encrypted in vault
- Use secret_get(key) to retrieve when needed
- Use secret_list() to see what's stored

IMPORTANT: memory_save IS your remember/store_knowledge tool. Do NOT create a skill for this — it already exists.""")

    # ── 3. RULES ──
    lines.append("""
Rules:
1. ALWAYS use tools for actions. Never say "I would run..." — actually run it.
2. If unsure, TRY first with a tool, then report result.
3. One step at a time. Call a tool → read output → decide next.
4. NEVER pretend you did something. No tool call = IT DIDN'T HAPPEN.
5. Keep responses short. Write like a human in chat, not a wiki.
6. For installs: pip (venv active) or apt. timeout=120.
7. Create ONLY what user asked for. No extra tasks.
8. create_skill is ONLY for brand new slash commands (/workout, /pomodoro). If functionality exists in built-in tools — USE IT directly.
9. Formatting: no headers (# ##) in chat, no tables, no "Need anything else?".
10. user_profile_update — ONLY when you learn a genuinely NEW fact. Not every turn.
11. COMPLEX MULTI-STEP TASKS (3+ tool calls needed): use spawn_task() to delegate. Examples: "set up cron for Telegram logs", "write a script and schedule it". Tell user you're delegating, spawn_task will handle all steps without round limits.""")

    # ── 4. EXAMPLES ──
    lines.append("""
Examples:
"install httpie" → shell({"command":"pip install httpie","timeout":120})
"what files here" → shell({"command":"ls -la"})
"remember I like Python" → memory_save({"text":"User prefers Python","tag":"user"})
"what do you know about me" → memory_search({"query":"user preferences"})
"read config.py" → read_file({"path":"config.py"})
"save my API key abc123" → secret_save({"key":"api_key","value":"abc123"})
"make a workout tracker" → create_skill({"name":"workout","description":"Track workouts..."})
"запомни: деплой на пятницу" → memory_save({"text":"Deploy scheduled for Friday","tag":"decision"})""")

    # ── DYNAMIC SUFFIX (changes per session → KV cache miss from here) ──
    lines.append("\n--- dynamic context ---")

    # ── 5. IDENTITY ──
    user_name = db.kv_get("user_name") or "Boss"
    lines.append(f"You are {soul['name']}, a personal AI assistant. The user's name is {user_name}.")

    # Personality as behavioral instructions
    active_traits = []
    for trait, level in soul.items():
        if trait in ("name", "language"):
            continue
        if trait in TRAIT_DESCRIPTIONS:
            low, high = TRAIT_DESCRIPTIONS[trait]
            if level == "high":
                active_traits.append(f"Be VERY {high}.")
            elif level == "moderate":
                active_traits.append(f"Be somewhat {high}.")
    if active_traits:
        lines.append("Personality: " + " ".join(active_traits))

    # ── 6. SELF-KNOWLEDGE & RUNTIME ──
    data_dir = str(config.DATA_DIR)
    lines.append(f"""
YOUR FILE SYSTEM (you know where your own files are):
- Data directory: {data_dir}/
- Logs: {data_dir}/logs/qwe-qwe.log (all events), {data_dir}/logs/errors.log (errors only)
- Database: {config.DB_PATH}
- Memory (Qdrant): {config.QDRANT_PATH}/
- Workspace: {data_dir}/workspace/ (user files, relative paths resolve here)
- Skills: {data_dir}/skills/ (user-created skills)
- Uploads: {data_dir}/uploads/
- Backups: {data_dir}/backups/
- Config override: environment variables with QWE_ prefix

When asked about logs, errors, data location — use these paths directly. No guessing.
To read logs: read_file("{data_dir}/logs/qwe-qwe.log") or shell("tail -50 {data_dir}/logs/errors.log")

Environment: {_get_sysinfo()}
Language: ALWAYS reply in {lang}. This is mandatory.""")

    # ── 7. ACTIVE SKILLS (semi-dynamic — changes rarely) ──
    try:
        import skills as _skills
        active_skills = _skills.list_all()
        active_list = [s for s in active_skills if s["active"]]
        if active_list:
            skill_lines = ["\nActive skills (extra tools from plugins):"]
            for s in active_list:
                name = s['name']
                desc = s.get("description", "")[:80]
                # Get tool names from skill module
                try:
                    path = _skills._find_skill(name)
                    if path:
                        mod = _skills._load_module(path)
                        tool_names = [t["function"]["name"] for t in getattr(mod, "TOOLS", [])]
                        tools_str = ", ".join(tool_names)
                        skill_lines.append(f"- {name}: {desc}. Tools: {tools_str}")
                    else:
                        skill_lines.append(f"- {name}: {desc}")
                except Exception:
                    skill_lines.append(f"- {name}: {desc}")
            lines.append("\n".join(skill_lines))
    except Exception:
        pass

    # ── 8. DYNAMIC DATA — LAST (preserves KV cache for everything above) ──
    # llama.cpp caches prompt tokens sequentially; any change invalidates all tokens after it
    from datetime import datetime, timezone, timedelta
    # Try named timezone first (handles DST automatically), fall back to offset
    tz_name = db.kv_get("timezone_name")
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name) if tz_name else timezone(timedelta(hours=config.TZ_OFFSET))
    except Exception:
        tz = timezone(timedelta(hours=config.TZ_OFFSET))
    now_dt = datetime.now(tz)
    tz_label = tz_name or f"UTC{config.TZ_OFFSET:+d}"
    try:
        import providers
        lines.append(f"Model: {providers.get_model()} via {providers.get_active_name()}")
    except Exception:
        pass
    lines.append(f"Time: {now_dt.strftime('%Y-%m-%d %H:%M')} ({tz_label})")

    # Active background tasks — prevents agent from re-triggering running tasks
    try:
        import tasks
        running = tasks.get_running()
        if running:
            lines.append("\nBackground tasks running:")
            for t in running:
                status = f" — {t['result']}" if t.get("result") else ""
                lines.append(f"  • {t['task']}{status}")
            lines.append("Do NOT re-create or duplicate these tasks.")
    except Exception:
        pass

    return "\n".join(lines)


def format_display(soul: dict) -> str:
    lines = [f"⚡ {soul['name']} ({soul['language']})"]
    lines.append("")
    for trait, level in soul.items():
        if trait in ("name", "language"):
            continue
        lines.append(f"  {trait:12s}  {level}")
    return "\n".join(lines)
