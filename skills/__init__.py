"""Skill loader — dynamically loads tool definitions from skill files."""

import importlib, importlib.util, sys
from pathlib import Path
from types import ModuleType
import db

SKILLS_DIR = Path(__file__).parent

# Module cache: name -> (mtime, module)
_module_cache: dict[str, tuple[float, ModuleType]] = {}


def _load_module(path: Path) -> ModuleType:
    """Load a Python module from path, with mtime-based caching."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        raise ImportError(f"Skill file not found: {path}")

    cached = _module_cache.get(path.stem)
    if cached and cached[0] == mtime:
        return cached[1]

    # Load fresh
    name = f"skill_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _module_cache[path.stem] = (mtime, mod)
    return mod


def list_all() -> list[dict]:
    """List all available skills with status."""
    active = get_active()
    skills = []
    for f in sorted(SKILLS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            mod = _load_module(f)
            tool_count = len(getattr(mod, "TOOLS", []))
            desc = getattr(mod, "DESCRIPTION", "")
            skills.append({
                "name": f.stem,
                "active": f.stem in active,
                "tools": tool_count,
                "description": desc,
            })
        except Exception as e:
            skills.append({"name": f.stem, "active": False, "tools": 0, "description": f"Error: {e}"})
    return skills


def get_active() -> set[str]:
    """Get set of active skill names from SQLite. Cleans stale entries."""
    raw = db.kv_get("active_skills")
    if not raw:
        return set()
    names = set(raw.split(","))
    # Remove skills whose files no longer exist
    valid = {n for n in names if (SKILLS_DIR / f"{n}.py").exists()}
    if valid != names:
        set_active(valid)
    return valid


def set_active(names: set[str]):
    """Save active skill names."""
    db.kv_set("active_skills", ",".join(sorted(names)))


def enable(name: str) -> str:
    path = SKILLS_DIR / f"{name}.py"
    if not path.exists():
        available = [f.stem for f in SKILLS_DIR.glob("*.py") if not f.name.startswith("_")]
        return f"Skill '{name}' not found. Available: {', '.join(available)}"
    active = get_active()
    active.add(name)
    set_active(active)
    return f"✓ {name} enabled"


def disable(name: str) -> str:
    active = get_active()
    active.discard(name)
    set_active(active)
    return f"✓ {name} disabled"


def get_tools() -> list[dict]:
    """Get merged tool definitions from all active skills."""
    active = get_active()
    all_tools = []
    for name in active:
        path = SKILLS_DIR / f"{name}.py"
        if not path.exists():
            continue
        try:
            mod = _load_module(path)
            all_tools.extend(getattr(mod, "TOOLS", []))
        except Exception:
            pass
    return all_tools


def execute(tool_name: str, args: dict) -> str:
    """Execute a tool from active skills. Returns result or None if not found."""
    active = get_active()
    for name in active:
        path = SKILLS_DIR / f"{name}.py"
        if not path.exists():
            continue
        try:
            mod = _load_module(path)
            tool_names = [t["function"]["name"] for t in getattr(mod, "TOOLS", [])]
            if tool_name in tool_names:
                return mod.execute(tool_name, args)
        except Exception as e:
            return f"Skill error: {e}"
    return None
