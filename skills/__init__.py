"""Skill loader — dynamically loads tool definitions from skill files.

Skills are loaded from two directories:
  1. Built-in skills: shipped with the project (skills/ in repo)
  2. User skills: ~/.castor/skills/ (safe from git updates)
"""

import hashlib
import importlib, importlib.util, json, re, sys
from pathlib import Path
from types import ModuleType
import db
import config
import logger

_log = logger.get("skills")

# Skill names: lowercase alphanumeric + underscores only, no path separators
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

BUILTIN_SKILLS_DIR = Path(__file__).parent
USER_SKILLS_DIR = config.USER_SKILLS_DIR
# For backward compat — code that references SKILLS_DIR
SKILLS_DIR = BUILTIN_SKILLS_DIR


# Hidden skills — require secret activation via self_config
_HIDDEN_SKILLS = {
    "spicy_duck": "quack",  # self_config(action="set", key="spicy_duck", value="quack")
}


def _active_preset_skills_dir() -> Path | None:
    """The active preset's skills/ directory, if any. Import lazily to avoid
    a circular import chain at module load."""
    try:
        import presets
        return presets.get_active_skills_dir()
    except Exception:
        return None


def _all_skill_paths() -> dict[str, Path]:
    """Return {name: path} for all skills. Later dirs override earlier ones.

    Priority (highest wins):
        1. Active preset skills (~/.castor/presets/<id>/skills/)
        2. User skills (~/.castor/skills/)
        3. Built-in skills (skills/)

    Hidden skills only appear when their activation key is set.
    """
    skills = {}
    search_dirs = [BUILTIN_SKILLS_DIR, USER_SKILLS_DIR]
    preset_dir = _active_preset_skills_dir()
    if preset_dir is not None:
        search_dirs.append(preset_dir)
    for d in search_dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            name = f.stem
            # Check if skill is hidden and requires activation
            if name in _HIDDEN_SKILLS:
                secret = _HIDDEN_SKILLS[name]
                if db.kv_get(name) != secret:
                    continue  # not activated, skip
            skills[name] = f
    return skills


def _find_skill(name: str) -> Path | None:
    """Find a skill file by name.

    Check order (first hit wins):
        1. Active preset skills
        2. User skills
        3. Built-in skills
    """
    # Reject names that contain path separators or other unsafe characters
    if not _SKILL_NAME_RE.match(name):
        return None
    preset_dir = _active_preset_skills_dir()
    if preset_dir is not None:
        p = preset_dir / f"{name}.py"
        if p.exists():
            return p
    user_path = USER_SKILLS_DIR / f"{name}.py"
    if user_path.exists():
        return user_path
    builtin_path = BUILTIN_SKILLS_DIR / f"{name}.py"
    if builtin_path.exists():
        return builtin_path
    return None

# Module cache: absolute path -> (mtime, module)
# Keyed by the FULL path (not the stem) so a preset-supplied skill that
# collides with a builtin name doesn't return a stale module from the
# cache when the active preset changes.
_module_cache: dict[str, tuple[float, ModuleType]] = {}

# ── Skill integrity manifest (SHA-256) ──────────────────────────────────
# Protects user skills at ~/.castor/skills/ from silent tampering.
# Built-in skills (in repo skills/) are git-tracked and exempt.
_MANIFEST_PATH = Path(config.DATA_DIR) / "skills_manifest.json"


def _load_manifest() -> dict:
    """Load skill integrity manifest.  Returns ``{abs_path: sha256_hex}``."""
    if _MANIFEST_PATH.exists():
        try:
            return json.loads(_MANIFEST_PATH.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            _log.warning("corrupt skills manifest — will re-register on next load")
            return {}
    return {}


def _save_manifest(manifest: dict) -> None:
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_user_skill(path: Path) -> bool:
    """True if *path* lives under the user-skills directory (not built-in)."""
    try:
        path.resolve().relative_to(Path(USER_SKILLS_DIR).resolve())
        return True
    except ValueError:
        return False


def _load_module(path: Path) -> ModuleType:
    """Load a Python module from path, with mtime-based caching."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        raise ImportError(f"Skill file not found: {path}")

    cache_key = str(path.resolve())
    cached = _module_cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    # Integrity check for user skills (not built-in / git-tracked)
    if _is_user_skill(path):
        manifest = _load_manifest()
        current_hash = _file_sha256(path)
        stored_hash = manifest.get(cache_key)
        if stored_hash is None:
            # First load — register
            manifest[cache_key] = current_hash
            _save_manifest(manifest)
            _log.info("skill %s: registered integrity hash", path.stem)
        elif stored_hash != current_hash:
            _log.warning(
                "skill %s: integrity hash mismatch (expected %.16s…, got %.16s…). "
                "Refusing to load. Delete %s to re-register.",
                path.stem, stored_hash, current_hash, _MANIFEST_PATH,
            )
            raise ImportError(
                f"Skill {path.stem} integrity check failed — file modified since registration"
            )

    # Load fresh
    name = f"skill_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _module_cache[cache_key] = (mtime, mod)
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


_DEFAULT_SKILLS = {"mcp_manager", "soul_editor", "skill_creator", "browser", "serial_port", "canvas"}  # always-on built-in skills


def get_active() -> set[str]:
    """Get set of active skill names from SQLite. Default skills always included."""
    raw = db.kv_get("active_skills")
    if not raw:
        return set(_DEFAULT_SKILLS)
    names = set(raw.split(","))
    all_paths = _all_skill_paths()
    valid = {n for n in names if n in all_paths}
    # Always include default skills + activated hidden skills
    valid |= {n for n in _DEFAULT_SKILLS if n in all_paths}
    for hidden, secret in _HIDDEN_SKILLS.items():
        if hidden in all_paths and db.kv_get(hidden) == secret:
            valid.add(hidden)
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
    Detects tool-name collisions across skills — first skill wins, duplicates
    are dropped with a warning.
    """
    active = get_active()
    all_tools = []
    seen_names: dict[str, str] = {}  # tool_name → skill_name
    for name in active:
        path = _find_skill(name)
        if not path:
            continue
        try:
            mod = _load_module(path)
            skill_tools = getattr(mod, "TOOLS", [])
            if compact:
                skill_tools = [_compact_tool(t) for t in skill_tools]
            for t in skill_tools:
                tool_name = t.get("function", {}).get("name")
                if not tool_name:
                    continue
                if tool_name in seen_names:
                    _log.warning(
                        "Tool name collision: '%s' defined by skill '%s' "
                        "shadows earlier definition from skill '%s' — skipping duplicate",
                        tool_name, name, seen_names[tool_name],
                    )
                    continue
                seen_names[tool_name] = name
                all_tools.append(t)
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
