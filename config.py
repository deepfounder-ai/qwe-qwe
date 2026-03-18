"""qwe-qwe configuration — all settings in one place.

Override any setting via environment variables with QWE_ prefix:
  QWE_LLM_URL, QWE_LLM_MODEL, QWE_LLM_KEY,
  QWE_EMBED_URL, QWE_EMBED_MODEL, QWE_EMBED_KEY,
  QWE_QDRANT_MODE, QWE_QDRANT_PATH, QWE_QDRANT_URL,
  QWE_DB_PATH, QWE_DATA_DIR
"""

import os
from pathlib import Path

_env = os.environ.get

# ── Data directory (all user data lives here, safe from git) ──
DATA_DIR = Path(_env("QWE_DATA_DIR", str(Path.home() / ".qwe-qwe")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# LLM
LLM_BASE_URL = _env("QWE_LLM_URL", "http://localhost:1234/v1")
LLM_MODEL = _env("QWE_LLM_MODEL", "qwen/qwen3.5-9b")
LLM_API_KEY = _env("QWE_LLM_KEY", "lm-studio")

# Embeddings (defaults to same server as LLM)
EMBED_BASE_URL = _env("QWE_EMBED_URL", LLM_BASE_URL)
EMBED_MODEL = _env("QWE_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
EMBED_API_KEY = _env("QWE_EMBED_KEY", LLM_API_KEY)
EMBED_DIM = int(_env("QWE_EMBED_DIM", "768"))

# Qdrant (local disk for persistence, no server needed)
QDRANT_MODE = _env("QWE_QDRANT_MODE", "disk")  # "memory" | "disk" | "server"
QDRANT_PATH = _env("QWE_QDRANT_PATH", str(DATA_DIR / "memory"))  # for disk mode
QDRANT_URL = _env("QWE_QDRANT_URL", "http://localhost:6333")  # for server mode
QDRANT_COLLECTION = _env("QWE_QDRANT_COLLECTION", "qwe_qwe")

# SQLite
DB_PATH = _env("QWE_DB_PATH", str(DATA_DIR / "qwe_qwe.db"))

# Other data paths
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
BACKUPS_DIR = DATA_DIR / "backups"
BACKUPS_DIR.mkdir(exist_ok=True)
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
USER_SKILLS_DIR = DATA_DIR / "skills"
USER_SKILLS_DIR.mkdir(exist_ok=True)
WORKSPACE_DIR = DATA_DIR / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

# ── Auto-migrate old data from project root to DATA_DIR ──
_PROJECT_ROOT = Path(__file__).parent

def _migrate_data():
    """Move user data from old locations to ~/.qwe-qwe/ (one-time migration).

    Old versions stored data relative to CWD (could be ~, project root, anywhere).
    We search multiple candidate dirs to find the real data.
    """
    import shutil
    marker = DATA_DIR / ".migrated_v2"
    if marker.exists():
        return
    moved = []

    # Candidate directories where old data might live
    # (project root, CWD, home — old code used relative paths)
    _candidates = []
    for d in (_PROJECT_ROOT, Path.cwd(), Path.home()):
        d = d.resolve()
        if d not in _candidates and d != DATA_DIR.resolve():
            _candidates.append(d)

    # Files to migrate — pick the LARGEST (most data) from candidates
    for fname in ("qwe_qwe.db", "qwe_qwe.db-shm", "qwe_qwe.db-wal",
                  "soul.json", "user.md", "heartbeat.md"):
        dst = DATA_DIR / fname
        if dst.exists() and dst.stat().st_size > 0:
            continue  # already have data, don't overwrite
        best_src = None
        best_size = 0
        for cdir in _candidates:
            src = cdir / fname
            if src.exists():
                sz = src.stat().st_size
                if sz > best_size:
                    best_size = sz
                    best_src = src
        if best_src:
            shutil.copy2(str(best_src), str(dst))
            moved.append(f"{fname} (from {best_src.parent})")

    # Directories to migrate
    for dname, target in [("memory", DATA_DIR / "memory"),
                          ("uploads", UPLOADS_DIR),
                          ("backups", BACKUPS_DIR),
                          ("logs", LOGS_DIR)]:
        if target.exists() and any(target.iterdir()):
            continue  # already has content
        for cdir in _candidates:
            src = cdir / dname
            if src.is_dir() and any(src.iterdir()):
                shutil.copytree(str(src), str(target), dirs_exist_ok=True)
                moved.append(f"{dname}/ (from {src.parent})")
                break

    # User skills (non-builtin .py files in skills/)
    _BUILTIN = {"__init__.py", "weather.py", "notes.py", "timer.py",
                "soul_editor.py", "skill_creator.py"}
    for cdir in _candidates:
        old_skills = cdir / "skills"
        if not old_skills.is_dir():
            continue
        for f in old_skills.glob("*.py"):
            if f.name not in _BUILTIN and not f.name.startswith("_"):
                dst = USER_SKILLS_DIR / f.name
                if not dst.exists():
                    shutil.copy2(str(f), str(dst))
                    moved.append(f"skills/{f.name}")

    marker.write_text(f"migrated: {', '.join(moved) or 'nothing to move'}\n")

try:
    _migrate_data()
except Exception:
    pass  # don't block startup on migration failure


# Timezone offset from UTC (hours). Stored in DB, set via /soul or ask user.
# Default: 0 (UTC). Agent should ask user on first run and save to kv "timezone".
TZ_OFFSET = 0  # overridden at runtime from DB

# Agent defaults (overridable via settings UI / kv store)
MAX_HISTORY_MESSAGES = 10  # last N messages in context (smart compaction handles the rest)
MAX_MEMORY_RESULTS = 3     # top-K auto-retrieved from Qdrant per turn
MAX_EXPERIENCE_RESULTS = 2 # top-K past experience cases injected per turn
MAX_TOOL_ROUNDS = 10       # max consecutive tool calls per turn
COMPACTION_THRESHOLD = 20  # auto-compact after N messages in DB
THINKING_ENABLED = False   # send enable_thinking to model (toggle via /thinking or settings)

# ── Runtime settings (DB-backed) ──
# These are read from kv store at runtime, with config.py values as defaults.

# Editable settings registry: key → (kv_key, type, default, description, min, max)
EDITABLE_SETTINGS = {
    "max_history_messages": ("setting:max_history_messages", int, 10, "Messages kept in context", 2, 50),
    "max_memory_results":   ("setting:max_memory_results",   int, 3,  "Memory results per turn", 0, 10),
    "max_tool_rounds":      ("setting:max_tool_rounds",      int, 10, "Max tool call rounds", 1, 30),
    "compaction_threshold": ("setting:compaction_threshold",  int, 20, "Auto-compact after N messages", 5, 100),
    "context_budget":       ("setting:context_budget",        int, 24000, "Token budget for context", 4000, 60000),
    "tool_retry_max":       ("setting:tool_retry_max",        int, 3,     "Max retries for broken tool calls", 0, 5),
    "self_check_enabled":   ("setting:self_check_enabled",    int, 1,     "Self-check before shell/write_file (0=off, 1=on)", 0, 1),
    "heartbeat_interval_min": ("setting:heartbeat_interval_min", int, 30, "Heartbeat interval in minutes", 5, 1440),
    "experience_learning":  ("setting:experience_learning",   int, 1,     "Learn from past task executions (0=off, 1=on)", 0, 1),
    "presence_penalty":     ("setting:presence_penalty",      float, 1.5,  "Presence penalty (Qwen3.5 recommends 1.5)", 0.0, 2.0),
}


def get(key: str):
    """Get a setting value from DB, falling back to default."""
    import db
    if key not in EDITABLE_SETTINGS:
        raise KeyError(f"Unknown setting: {key}")
    kv_key, type_, default, *_ = EDITABLE_SETTINGS[key]
    val = db.kv_get(kv_key)
    if val is not None:
        try:
            return type_(val)
        except (ValueError, TypeError):
            return default
    return default


def set(key: str, value) -> str:
    """Set a setting value in DB. Returns confirmation string."""
    import db
    if key not in EDITABLE_SETTINGS:
        return f"Unknown setting: {key}"
    kv_key, type_, default, desc, min_val, max_val = EDITABLE_SETTINGS[key]
    try:
        v = type_(value)
    except (ValueError, TypeError):
        return f"Invalid value: {value} (expected {type_.__name__})"
    if v < min_val or v > max_val:
        return f"Out of range: {v} (allowed {min_val}-{max_val})"
    db.kv_set(kv_key, str(v))
    return f"✓ {key} = {v}"


def get_all() -> dict:
    """Get all editable settings with current values and metadata."""
    result = {}
    for key, (kv_key, type_, default, desc, min_val, max_val) in EDITABLE_SETTINGS.items():
        result[key] = {
            "value": get(key),
            "default": default,
            "description": desc,
            "min": min_val,
            "max": max_val,
            "type": type_.__name__,
        }
    return result
