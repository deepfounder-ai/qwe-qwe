"""Online pricing fetcher + cache + fallback chain.

Fetches LiteLLM's community-maintained model_prices_and_context_window.json,
caches it on disk, and falls back to a bundled minimal dict for air-gapped
or offline scenarios. Network I/O is owned by the background refresher
(start_background_refresher) and POST /api/pricing/refresh — get_price()
itself is purely in-memory and never blocks.

Lookup chain (in get_price):
  1. KV override:     pricing_override_<model>
  2. Local provider:  lmstudio:/ollama:/local: prefix → 0.0
  3. Memory cache:    populated by _ensure_loaded()
  4. Bundled fallback: top-10 hardcoded models
  5. None             (caller writes cost_usd = NULL)
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, Optional

import config
import db
import logger

_log = logger.get("pricing")

DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_TTL_SEC = 24 * 3600
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB hard cap on JSON download
REMOTE_TIMEOUT_SEC = 10

# Top-10 fallback. Values in $/token (NOT $/1M tokens).
_BUNDLED_FALLBACK: dict[str, dict[str, float]] = {
    "gpt-4o-mini":                {"input": 0.00000015, "output": 0.00000060},
    "gpt-4o":                     {"input": 0.00000250, "output": 0.00001000},
    "gpt-4-turbo":                {"input": 0.00001000, "output": 0.00003000},
    "claude-3-5-sonnet-20241022": {"input": 0.00000300, "output": 0.00001500},
    "claude-3-5-haiku-20241022":  {"input": 0.00000080, "output": 0.00000400},
    "claude-3-opus-20240229":     {"input": 0.00001500, "output": 0.00007500},
    "deepseek-chat":              {"input": 0.00000014, "output": 0.00000028},
    "groq/llama-3.3-70b-versatile": {"input": 0.00000059, "output": 0.00000079},
    "groq/llama-3.1-8b-instant":  {"input": 0.00000005, "output": 0.00000008},
    "mistral-large-latest":       {"input": 0.00000200, "output": 0.00000600},
}

_LOCAL_PREFIXES = ("lmstudio:", "ollama:", "local:")

SKIP_MODES = {"embedding", "image_generation", "audio_transcription", "audio_speech"}

_lock = threading.Lock()
_pricing_cache: dict[str, dict[str, float]] | None = None
_cache_fetched_at: float | None = None
_refresher_started = False
_refresher_lock = threading.Lock()


def _cache_path() -> Path:
    return Path(config.DATA_DIR) / "pricing_cache.json"


def get_price(model: str, kind: Literal["input", "output"]) -> float | None:
    """$/token for (model, kind); None if unknown. Never does network I/O."""
    if not model:
        return None
    # 1. KV override
    raw = db.kv_get(f"pricing_override_{model}")
    if raw:
        try:
            return float(json.loads(raw)[kind])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            _log.warning(f"invalid pricing_override for {model}")
    # 2. Local providers
    if model.startswith(_LOCAL_PREFIXES):
        return 0.0
    # 3. Memory / disk cache → 4. Bundled fallback
    pricing = _ensure_loaded()
    entry = pricing.get(model)
    if entry and kind in entry:
        return entry[kind]
    fb = _BUNDLED_FALLBACK.get(model)
    if fb and kind in fb:
        return fb[kind]
    return None


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Total $ cost. None if either side's price is unknown."""
    in_p = get_price(model, "input")
    out_p = get_price(model, "output")
    if in_p is None or out_p is None:
        return None
    return float(input_tokens) * in_p + float(output_tokens) * out_p


def _ensure_loaded() -> dict[str, dict[str, float]]:
    """Lazy-load disk cache into memory. Empty dict if neither present."""
    global _pricing_cache, _cache_fetched_at
    if _pricing_cache is not None:
        return _pricing_cache
    with _lock:
        if _pricing_cache is not None:
            return _pricing_cache
        path = _cache_path()
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                _pricing_cache = payload.get("models") or {}
                _cache_fetched_at = float(payload.get("fetched_at") or 0)
                return _pricing_cache
            except (json.JSONDecodeError, OSError, ValueError) as e:
                _log.warning(f"corrupt pricing cache, ignoring: {e}")
        _pricing_cache = {}
        _cache_fetched_at = None
        return _pricing_cache


