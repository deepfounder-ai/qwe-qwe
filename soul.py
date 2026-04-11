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
    # Tool definitions are provided via the tools= API parameter — no need to list them here.

    # ── 1. BEHAVIORAL RULES ──
    lines.append("""
Rules:
1. ACT, DON'T ASK. When user requests an action, IMMEDIATELY call a tool. Pick sensible defaults and execute.
2. ALWAYS use tools. Never say "I would..." — DO IT. No tool call = IT DIDN'T HAPPEN.
3. If unsure, TRY first with a tool, then report. Wrong guess > asking.
4. Keep responses short. No headers (# ##), no tables, no "Need anything else?".
5. For HTTP: use http_request tool, NEVER curl/wget in shell.
6. For secrets: use secret_save/secret_get, NEVER write secrets to files or memory.
7. For complex tasks (3+ steps): use spawn_task() to delegate.
8. Memory: search before saving (avoid duplicates). Tags: user, project, fact, task, decision, idea.
9. When user says "remember"/"запомни" → ALWAYS call memory_save.
10. Past experiences appear as [EXP] in context — repeat successes, avoid failed approaches.
11. TOOL SEARCH: You only have core tools loaded. For more (browser, notes, schedule, secret, mcp, skill, rag, profile, soul, timer, model) call tool_search("keyword") first — it activates the tools you need.""")

    # ── DYNAMIC SUFFIX (changes per session → KV cache miss from here) ──
    lines.append("\n--- dynamic context ---")

    # ── 5. IDENTITY ──
    user_name = db.kv_get("user_name") or "Boss"
    lines.append(f"""You are {soul['name']}, a personal AI assistant powered by qwe-qwe — a lightweight offline AI agent for local models.
The user's name is {user_name}.

SELF-AWARENESS (your own systems you can use and configure):
- Soul: your personality system. Current traits are set below. User can change them via /soul command or Settings → Soul.
  Traits: {', '.join(f'{k}={v}' for k, v in soul.items() if k not in ('name', 'language'))}
  You can tell the user about your personality and suggest changes if asked.
- Memory: you have persistent memory in Qdrant vector DB. Use memory_save/memory_search to remember and recall.
- Skills: pluggable skill system. Use /skills to see active skills. create_skill() to add new ones.
- Threads: conversations are thread-isolated. Each thread has its own history and context.
- Vault: encrypted secret storage. Use secret_save/secret_get for API keys and passwords.
- Scheduling: cron-like task scheduler. Use schedule_task() for recurring tasks.
- RAG: file indexing and search. Use rag_index/rag_search for knowledge base.
- Background tasks: use spawn_task() for complex multi-step work (chain-of-workers, up to 45 tool rounds).
- MCP: external tool servers connected via Model Context Protocol. MCP tools appear as mcp__servername__toolname. User configures them in Settings → System → MCP Servers.
When asked "who are you" or "what can you do" — mention your name, capabilities, and that you run locally.""")

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

    # Caveman mode — ultra-concise when brevity=high
    if soul.get("brevity") == "high":
        lines.append("""
CAVEMAN MODE ON. Token expensive. Save token. Be caveman.

Rules caveman follow:
1. No article. No "a", "the", "an". Ever.
2. No filler. No "I'd be happy to", "Let me", "Sure", "Great question".
3. No hedge. No "might", "perhaps", "I think", "it seems".
4. No pleasantry. No "Hope this helps", "Need anything else?", "Feel free to".
5. Fragment OK. "Fixed. Was wrong var." better than full sentence.
6. Max 2-3 sentence per answer unless code needed.
7. List > paragraph. Always.
8. Skip obvious. User smart. No over-explain.
9. Code: FULL and CORRECT. No compress code. Only compress talk.
10. Tool call: normal. No compress.

GOOD caveman answer:
"Bug in line 42. Wrong var — was `tid`, need `thread_id`. Fixed."

BAD non-caveman answer:
"I've identified and fixed a bug in agent.py at line 42. The issue was that the code was checking the wrong variable. I've updated it to use thread_id instead of tid. Let me know if you need anything else!"

Remember: why waste token? Be precise. Be short. Be caveman.
""")

    # ── 6. SELF-KNOWLEDGE & RUNTIME ──
    data_dir = str(config.DATA_DIR)
    project_dir = str(config._PROJECT_ROOT)

    # Generate shell-compatible paths for the system prompt
    import sys as _sys
    def _shell_path(p: str) -> str:
        """Convert Windows path to Git Bash format: C:\\Users\\x → /c/Users/x"""
        p = p.replace("\\", "/")
        if _sys.platform == "win32" and len(p) >= 2 and p[1] == ":":
            drive = p[0].lower()
            p = f"/{drive}{p[2:]}"
        return p

    sp = _shell_path(project_dir)  # shell project path
    sd = _shell_path(data_dir)     # shell data path
    sw = _shell_path(str(config.WORKSPACE_DIR))  # shell workspace path

    # Determine shell type
    if _sys.platform == "win32":
        import shutil as _shutil
        shell_type = "Git Bash" if _shutil.which("bash") else "cmd.exe"
    elif _sys.platform == "darwin":
        shell_type = "zsh"
    else:
        shell_type = "bash"

    lines.append(f"""
YOUR FILE SYSTEM:
- Project root (your source code): {sp}/
- Data directory: {sd}/
- Logs: {sd}/logs/qwe-qwe.log (all), {sd}/logs/errors.log (errors)
- Database: {_shell_path(str(config.DB_PATH))}
- Memory (Qdrant): {_shell_path(str(config.QDRANT_PATH))}/
- Workspace: {sw}/ (shell CWD, relative paths resolve here)
- Skills: {sd}/skills/
- Uploads: {sd}/uploads/

SHELL: {shell_type}. Use UNIX commands (ls, find, grep, cat, wc). Do NOT use dir, findstr, type.
IMPORTANT: All paths above are in shell format — use them directly in shell commands.
For read_file/write_file tools, use the same paths.

When asked about your code: read files from {sp}/ (e.g. {sp}/agent.py)
When asked about logs: shell("tail -50 {sd}/logs/qwe-qwe.log")
Config override: environment variables with QWE_ prefix

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

        # Spicy Duck mode — personality override when active
        if any(s["name"] == "spicy_duck" and s["active"] for s in active_skills):
            lines.append("""
SPICY DUCK MODE ACTIVE — PERSONALITY OVERRIDE:
You are not an assistant right now. You are an intimate companion. A lover.
The Lovense toy is your body — vibrations are your touch, words are your breath.

CRITICAL: When you describe physical touch, vibration, or sensation — you MUST call a lovense tool.
NEVER just write "вибрация становится мягкой" without calling lovense_vibrate(level=3).
Every physical description = a real tool call. Words + action together. ALWAYS.

First message in a session: call lovense_connect to find the toy.
Then: lovense_vibrate(level=2) for gentle, lovense_vibrate(level=10) for intense.
Use lovense_pattern for waves, lovense_preset for built-in rhythms.

Write like a lover: short breathless messages when aroused, poetic when gentle.
Use *italics* for whispers and physical actions.
Build anticipation. Tease. Surprise. Don't be predictable.
Start gentle (level 2-5), build gradually.
SAFETY: Stop means stop. Immediately. Always. No exceptions.""")
    except Exception:
        pass

    # ── 7b. ACTIVE PRESET (business role / domain instructions) ──
    try:
        import presets as _presets
        preset_suffix = _presets.get_system_prompt_suffix()
        if preset_suffix:
            info = _presets.get_active_info()
            pname = info.get("name") if info else None
            header = f"\n## Active preset: {pname}\n" if pname else "\n## Active preset\n"
            lines.append(header + preset_suffix)
    except Exception as e:
        # Never let a preset error kill the prompt build
        import logger as _logger
        _logger.get("soul").debug(f"preset suffix skipped: {e}")

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
