"""qwe-qwe configuration — all settings in one place."""

# LLM
LLM_BASE_URL = "http://192.168.0.49:1234/v1"
LLM_MODEL = "qwen/qwen3.5-9b"
LLM_API_KEY = "lm-studio"

# Embeddings (same LM Studio server)
EMBED_BASE_URL = "http://192.168.0.49:1234/v1"
EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"
EMBED_API_KEY = "lm-studio"
EMBED_DIM = 768

# Qdrant (local disk for persistence, no server needed)
QDRANT_MODE = "disk"  # "memory" | "disk" | "server"
QDRANT_PATH = "./memory"  # for disk mode
QDRANT_URL = "http://localhost:6333"  # for server mode
QDRANT_COLLECTION = "qwe_qwe"

# SQLite
DB_PATH = "qwe_qwe.db"

# Timezone offset from UTC (hours). Stored in DB, set via /soul or ask user.
# Default: 0 (UTC). Agent should ask user on first run and save to kv "timezone".
TZ_OFFSET = 0  # overridden at runtime from DB

# Agent defaults (overridable via settings UI / kv store)
MAX_HISTORY_MESSAGES = 10  # last N messages in context (smart compaction handles the rest)
MAX_MEMORY_RESULTS = 3     # top-K auto-retrieved from Qdrant per turn
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
