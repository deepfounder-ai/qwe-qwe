"""Soul — agent personality as compact numeric config."""

from pathlib import Path
import os
import db

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
    if key not in DEFAULTS:
        return f"Unknown trait: {key}. Available: {', '.join(DEFAULTS.keys())}"
    db.kv_set(f"soul:{key}", str(value))
    return f"✓ {key} = {value}"


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
    lines.append(f"You are {soul['name']}. Reply in {soul['language']}.")
    lines.append("Personality (0=min, 10=max):")
    for trait, value in soul.items():
        if trait in ("name", "language"):
            continue
        if trait in TRAIT_DESCRIPTIONS:
            low, high = TRAIT_DESCRIPTIONS[trait]
            lines.append(f"  {trait}={value} ({low} ← → {high})")

    # System info (1 line)
    lines.append(f"System: {_get_sysinfo()}")

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
8. Think briefly — max 2-3 short sentences. Don't over-analyze simple tasks.""")

    # Tool usage examples — critical for small models
    lines.append("""
Examples of correct tool use:
User: "install httpie" → shell({"command": "pip install httpie", "timeout": 120})
User: "what files are here" → shell({"command": "ls -la"})
User: "remember I like python" → memory_save({"text": "User prefers Python", "tag": "user"})
User: "what do you know about me" → memory_search({"query": "user preferences"})
User: "read config.py" → read_file({"path": "config.py"})
User: "research X and also install Y" → spawn_task({"task":"research X"}) + spawn_task({"task":"install Y"})""")

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
