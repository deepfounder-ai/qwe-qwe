"""Provider management — switch between LLM endpoints on the fly.

Stores provider configs in SQLite. Supports any OpenAI-compatible API:
LM Studio, Ollama, OpenAI, OpenRouter, Together, Groq, local vLLM, etc.

Usage:
    import providers
    client = providers.get_client()       # OpenAI client for current provider
    providers.set_model("gpt-4o")         # switch model
    providers.switch("openai")            # switch provider
    providers.add("groq", url="...", key="...", models=["llama-3.1-70b"])
"""

import json
from openai import OpenAI
import config
import db
import logger

_log = logger.get("providers")

# ── Cached client (invalidated on provider/model switch) ──
_client: OpenAI | None = None
_client_key: str | None = None  # "url|key" — to detect when to recreate


# ── Built-in presets ──

PRESETS = {
    "lmstudio": {
        "name": "LM Studio",
        "url": "http://192.168.0.49:1234/v1",
        "key": "lm-studio",
        "models": [],  # auto-detected
    },
    "ollama": {
        "name": "Ollama",
        "url": "http://localhost:11434/v1",
        "key": "ollama",
        "models": [],
    },
    "openai": {
        "name": "OpenAI",
        "url": "https://api.openai.com/v1",
        "key": "",  # user must set
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini"],
    },
    "openrouter": {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1",
        "key": "",
        "models": ["anthropic/claude-sonnet-4", "google/gemini-2.5-flash", "deepseek/deepseek-chat"],
    },
    "groq": {
        "name": "Groq",
        "url": "https://api.groq.com/openai/v1",
        "key": "",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    },
    "together": {
        "name": "Together",
        "url": "https://api.together.xyz/v1",
        "key": "",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://api.deepseek.com/v1",
        "key": "",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
}


# ── Core functions ──

def _db_key(k: str) -> str:
    return f"provider:{k}"


def get_active_name() -> str:
    """Get the active provider name."""
    return db.kv_get(_db_key("active")) or "lmstudio"


def get_provider(name: str | None = None) -> dict:
    """Get provider config by name. Returns {name, url, key, models, model}."""
    name = name or get_active_name()

    # Check user-saved config first
    raw = db.kv_get(_db_key(f"config:{name}"))
    if raw:
        try:
            p = json.loads(raw)
            p.setdefault("name", name)
            return p
        except json.JSONDecodeError:
            pass

    # Fall back to preset
    if name in PRESETS:
        p = dict(PRESETS[name])
        p["name"] = name
        return p

    return {"name": name, "url": "", "key": "", "models": []}


def get_active() -> dict:
    """Get the current active provider config + active model."""
    p = get_provider()
    p["model"] = get_model()
    return p


def get_model() -> str:
    """Get the currently active model."""
    return db.kv_get(_db_key("model")) or config.LLM_MODEL


def get_url() -> str:
    """Get the current API URL."""
    p = get_provider()
    return p.get("url") or config.LLM_BASE_URL


def get_key() -> str:
    """Get the current API key."""
    p = get_provider()
    return p.get("key") or config.LLM_API_KEY


def get_client() -> OpenAI:
    """Get or create an OpenAI client for the active provider."""
    global _client, _client_key

    url = get_url()
    key = get_key()
    cache_key = f"{url}|{key}"

    if _client is not None and _client_key == cache_key:
        return _client

    _client = OpenAI(base_url=url, api_key=key)
    _client_key = cache_key
    _log.info(f"client created: {url}")
    return _client


def _invalidate():
    """Force client recreation on next call."""
    global _client, _client_key
    _client = None
    _client_key = None


# ── Switch/set operations ──

def set_model(model: str) -> str:
    """Set the active model (within current provider)."""
    old = get_model()
    db.kv_set(_db_key("model"), model)
    _log.info(f"model switched: {old} → {model}")

    # Update config.py runtime values
    config.LLM_MODEL = model
    _invalidate()
    return f"✓ Model: {model}"


def switch(name: str) -> str:
    """Switch to a different provider."""
    p = get_provider(name)
    if not p.get("url"):
        return f"✗ Unknown provider '{name}'. Available: {', '.join(list_providers())}"

    if not p.get("key"):
        return f"✗ Provider '{name}' has no API key. Set it with: /provider {name} key <your-key>"

    old = get_active_name()
    db.kv_set(_db_key("active"), name)

    # Update config runtime values
    config.LLM_BASE_URL = p["url"]
    config.LLM_API_KEY = p["key"]

    # Always reset model when switching providers.
    # Current model may not exist on the new provider.
    current_model = get_model()
    provider_models = p.get("models", [])

    if provider_models and current_model not in provider_models:
        # Current model doesn't exist on new provider — pick first available
        new_model = provider_models[0]
        config.LLM_MODEL = new_model
        db.kv_set(_db_key("model"), new_model)
        _log.info(f"model auto-switched: {current_model} → {new_model} (not available on {name})")
    elif not provider_models:
        # No model list (e.g. lmstudio/ollama) — try to discover
        try:
            discovered = fetch_models(name)
            if discovered and current_model not in discovered:
                new_model = discovered[0]
                config.LLM_MODEL = new_model
                db.kv_set(_db_key("model"), new_model)
                _log.info(f"model auto-switched: {current_model} → {new_model} (discovered from {name})")
        except Exception:
            pass  # can't discover, keep current model

    _invalidate()
    _log.info(f"provider switched: {old} → {name} ({p['url']})")
    return f"✓ Switched to {p.get('name', name)} ({p['url']})"


def add(name: str, url: str, key: str = "", models: list[str] | None = None) -> str:
    """Add or update a provider."""
    p = {
        "name": name,
        "url": url.rstrip("/"),
        "key": key,
        "models": models or [],
    }
    db.kv_set(_db_key(f"config:{name}"), json.dumps(p))
    _log.info(f"provider added: {name} → {url}")
    return f"✓ Provider '{name}' saved ({url})"


def set_key(name: str, key: str) -> str:
    """Set API key for a provider."""
    p = get_provider(name)
    p["key"] = key
    db.kv_set(_db_key(f"config:{name}"), json.dumps(p))

    # If this is the active provider, update runtime
    if name == get_active_name():
        config.LLM_API_KEY = key
        _invalidate()

    _log.info(f"API key set for: {name}")
    return f"✓ API key set for {name}"


def list_providers() -> list[str]:
    """List all available provider names (presets + custom)."""
    names = set(PRESETS.keys())
    # Scan DB for custom providers
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT key FROM kv WHERE key LIKE 'provider:config:%'"
    ).fetchall()
    for (k,) in rows:
        name = k.replace("provider:config:", "")
        names.add(name)
    return sorted(names)


