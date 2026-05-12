"""qwe-qwe configuration — all settings in one place.

Override any setting via environment variables with QWE_ prefix:
  QWE_LLM_URL, QWE_LLM_MODEL, QWE_LLM_KEY,
  QWE_QDRANT_MODE, QWE_QDRANT_PATH, QWE_QDRANT_URL,
  QWE_DB_PATH, QWE_DATA_DIR

Embeddings are handled by FastEmbed (ONNX, local, no server needed).
"""

import os
from pathlib import Path

VERSION = "0.22.1"
_env = os.environ.get

# ── Data directory (all user data lives here, safe from git) ──
DATA_DIR = Path(_env("QWE_DATA_DIR", str(Path.home() / ".qwe-qwe")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# LLM
LLM_BASE_URL = _env("QWE_LLM_URL", "http://localhost:1234/v1")
LLM_MODEL = _env("QWE_LLM_MODEL", "qwen/qwen3.5-9b")
LLM_API_KEY = _env("QWE_LLM_KEY", "lm-studio")

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
PRESETS_DIR = DATA_DIR / "presets"
PRESETS_DIR.mkdir(exist_ok=True)

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
except Exception as e:
    import sys
    print(f"⚠️ Data migration failed: {e}", file=sys.stderr)
    # Don't block startup — user can still use the app


# Timezone offset from UTC (hours). Stored in DB, set via /soul or ask user.
# Default: 0 (UTC). Agent should ask user on first run and save to kv "timezone".
TZ_OFFSET = 0  # overridden at runtime from DB

# Agent defaults (overridable via settings UI / kv store)
MAX_HISTORY_MESSAGES = 10  # last N messages in context (smart compaction handles the rest)
MAX_MEMORY_RESULTS = 3     # top-K auto-retrieved from Qdrant per turn
MAX_EXPERIENCE_RESULTS = 2 # top-K past experience cases injected per turn
MAX_TOOL_ROUNDS = 0        # 0 = unlimited — loop detection handles infinite loops
COMPACTION_THRESHOLD = 20  # auto-compact after N messages in DB
THINKING_ENABLED = True    # send enable_thinking to model (toggle via /thinking or settings)

# ── Runtime settings (DB-backed) ──
# These are read from kv store at runtime, with config.py values as defaults.

# Editable settings registry: key → (kv_key, type, default, description, min, max)
EDITABLE_SETTINGS = {
    "max_history_messages": ("setting:max_history_messages", int, 10, "Messages kept in context", 1, None),
    "max_memory_results":   ("setting:max_memory_results",   int, 3,  "Memory results per turn", 0, None),
    "max_tool_rounds":      ("setting:max_tool_rounds",      int, 0,  "Max tool call rounds (0 = unlimited, loop detection handles loops)", 0, None),
    "compaction_threshold": ("setting:compaction_threshold",  int, 20, "Auto-compact after N messages", 3, None),
    "context_budget":       ("setting:context_budget",        int, 24000, "Token budget for context", 1000, None),
    "tool_retry_max":       ("setting:tool_retry_max",        int, 3,     "Max retries for broken tool calls", 0, None),
    "self_check_enabled":   ("setting:self_check_enabled",    int, 1,     "Self-check before shell/write_file (0=off, 1=on)", 0, 1),
    "heartbeat_interval_min": ("setting:heartbeat_interval_min", int, 30, "Heartbeat interval in minutes", 1, None),
    "experience_learning":  ("setting:experience_learning",   int, 1,     "Learn from past task executions (0=off, 1=on)", 0, 1),
    "presence_penalty":     ("setting:presence_penalty",      float, 1.5,  "Presence penalty (Qwen3.5 recommends 1.5)", 0.0, None),
    "rag_chunk_size":       ("setting:rag_chunk_size",        int, 800,    "RAG chunk size in chars (re-index after change)", 200, 4000),
    # ── Agent Loop ──
    "agent_loop_v2":        ("setting:agent_loop_v2",         int, 1,      "Use new agent loop v2 (0=legacy, 1=new)", 0, 1),
    # ── Knowledge Graph Synthesis ──
    "synthesis_enabled":    ("setting:synthesis_enabled",     int, 1,      "Enable night synthesis (0=off, 1=on)", 0, 1),
    "synthesis_time":       ("setting:synthesis_time",        str, "03:00", "Night synthesis time (HH:MM)", "", ""),
    "synthesis_max_per_run": ("setting:synthesis_max_per_run", int, 50,    "Max items per synthesis run", 1, None),
    "rag_chunk_overlap":    ("setting:rag_chunk_overlap",     int, 100,    "RAG chunk overlap in chars", 0, 500),
    "tz_name":              ("setting:tz_name",               str, "",     "IANA timezone name (e.g. Europe/Moscow, America/New_York). When set, scheduler uses it via zoneinfo and honours DST. Empty = use fixed TZ_OFFSET.", "", ""),
    "fallback_provider":    ("setting:fallback_provider",     str, "",     "Fallback provider for complex tasks (e.g. openrouter)", "", ""),
    "fallback_model":       ("setting:fallback_model",        str, "",     "Fallback model (e.g. anthropic/claude-sonnet-4)", "", ""),
    "ollama_num_ctx":       ("setting:ollama_num_ctx",        int, 16384,  "Ollama context window (tokens)", 2048, 131072),
    "model_context":        ("setting:model_context",         int, 0,      "Model context window in tokens (0 = auto-detect from provider, else override)", 0, 2000000),
    "yt_cookies_from_browser": ("setting:yt_cookies_from_browser", str, "", "Use browser cookies for YouTube to bypass rate limits. Values: chrome, firefox, edge, safari, brave, chromium, opera, vivaldi. Empty = anonymous (rate-limited after a few videos).", "", ""),
    "embed_device":            ("setting:embed_device",            str, "cpu", "FastEmbed ONNX execution provider. qwe-qwe is CPU-only by design — the CPU embedder runs comfortably on a laptop and avoids CUDA install pain. Set to 'cuda' only if you've explicitly installed onnxruntime-gpu + CUDA Toolkit + cuDNN and want GPU acceleration; 'auto' tries CUDA first and falls back to CPU on failure.", "", ""),
    # ── Privacy / Telemetry ──
    # All four default to "off / empty" — no telemetry leaves the machine
    # without explicit opt-in via Settings → Privacy or first-run prompt.
    # See `docs/PRIVACY.md` for the full data inventory + privacy contract.
    "telemetry_enabled":      ("setting:telemetry_enabled",     int, 0,     "Send anonymous usage metrics to the configured endpoint. Default OFF. Opt-in only. No chat content / no soul / no PII collected. See Settings → Privacy → Telemetry for the full list of what's sent. See docs/PRIVACY.md for details.", 0, 1),
    # Project-default destination is the deepfounder.ai self-hosted Countly
    # instance. This is the endpoint your events go to IF you explicitly
    # opt in via Settings → Privacy → Telemetry. Default is still OFF; the
    # endpoint just tells the queue where to flow when enabled. Override
    # with empty string (queue locally, nothing leaves) or any other URL
    # (point at your own Countly / collector).
    "telemetry_endpoint":     ("setting:telemetry_endpoint",    str, "https://qwelytics.deepfounder.ai/i", "URL to POST batched telemetry events to. Project default is the deepfounder.ai self-hosted Countly instance. Empty = no network calls. Override to point at your own Countly / custom collector. For Countly: full URL including /i path.", "", ""),
    "telemetry_format":       ("setting:telemetry_format",      str, "countly", "Wire format. 'raw' = single POST with our JSON shape ({events: [...]}) suited for custom collectors. 'countly' = batched POST in Countly's /i format using anonymous_id as device_id (cross-day per-user tracking works natively).", "", ""),
    # Public app_key for the project's Countly instance. App keys are
    # NOT secrets in Countly's threat model — they ride in plain HTML on
    # any Countly-tracked website. Hardcoding the project default here
    # is intentional and equivalent to PostHog/Sentry projects publishing
    # their public DSN. Self-hosted users override this with their own
    # Countly app key.
    "telemetry_countly_app_key": ("setting:telemetry_countly_app_key", str, "4fdcd9057fcbfa22a66d9c66e4e15f3c64f22741", "Countly app key from your dashboard (Settings → Apps). Project default points at the deepfounder.ai instance. Required when telemetry_format=countly. Override with your own app key when self-hosting.", "", ""),
    "telemetry_anonymous_id": ("setting:telemetry_anonymous_id", str, "",   "Random UUID generated on first opt-in. Read-only (rotate via Settings → Privacy → Reset). Never derived from PII.", "", ""),
    # Consent policy version. Bumped when the project changes either
    # ALLOWED_EVENTS shape or the default destination (so users see the
    # new endpoint URL before silently routing there). Wire-up of the
    # re-prompt-on-mismatch logic lands in a follow-up — for now this is
    # informational metadata that stamps which policy the user accepted.
    # Version 1 = first release with a default project endpoint
    # (deepfounder.ai Countly).
    "telemetry_consent_version": ("setting:telemetry_consent_version", int, 0, "Consent policy version the user agreed to. Bumped when ALLOWED_EVENTS changes shape OR default endpoint changes, triggering a re-prompt. Internal — not user-edited.", 0, 1000),
    # ── Vision (Camera) ──
    "camera_index":         ("setting:camera_index",          int, -1,    "Camera index for agent vision (-1 = auto-detect best, 0/1/2 = specific camera)", -1, 10),
    "camera_resolution":    ("setting:camera_resolution",     str, "auto", "Capture resolution: auto (camera default), 480p (640x480), 720p (1280x720), 1080p (1920x1080). Higher = sharper vision but bigger payload to LLM.", "", ""),
    "camera_quality":       ("setting:camera_quality",        int, 70,    "JPEG quality (1-100) for camera frames sent to vision LLM. 70 default; raise for sharper detail at cost of larger base64 size, lower for cheaper/faster vision turns.", 1, 100),
    # ── Voice: STT ──
    "stt_backend":          ("setting:stt_backend",           str, "auto", "STT backend: auto (API if key else local), local, api", "", ""),
    "stt_model":            ("setting:stt_model",             str, "base", "Whisper model size (tiny/base/small/medium) — local only", "", ""),
    "stt_language":         ("setting:stt_language",           str, "",     "STT language (empty=auto, en, ru, etc.)", "", ""),
    "stt_api_url":          ("setting:stt_api_url",           str, "",     "STT API URL (OpenAI-compatible). Empty = api.openai.com. Examples: Groq https://api.groq.com/openai/v1", "", ""),
    "stt_api_model":        ("setting:stt_api_model",         str, "whisper-1", "STT API model name (whisper-1, whisper-large-v3-turbo, etc.)", "", ""),
    "stt_openai_key":       ("setting:stt_openai_key",        str, "",     "API key for STT (OpenAI / Groq / any OpenAI-compatible)", "", ""),
    # ── Voice: TTS (s2.cpp HTTP API) ──
    "tts_enabled":          ("setting:tts_enabled",           int, 0,     "Enable TTS voice responses (0=off, 1=on)", 0, 1),
    "tts_api_url":          ("setting:tts_api_url",           str, "http://localhost:3030", "TTS server URL (s2.cpp /generate, custom /tts, or OpenAI-compatible /v1/audio/speech)", "", ""),
    "tts_api_key":          ("setting:tts_api_key",           str, "",     "TTS API key (for OpenAI / ElevenLabs / any cloud API)", "", ""),
    "tts_api_model":        ("setting:tts_api_model",         str, "tts-1", "TTS API model (tts-1, tts-1-hd, or provider-specific)", "", ""),
    "tts_api_voice":        ("setting:tts_api_voice",         str, "alloy", "TTS API voice (alloy, echo, fable, onyx, nova, shimmer)", "", ""),
    "tts_ref_audio":        ("setting:tts_ref_audio",         str, "",     "Reference audio for voice cloning (5-30s WAV) — local backends only", "", ""),
    "tts_ref_text":         ("setting:tts_ref_text",          str, "",     "Transcript of reference audio", "", ""),
    # ── Cost Tracking ──
    "pricing_url":          ("setting:pricing_url",           str, "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json", "URL for online pricing JSON (LiteLLM format). Override for air-gapped mirrors.", "", ""),
    "pricing_auto_update":  ("setting:pricing_auto_update",   bool, True, "Refresh pricing every 24h in background.", None, None),
    # ── Auto-Resume ──
    "resume_ttl_web_sec":       ("setting:resume_ttl_web_sec",       int, 604800, "How long (sec) a Web abort stays resumable. Default 7 days.", 60, 31536000),
    "resume_ttl_telegram_sec":  ("setting:resume_ttl_telegram_sec",  int, 86400,  "How long (sec) a Telegram abort stays resumable. Default 24h.", 60, 31536000),
    "resume_ttl_routine_sec":   ("setting:resume_ttl_routine_sec",   int, 300,    "Window (sec) for auto-firing aborted routines on server start. Default 5 min.", 0, 86400),
    "resume_routine_auto":      ("setting:resume_routine_auto",      bool, True,   "Enable/disable routine auto-resume entirely.", None, None),
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
    if isinstance(v, (int, float)):
        if min_val is not None and v < min_val:
            return f"Out of range: {v} (min {min_val})"
        if max_val is not None and v > max_val:
            return f"Out of range: {v} (max {max_val})"
    db.kv_set(kv_key, str(v))
    return f"✓ {key} = {v}"


def export_config() -> dict:
    """Export all settings, provider, soul, heartbeat as JSON-serializable dict."""
    import db
    import json
    import time

    # Settings
    settings = {}
    for key in EDITABLE_SETTINGS:
        settings[key] = get(key)

    # Provider
    try:
        import providers
        provider_data = {
            "active": providers.get_active_name(),
            "model": providers.get_model(),
        }
        p = providers.get_provider()
        if p:
            provider_data["url"] = p.get("url", "")
            provider_data["key"] = p.get("key", "")
    except Exception:
        provider_data = {}

    # Soul
    try:
        import soul
        soul_data = soul.load()
    except Exception:
        soul_data = {}

    # Heartbeat
    hb_enabled = db.kv_get("heartbeat:enabled") != "0"
    raw_items = db.kv_get("heartbeat:items")
    hb_items = json.loads(raw_items) if raw_items else []

    # Scheduled tasks
    try:
        import scheduler
        tasks = scheduler.list_tasks()
    except Exception:
        tasks = []

    return {
        "meta": {"version": VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        "settings": settings,
        "provider": provider_data,
        "soul": soul_data,
        "heartbeat": {"enabled": hb_enabled, "items": hb_items},
        "cron": tasks,
    }


def import_config(data: dict) -> list[str]:
    """Import settings from exported dict. Returns list of applied changes."""
    import db
    import json
    results = []

    # Settings
    for key, value in data.get("settings", {}).items():
        if key in EDITABLE_SETTINGS:
            r = set(key, value)
            results.append(r)

    # Provider
    prov = data.get("provider", {})
    if prov.get("active"):
        try:
            import providers
            providers.switch(prov["active"])
            results.append(f"✓ provider = {prov['active']}")
            if prov.get("model"):
                providers.set_model(prov["model"])
                results.append(f"✓ model = {prov['model']}")
        except Exception as e:
            results.append(f"✗ provider: {e}")

    # Soul
    soul_data = data.get("soul")
    if soul_data and isinstance(soul_data, dict):
        try:
            import soul
            soul.save(soul_data)
            results.append("✓ soul traits restored")
        except Exception as e:
            results.append(f"✗ soul: {e}")

    # Heartbeat
    hb = data.get("heartbeat")
    if hb and isinstance(hb, dict):
        db.kv_set("heartbeat:enabled", "1" if hb.get("enabled") else "0")
        if hb.get("items"):
            db.kv_set("heartbeat:items", json.dumps(hb["items"]))
        results.append("✓ heartbeat restored")

    return results


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
