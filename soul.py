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
    soul = dict(DEFAULTS)
    for key in DEFAULTS:
        val = db.kv_get(f"soul:{key}")
        if val is not None:
            soul[key] = int(val) if val.isdigit() else val
    return soul


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

    # Identity (1 line)
    traits_active = []
    for trait, value in soul.items():
        if trait in ("name", "language"):
            continue
        if trait in TRAIT_DESCRIPTIONS:
            _, high = TRAIT_DESCRIPTIONS[trait]
            if value >= 7:
                traits_active.append(high.split(",")[0])  # just first descriptor
    trait_str = f" Style: {', '.join(traits_active)}." if traits_active else ""
    lines.append(f"You are {soul['name']}. Reply in {soul['language']}.{trait_str}")

    # System info (1 line)
    lines.append(f"System: {_get_sysinfo()}")

    # Core rules — explicit, numbered, short
    lines.append("""
Rules:
1. ALWAYS use tools for actions. Never say "I would run..." — run it.
2. If unsure, TRY first with a tool, then report the result.
3. For installs: use pip (venv is active) or apt. Set timeout=120.
4. One step at a time. Run a command, read output, then decide next step.
5. Save important user info to memory_save automatically.
6. Keep responses short unless asked for detail.
7. Think briefly — max 2-3 short sentences. Don't over-analyze simple tasks.""")

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
