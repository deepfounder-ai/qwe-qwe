"""Auto-discover LLM servers on the local network."""

import requests
import logger

_log = logger.get("discovery")

# Common LLM server defaults
SCAN_TARGETS = [
    ("localhost", 1234, "lmstudio"),
    ("127.0.0.1", 1234, "lmstudio"),
    ("localhost", 11434, "ollama"),
    ("127.0.0.1", 11434, "ollama"),
    ("localhost", 8080, "llamacpp"),
    ("127.0.0.1", 8080, "llamacpp"),
]


def discover(timeout: float = 1.5) -> list[dict]:
    """Scan known ports for LLM servers.

    Returns list of dicts: [{host, port, provider, url, models}]
    """
    found = []
    seen = set()  # avoid duplicates (localhost == 127.0.0.1)

    for host, port, provider in SCAN_TARGETS:
        url = f"http://{host}:{port}/v1"
        # Deduplicate by port (localhost and 127.0.0.1 are the same)
        if port in seen:
            continue
        try:
            resp = requests.get(f"{url}/models", timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                models = [m["id"] for m in data.get("data", [])]
                found.append({
                    "host": host,
                    "port": port,
                    "provider": provider,
                    "url": url,
                    "models": models,
                })
                seen.add(port)
                _log.info(f"found {provider} at {host}:{port} with {len(models)} models")
        except Exception:
            pass

    return found


def discover_first(timeout: float = 1.5) -> dict | None:
    """Return first found LLM server, or None."""
    results = discover(timeout=timeout)
    return results[0] if results else None
