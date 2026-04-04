"""Skill loader — dynamically loads tool definitions from skill files.

Skills are loaded from two directories:
  1. Built-in skills: shipped with the project (skills/ in repo)
  2. User skills: ~/.qwe-qwe/skills/ (safe from git updates)
"""

import importlib, importlib.util, sys
from pathlib import Path
from types import ModuleType
import db
import config

BUILTIN_SKILLS_DIR = Path(__file__).parent
USER_SKILLS_DIR = config.USER_SKILLS_DIR
# For backward compat — code that references SKILLS_DIR
SKILLS_DIR = BUILTIN_SKILLS_DIR


def _all_skill_paths() -> dict[str, Path]:
    """Return {name: path} for all skills. User skills override built-in."""
    skills = {}
    for d in (BUILTIN_SKILLS_DIR, USER_SKILLS_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            skills[f.stem] = f
    return skills


def _find_skill(name: str) -> Path | None:
    """Find a skill file by name, checking user dir first."""
    user_path = USER_SKILLS_DIR / f"{name}.py"
    if user_path.exists():
        return user_path
    builtin_path = BUILTIN_SKILLS_DIR / f"{name}.py"
    if builtin_path.exists():
        return builtin_path
    return None

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
    for name, path in sorted(_all_skill_paths().items()):
        try:
            mod = _load_module(path)
            tool_count = len(getattr(mod, "TOOLS", []))
            desc = getattr(mod, "DESCRIPTION", "")
            is_user = path.parent == USER_SKILLS_DIR
            skills.append({
                "name": name,
                "active": name in active,
                "tools": tool_count,
                "description": desc,
                "user_skill": is_user,
            })
        except Exception as e:
            skills.append({"name": name, "active": False, "tools": 0, "description": f"Error: {e}"})
    return skills


_DEFAULT_SKILLS = {"mcp_manager", "soul_editor", "skill_creator", "browser"}  # always-on built-in skills


def get_active() -> set[str]:
    """Get set of active skill names from SQLite. Default skills always included."""
    raw = db.kv_get("active_skills")
    if not raw:
        return set(_DEFAULT_SKILLS)
    names = set(raw.split(","))
    all_paths = _all_skill_paths()
    valid = {n for n in names if n in all_paths}
    # Always include default skills
    valid |= {n for n in _DEFAULT_SKILLS if n in all_paths}
    if valid != names:
        set_active(valid)
    return valid


def set_active(names: set[str]):
    """Save active skill names."""
    db.kv_set("active_skills", ",".join(sorted(names)))


def enable(name: str) -> str:
    path = _find_skill(name)
    if not path:
        available = sorted(_all_skill_paths().keys())
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


def _compact_tool(tool: dict) -> dict:
    """Return a copy of tool definition with truncated description (≤80 chars)."""
    import copy
    t = copy.deepcopy(tool)
    desc = t["function"].get("description", "")
    # Truncate to first sentence or 80 chars
    dot = desc.find(". ")
    if 0 < dot <= 80:
        t["function"]["description"] = desc[:dot + 1]
    elif len(desc) > 80:
        t["function"]["description"] = desc[:77] + "..."
    return t


def get_tools(compact: bool = False) -> list[dict]:
    """Get merged tool definitions from all active skills.

    If compact=True, truncate descriptions to save tokens in system prompt.
    """
    active = get_active()
    all_tools = []
    for name in active:
        path = _find_skill(name)
        if not path:
            continue
        try:
            mod = _load_module(path)
            skill_tools = getattr(mod, "TOOLS", [])
            if compact:
                skill_tools = [_compact_tool(t) for t in skill_tools]
            all_tools.extend(skill_tools)
        except Exception:
            pass
    return all_tools


def get_instruction(tool_name: str) -> str | None:
    """Get the INSTRUCTION text from the skill that owns this tool.

    Returns None if the skill has no INSTRUCTION attribute.
    """
    active = get_active()
    for name in active:
        path = _find_skill(name)
        if not path:
            continue
        try:
            mod = _load_module(path)
            tool_names = [t["function"]["name"] for t in getattr(mod, "TOOLS", [])]
            if tool_name in tool_names:
                return getattr(mod, "INSTRUCTION", None)
        except Exception:
            pass
    return None


def validate_skill(skill_path: str) -> tuple[bool, list[str]]:
    """Validate a skill file for correctness before use.

    Checks: syntax, required attributes, execute() signature, db API usage.
    Returns (is_valid, errors_list).
    """
    import ast, inspect

    errors = []
    path = Path(skill_path)

    if not path.exists():
        return False, [f"File not found: {skill_path}"]

    # 1. Syntax check
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, [f"Syntax error: {e}"]

    # 2. Required attributes: DESCRIPTION, TOOLS, execute()
    top_names = {node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 else node.targets[0].id if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name)
                 else None
                 for node in ast.iter_child_nodes(tree)}
    top_names.discard(None)

    for attr in ("DESCRIPTION", "TOOLS"):
        if attr not in top_names:
            errors.append(f"Missing required attribute: {attr}")

    if "execute" not in top_names:
        errors.append("Missing required function: execute()")

    # 3. Check execute() signature: must accept (name, args)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            arg_names = [a.arg for a in node.args.args]
            if len(arg_names) < 2:
                errors.append(f"execute() must accept (name, args), got ({', '.join(arg_names)})")
            break

    # 4. Check db API usage — only allowed methods
    _DB_WHITELIST = {
        "_get_conn", "kv_get", "kv_set", "kv_get_prefix", "kv_inc",
        "save_message", "get_recent_messages",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "db" and node.attr not in _DB_WHITELIST:
                errors.append(f"Forbidden db method: db.{node.attr}() — use only: {', '.join(sorted(_DB_WHITELIST))}")

    # 5. Try importing (catches runtime import errors)
    if not errors:
        try:
            mod_name = f"_validate_{path.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Verify TOOLS is a list of dicts with function.name
            skill_tools = getattr(mod, "TOOLS", None)
            if not isinstance(skill_tools, list):
                errors.append("TOOLS must be a list")
            elif skill_tools:
                for i, t in enumerate(skill_tools):
                    fn = t.get("function", {}) if isinstance(t, dict) else {}
                    if not fn.get("name"):
                        errors.append(f"TOOLS[{i}] missing function.name")
        except Exception as e:
            errors.append(f"Import failed: {e}")

    return (len(errors) == 0, errors)


# Common hallucinated tool names → redirect to real skill
_TOOL_ALIASES = {
    "google_search": "browser",
    "open_url": "browser",
    "navigate": "browser",
    "browse": "browser",
    "extract_content": "browser",
    "get_page_content": "browser",
    "read_page": "browser",
    "take_screenshot": "browser",
    "capture_screenshot": "browser",
}


def execute(tool_name: str, args: dict) -> str:
    """Execute a tool from active skills. Returns result or None if not found."""
    active = get_active()
    for name in active:
        path = _find_skill(name)
        if not path:
            continue
        try:
            mod = _load_module(path)
            tool_names = [t["function"]["name"] for t in getattr(mod, "TOOLS", [])]
            if tool_name in tool_names:
                return mod.execute(tool_name, args)
        except Exception as e:
            return f"Skill error: {e}"

    # Fallback: check if hallucinated tool name has an alias to a real skill
    alias_skill = _TOOL_ALIASES.get(tool_name)
    if alias_skill and alias_skill in active:
        path = _find_skill(alias_skill)
        if path:
            try:
                mod = _load_module(path)
                return mod.execute(tool_name, args)
            except Exception as e:
                return f"Skill error: {e}"

    return None