def list_all() -> list[dict]:
    """List all providers with status."""
    active = get_active_name()
    result = []
    for name in list_providers():
        p = get_provider(name)
        result.append({
            "name": name,
            "display": p.get("name", name),
            "url": p.get("url", ""),
            "has_key": bool(p.get("key")),
            "models": p.get("models", []),
            "active": name == active,
        })
    return result


# ── Model discovery ──

def fetch_models(name: str | None = None) -> list[str]:
    """Fetch available models from provider's /v1/models endpoint."""
    p = get_provider(name)
    url = p.get("url", "")
    key = p.get("key", "")

    if not url:
        return []

    try:
        client = OpenAI(base_url=url, api_key=key or "none")
        response = client.models.list()
        models = sorted([m.id for m in response.data])

        # Save discovered models
        p["models"] = models
        db.kv_set(_db_key(f"config:{name or get_active_name()}"), json.dumps(p))

        _log.info(f"discovered {len(models)} models from {name or get_active_name()}")
        return models
    except Exception as e:
        _log.warning(f"model discovery failed for {name}: {e}")
        return p.get("models", [])


# ── Init: sync config.py with DB state on import ──

def _init():
    """Load saved provider state into config module."""
    active = get_active_name()
    p = get_provider(active)

    if p.get("url"):
        config.LLM_BASE_URL = p["url"]
    if p.get("key"):
        config.LLM_API_KEY = p["key"]

    saved_model = db.kv_get(_db_key("model"))
    if saved_model:
        config.LLM_MODEL = saved_model

    # Timezone
    tz = db.kv_get("tz_offset")
    if tz:
        config.TZ_OFFSET = int(tz)


_init()
