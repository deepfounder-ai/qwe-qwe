"""Soul — agent personality as compact numeric config."""

from pathlib import Path
import db

# Default personality template
DEFAULTS = {
    "name": "Agent",
    "language": "English",
    "humor": 5,
    "honesty": 8,
    "curiosity": 6,
    "brevity": 7,        # 10 = max concise, 0 = verbose
    "formality": 3,      # 10 = formal, 0 = casual
    "proactivity": 5,    # 10 = suggests things, 0 = only answers
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
    """Load soul from SQLite, merging with defaults."""
    soul = dict(DEFAULTS)
    for key in DEFAULTS:
        val = db.kv_get(f"soul:{key}")
        if val is not None:
            soul[key] = int(val) if val.isdigit() else val
    return soul


def save(key: str, value) -> str:
    """Save a soul trait."""
    if key not in DEFAULTS:
        return f"Unknown trait: {key}. Available: {', '.join(DEFAULTS.keys())}"
    db.kv_set(f"soul:{key}", str(value))
    return f"✓ {key} = {value}"


def _system_info() -> str:
    """Detect system info once."""
    import platform, os, shutil
    parts = [f"OS: {platform.system()} {platform.release()}"]
    parts.append(f"Arch: {platform.machine()}")
    parts.append(f"Python: {platform.python_version()}")
    parts.append(f"Shell: {os.environ.get('SHELL', 'unknown')}")
    parts.append(f"Home: {Path.home()}")
    parts.append(f"CWD: {os.getcwd()}")
    # Package managers
    pms = []
    for pm in ("apt", "brew", "dnf", "pacman", "pip", "npm"):
        if shutil.which(pm):
            pms.append(pm)
    parts.append(f"Package managers: {', '.join(pms)}")
    # WSL detection
    if "microsoft" in platform.release().lower() or "wsl" in platform.release().lower():
        parts.append("Environment: WSL (Windows Subsystem for Linux)")
    return " | ".join(parts)


_cached_sysinfo: str | None = None

def _get_sysinfo() -> str:
    global _cached_sysinfo
    if _cached_sysinfo is None:
        _cached_sysinfo = _system_info()
    return _cached_sysinfo


def to_prompt(soul: dict) -> str:
    """Convert soul config to a compact system prompt."""
    lines = [f"You are {soul['name']}. Language: {soul['language']}."]
    lines.append("Personality traits (scale 0-10):")

    for trait, value in soul.items():
        if trait in ("name", "language"):
            continue
        if trait in TRAIT_DESCRIPTIONS:
            low, high = TRAIT_DESCRIPTIONS[trait]
            if value >= 7:
                lines.append(f"- {trait}={value}: {high}")
            elif value <= 3:
                lines.append(f"- {trait}={value}: {low}")
            # 4-6 = neutral, skip to save tokens

    lines.append("")
    lines.append(f"System: {_get_sysinfo()}")
    lines.append("")
    lines.append("You have access to tools. ALWAYS use them for actions — never guess or assume.")
    lines.append("If asked to install, run, open, or do something — execute it with shell/tools first, report result after.")
    lines.append("Don't say 'that URL might be wrong' — try it and see. Action over speculation.")

    return "\n".join(lines)


def format_display(soul: dict) -> str:
    """Format soul for CLI display."""
    lines = [f"⚡ {soul['name']} ({soul['language']})"]
    lines.append("")
    for trait, value in soul.items():
        if trait in ("name", "language"):
            continue
        bar = "█" * value + "░" * (10 - value)
        lines.append(f"  {trait:12s} [{bar}] {value}/10")
    return "\n".join(lines)