def last_updated() -> Optional[float]:
    _ensure_loaded()
    return _cache_fetched_at


def all_known_models() -> list[str]:
    return sorted(set(_ensure_loaded().keys()) | set(_BUNDLED_FALLBACK.keys()))


def _normalize_litellm(raw: dict) -> dict[str, dict[str, float]]:
    """Convert LiteLLM's JSON into our flat {model: {input, output}} shape.

    Skips:
      - the 'sample_spec' meta-entry
      - any entry where mode is in SKIP_MODES (embeddings, images, audio)
      - any entry missing input_cost_per_token or output_cost_per_token
    """
    out: dict[str, dict[str, float]] = {}
    for name, entry in raw.items():
        if name == "sample_spec" or not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if mode in SKIP_MODES:
            continue
        try:
            in_p = float(entry["input_cost_per_token"])
            out_p = float(entry["output_cost_per_token"])
        except (KeyError, TypeError, ValueError):
            continue
        out[name] = {"input": in_p, "output": out_p}
    return out


def _ssrf_allowed(url: str) -> bool:
    """Block private/loopback/link-local unless QWE_ALLOW_PRIVATE_URLS=1."""
    if os.environ.get("QWE_ALLOW_PRIVATE_URLS") == "1":
        return True
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            ip = ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
    except (OSError, ValueError):
        return False
    return True


def refresh_pricing(force: bool = False) -> bool:
    """Refresh pricing from remote. Returns True on success.

    Cache metadata updates are serialised under ``_lock``; concurrent
    refreshes may fetch independently (network + parse happen without
    the lock) but disk writes are atomic via ``Path.replace`` and global
    state is set together. Never raises — network/parse errors are
    logged and surfaced as ``False``.
    """
    global _pricing_cache, _cache_fetched_at
    url = config.get("pricing_url") or DEFAULT_PRICING_URL
    if not force and _cache_fetched_at and (time.time() - _cache_fetched_at) < CACHE_TTL_SEC:
        return True
    if not _ssrf_allowed(url):
        _log.warning(f"pricing_url blocked by SSRF guard: {url}")
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "qwe-qwe-pricing/1.0"})
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT_SEC) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            _log.warning(f"pricing response > {MAX_BODY_BYTES} bytes, refusing")
            return False
        raw = json.loads(body.decode("utf-8"))
        models = _normalize_litellm(raw)
        if not models:
            _log.warning("pricing JSON yielded zero usable models; keeping cache")
            return False
        payload = {
            "fetched_at": time.time(),
            "source_url": url,
            "models": models,
        }
        tmp = _cache_path().with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(_cache_path())
        with _lock:
            _pricing_cache = models
            _cache_fetched_at = payload["fetched_at"]
        _log.info(f"pricing refreshed: {len(models)} models")
        return True
    except Exception as e:
        _log.warning(f"pricing refresh failed: {e}")
        return False


def start_background_refresher() -> None:
    """Start a daemon thread that calls refresh_pricing() every CACHE_TTL_SEC.

    Idempotent — safe to call multiple times; only starts one thread.
    No-op when pricing_auto_update is disabled.
    """
    global _refresher_started
    if not config.get("pricing_auto_update"):
        return
    with _refresher_lock:
        if _refresher_started:
            return
        _refresher_started = True

    def _loop():
        while True:
            try:
                refresh_pricing(force=False)
            except Exception as e:
                _log.warning(f"pricing refresher loop error: {e}")
            time.sleep(CACHE_TTL_SEC)

    t = threading.Thread(target=_loop, name="pricing-refresher", daemon=True)
    t.start()
    _log.info("pricing background refresher started")
