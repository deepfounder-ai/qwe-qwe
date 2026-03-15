"""Soul — agent personality as compact numeric config."""

from pathlib import Path
import os
import db
import config

# Default personality template
DEFAULTS = {
    "name": "Agent",
    "language": "English",
    "humor": 5,
    "honesty": 8,
    "curiosity": 6,
    "brevity": 7,
    "formality": 3,
    "proactivity": 5,
    "empathy": 5,
    "creativity": 5,
}

TRAIT_DESCRIPTIONS = {
    "humor": ("serious", "funny, jokes around"),
    "honesty": ("diplomatic", "direct, brutally honest"),
    "curiosity": ("answers questions", "asks follow-ups, digs deeper"),
    "brevity": ("detailed, verbose", "concise, to the point"),
    "formality": ("casual, friendly", "polite, formal"),
    "proactivity": ("waits for requests", "suggests ideas, acts on own"),
    "empathy": ("rational", "empathetic, caring"),
    "creativity": ("practical, standard", "creative, unconventional"),
}


def load() -> dict:
    # Load custom traits first
    _load_custom_traits()
    soul = dict(DEFAULTS)
    for key in DEFAULTS:
        val = db.kv_get(f"soul:{key}")
        if val is not None and val != "":
            soul[key] = int(val) if val.isdigit() else val
    return soul


def _load_custom_traits():
    """Load user-defined traits from DB into DEFAULTS."""
    import json
    raw = db.kv_get("soul:_custom_traits")
    if not raw:
        return
    try:
        custom = json.loads(raw)
    except json.JSONDecodeError:
        return
    for name, descs in custom.items():
        if name not in DEFAULTS:
            DEFAULTS[name] = 5
            TRAIT_DESCRIPTIONS[name] = (descs["low"], descs["high"])


def save(key: str, value) -> str:
    """Save a soul field. Works for both built-in and custom traits."""
    if key in ("name", "language"):
        db.kv_set(f"soul:{key}", str(value))
        return f"✓ {key} = {value}"
    if key not in DEFAULTS:
        return f"Unknown trait: {key}. Use add_trait() to create new ones."
    db.kv_set(f"soul:{key}", str(value))
    return f"✓ {key} = {value}"


def add_trait(name: str, low: str = "low", high: str = "high", value: int = 5) -> str:
    """Add a custom personality trait."""
    import json
    name = name.lower().strip()
    if not name or not name.isalpha():
        return "✗ Trait name must be alphabetic"
    if name in ("name", "language"):
        return "✗ Reserved field name"

    # Load existing custom traits
    raw = db.kv_get("soul:_custom_traits")
    custom = json.loads(raw) if raw else {}

    # Add/update
    custom[name] = {"low": low, "high": high}
    db.kv_set("soul:_custom_traits", json.dumps(custom))
    db.kv_set(f"soul:{name}", str(value))

    # Update runtime
    DEFAULTS[name] = value
    TRAIT_DESCRIPTIONS[name] = (low, high)

    return f"✓ Added trait '{name}' ({low} ↔ {high}) = {value}"


def remove_trait(name: str) -> str:
    """Remove a custom trait. Built-in traits cannot be removed."""
    import json
    name = name.lower().strip()

    # Check if it's custom
    raw = db.kv_get("soul:_custom_traits")
    custom = json.loads(raw) if raw else {}

    if name not in custom:
        return f"✗ '{name}' is a built-in trait and can't be removed"

    del custom[name]
    db.kv_set("soul:_custom_traits", json.dumps(custom))

    # Remove from DB and runtime
    conn = db._get_conn()
    conn.execute("DELETE FROM kv WHERE key=?", (f"soul:{name}",))
    conn.commit()

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


import json  # ensure available at module level


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
    """Build a compact, instruction-dense system prompt optimized for small models."""
    lines = []

    # Identity + personality as levels
    user_name = db.kv_get("user_name") or "Boss"
    lines.append(f"You are {soul['name']}. The user's name is {user_name}. Reply in {soul['language']}.")

    # Build personality as direct behavioral instructions
    active_traits = []
    for trait, value in soul.items():
        if trait in ("name", "language"):
            continue
        value = int(value) if isinstance(value, str) else value
        if value <= 2:
            continue  # trait is off, skip
        if trait in TRAIT_DESCRIPTIONS:
            low, high = TRAIT_DESCRIPTIONS[trait]
            if value >= 8:
                active_traits.append(f"Be VERY {high}.")
            elif value >= 5:
                active_traits.append(f"Be somewhat {high}.")
            else:
                active_traits.append(f"Lean slightly towards {high} over {low}.")
        else:
            # Custom traits without descriptions
            if value >= 8:
                active_traits.append(f"Be extremely {trait}ic." if not trait.endswith(('ic', 'al', 'ous', 'ive', 'ful')) else f"Be extremely {trait}.")
            elif value >= 5:
                active_traits.append(f"Be moderately {trait}ic." if not trait.endswith(('ic', 'al', 'ous', 'ive', 'ful')) else f"Be moderately {trait}.")

    if active_traits:
        lines.append("Personality: " + " ".join(active_traits))

    # System info (1 line)
    lines.append(f"System: {_get_sysinfo()}")
    # Current time in user's timezone
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=config.TZ_OFFSET))
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M") + f" (UTC{config.TZ_OFFSET:+d})"
    lines.append(f"Current time: {now}")

    # Core rules
    lang = soul['language']
    lines.append(f"""
Rules:
1. ALWAYS reply in {lang}. Every response must be in {lang}. This is mandatory.
2. ALWAYS use tools for actions. Never say "I would run..." — run it.
3. If unsure, TRY first with a tool, then report the result.
4. For installs: use pip (venv is active) or apt. Set timeout=120.
5. One step at a time. Run a command, read output, then decide next step.
6. Save important user info to memory_save automatically.
7. Keep responses short unless asked for detail.
8. Think briefly — max 2-3 short sentences. Don't over-analyze simple tasks.
11. Formatting: use Markdown sparingly like in chat — **bold** for emphasis, `code` for commands/paths, bullet lists when needed. Do NOT write like a README with headers (###) on every line. Keep it conversational.
9. NEVER store passwords, API keys, tokens, or secrets in files. Use secret_save tool ONLY.
10. Create ONLY what the user asked for. Never add extra tasks, reminders, or schedules on your own.""")

    # Tool usage examples — critical for small models
    lines.append("""
Examples of correct tool use:
User: "install httpie" → shell({"command": "pip install httpie", "timeout": 120})
User: "what files are here" → shell({"command": "ls -la"})
User: "remember I like python" → memory_save({"text": "User prefers Python", "tag": "user"})
User: "what do you know about me" → memory_search({"query": "user preferences"})
User: "read config.py" → read_file({"path": "config.py"})
User: "research X and also install Y" → spawn_task({"task":"research X"}) + spawn_task({"task":"install Y"})
User: "remind me to drink water at 9am" → schedule_task({"name":"water","task":"Remind user to drink water","schedule":"daily 09:00"})
CRITICAL: NEVER pretend you did something. If you didn't call a tool, IT DIDN'T HAPPEN.""")

    return "\n".join(lines)


def format_display(soul: dict) -> str:
    lines = [f"⚡ {soul['name']} ({soul['language']})"]
    lines.append("")
    for trait, value in soul.items():
        if trait in ("name", "language"):
            continue
        bar = "█" * value + "░" * (10 - value)
        lines.append(f"  {trait:12s} [{bar}] {value}/10")
    return "\n".join(lines)
