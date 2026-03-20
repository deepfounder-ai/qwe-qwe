"""Web server for qwe-qwe — FastAPI + WebSocket chat."""

import faulthandler, sys, signal
faulthandler.enable(file=sys.stderr)

def _signal_handler(signum, frame):
    import traceback, logger as _lg
    _l = _lg.get("server")
    _l.error(f"SIGNAL {signum} received!")
    _l.error("".join(traceback.format_stack(frame)))

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

import asyncio
import hashlib
import hmac
import json
import re
import time
import os
import threading
from pathlib import Path
from contextlib import asynccontextmanager

# Global abort flag
_abort_event = threading.Event()

# Connected WebSocket clients for broadcast (thread-safe via copy-on-iterate)
import threading as _threading
_ws_clients: set = set()
_ws_lock = _threading.Lock()
_ws_loop: asyncio.AbstractEventLoop | None = None

# Module-level state for knowledge indexing
_knowledge_task: dict | None = None  # {task_id, status, current, total, file, phase, errors}
_knowledge_lock = threading.Lock()

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

import config
import db
import soul
import logger
import memory as mem
import providers
import threads

_log = logger.get("server")


def _validate_home_path(raw: str) -> Path:
    """Validate path is within user's home directory. Raises ValueError if not."""
    home = Path.home().resolve()
    p = Path(raw).expanduser().resolve()
    try:
        p.relative_to(home)
    except ValueError:
        raise ValueError(f"Access denied: path outside home directory")
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {raw}")
    return p


# ── Error formatting ──

def _friendly_error(e: Exception) -> str:
    """Convert raw exceptions to user-friendly messages."""
    err = str(e).lower()
    if "connection" in err and ("refused" in err or "failed" in err or "error" in err):
        return "⚠️ Cannot connect to LLM server. Make sure LM Studio or Ollama is running."
    if "timeout" in err:
        return "⚠️ LLM server timed out. The model may still be loading — try again in a moment."
    if "401" in err or "unauthorized" in err or "authentication" in err:
        return "⚠️ Authentication failed. Check your API key in Settings → Provider."
    if "404" in err and "model" in err:
        return "⚠️ Model not found. Load the model in LM Studio or check the model name in Settings."
    if "rate" in err and "limit" in err:
        return "⚠️ Rate limit exceeded. Wait a moment and try again."
    if "context" in err and ("length" in err or "too long" in err):
        return "⚠️ Message too long for model context. Try a shorter message or clear history."
    # Fallback: show first 200 chars of the error
    msg = str(e)
    if len(msg) > 200:
        msg = msg[:200] + "…"
    return f"⚠️ Error: {msg}"


# ── Agent runner in thread pool (agent.run is sync/blocking) ──

def _emit_agent_status(text: str):
    """Broadcast live status update from agent thread to WS clients."""
    if _ws_loop and _ws_clients:
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast({"type": "status", "text": text}), _ws_loop
            )
        except Exception:
            pass


def _emit_agent_thinking(text: str):
    """Broadcast live thinking chunk from agent thread to WS clients."""
    if _ws_loop and _ws_clients:
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast({"type": "thinking_delta", "text": text}), _ws_loop
            )
        except Exception:
            pass


def _run_agent_sync(user_input: str, thread_id: str | None = None,
                    image_b64: str | None = None,
                    image_path: str | None = None) -> dict:
    """Run agent.run() synchronously — called from thread pool."""
    import agent
    _abort_event.clear()
    agent._abort_event = _abort_event  # share abort flag with agent
    # Pass image_path so agent can store it in user message meta
    agent._pending_image_path = image_path
    # Set live status callback for tool progress
    agent._status_callback = _emit_agent_status
    agent._thinking_callback = _emit_agent_thinking
    t0 = time.time()
    result = agent.run(user_input, thread_id=thread_id, source="web", image_b64=image_b64)
    elapsed = int((time.time() - t0) * 1000)
    return {
        "reply": result.reply,
        "thinking": result.thinking,
        "tools": result.tool_calls_made,
        "duration_ms": elapsed,
        "context_hits": result.auto_context_hits,
        "thread_id": thread_id or threads.get_active_id(),
        "model_used": result.model,
    }


# ── Lifespan ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    import scheduler
    scheduler.start()
    _log.info("web server started")
    yield
    # Shutdown
    _log.info("web server stopped")


# ── App ──

app = FastAPI(title="qwe-qwe", version="0.3.0", lifespan=lifespan)

# ── Optional auth (set QWE_PASSWORD env to enable) ──
_AUTH_PASSWORD = os.environ.get("QWE_PASSWORD", "")
_AUTH_COOKIE = "qwe_auth"
_AUTH_TOKEN = hashlib.sha256(f"qwe-auth-{_AUTH_PASSWORD}".encode()).hexdigest()[:32] if _AUTH_PASSWORD else ""

# Max upload size (10 MB)
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# ── Rate limiting (in-memory, per-IP) ──
_rate_log: dict[str, list[float]] = {}  # ip -> [timestamps]
_RATE_LIMIT = 120  # requests per minute (Settings page loads many endpoints at once)


def _check_rate_limit(ip: str) -> bool:
    """Returns True if request is allowed."""
    now = time.time()
    timestamps = _rate_log.get(ip, [])
    # Remove entries older than 60s
    timestamps = [t for t in timestamps if now - t < 60]
    if not timestamps:
        _rate_log.pop(ip, None)  # evict inactive IPs to prevent memory leak
        _rate_log[ip] = [now]    # record this request
        return True
    if len(timestamps) >= _RATE_LIMIT:
        _rate_log[ip] = timestamps
        return False
    timestamps.append(now)
    _rate_log[ip] = timestamps
    return True


@app.middleware("http")
async def auth_and_rate_middleware(request: Request, call_next):
    """Optional password auth + rate limiting middleware."""
    path = request.url.path

    # Skip auth for login endpoint and static files
    if path in ("/api/login", "/static") or path.startswith("/static/"):
        return await call_next(request)

    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

    # Auth check (only if QWE_PASSWORD is set)
    if _AUTH_PASSWORD:
        # Allow login page
        if path == "/" and request.method == "GET":
            cookie = request.cookies.get(_AUTH_COOKIE, "")
            if not hmac.compare_digest(cookie, _AUTH_TOKEN):
                # Serve login page instead
                return await call_next(request)

        # API/WS require auth cookie
        if path.startswith("/api/") or path.startswith("/ws"):
            cookie = request.cookies.get(_AUTH_COOKIE, "")
            if not hmac.compare_digest(cookie, _AUTH_TOKEN):
                return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)


# Login endpoint
@app.post("/api/login")
async def login(request: Request):
    """Authenticate with password. Sets auth cookie."""
    if not _AUTH_PASSWORD:
        return {"ok": True, "message": "no password set"}
    body = await request.json()
    password = body.get("password", "")
    if hmac.compare_digest(password, _AUTH_PASSWORD):
        response = JSONResponse({"ok": True})
        response.set_cookie(_AUTH_COOKIE, _AUTH_TOKEN,
                            max_age=86400, httponly=True, samesite="strict")
        return response
    return JSONResponse({"error": "wrong password"}, status_code=401)


# Static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Uploads directory (in user data dir, safe from git)
UPLOADS_DIR = config.UPLOADS_DIR
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


# ── Routes ──

@app.post("/api/upload")
async def upload_image(request: Request):
    """Upload an image file. Returns image_id for use in chat."""
    import base64, uuid
    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        from fastapi import UploadFile
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse({"error": "no file"}, status_code=400)
        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": f"file too large (max {_MAX_UPLOAD_BYTES // 1024 // 1024}MB)"}, status_code=413)
        filename = file.filename or "image.png"
    else:
        # Raw base64 JSON: {"data": "base64...", "filename": "image.png"}
        body = await request.json()
        data = base64.b64decode(body.get("data", ""))
        if len(data) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": f"file too large (max {_MAX_UPLOAD_BYTES // 1024 // 1024}MB)"}, status_code=413)
        filename = body.get("filename", "image.png")

    # Validate it's an image
    ext = Path(filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return JSONResponse({"error": "unsupported image format"}, status_code=400)

    # Save
    image_id = str(uuid.uuid4())[:8]
    save_path = UPLOADS_DIR / f"{image_id}{ext}"
    save_path.write_bytes(data)

    # Return base64 for immediate use (no absolute path — security)
    b64 = base64.b64encode(data).decode()
    _log.info(f"image uploaded: {image_id} ({len(data)} bytes)")
    return {"image_id": image_id, "b64": b64}


@app.get("/")
async def index():
    """Serve the chat UI."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "static/index.html not found"}, status_code=404)


@app.get("/api/status")
async def status():
    """Agent status — soul, stats, skills."""
    s = soul.load()
    s_compl = int(db.kv_get("session_completion_tokens") or "0")
    s_turns = int(db.kv_get("session_turns") or "0")
    mem_count = 0
    try:
        mem_count = mem.count()
    except Exception:
        pass

    import skills
    active_skills = sorted(skills.get_active())

    active_thread = threads.get(threads.get_active_id())

    return {
        "agent": s["name"],
        "model": providers.get_model(),
        "provider": providers.get_active_name(),
        "thread": active_thread,
        "language": s["language"],
        "soul": {k: v for k, v in s.items() if k not in ("name", "language")},
        "tokens": s_compl,
        "turns": s_turns,
        "memories": mem_count,
        "skills": active_skills,
    }


def _check_version_sync() -> dict:
    """Check current version vs latest GitHub release (sync, for thread pool)."""
    # Read version from pyproject.toml (not importlib.metadata which caches stale values)
    try:
        from updater import _current_version
        current = _current_version()
    except Exception:
        try:
            import importlib.metadata
            current = importlib.metadata.version("qwe-qwe")
        except Exception:
            current = app.version

    # Check cached latest version (avoid hammering GitHub)
    cached = db.kv_get("version:latest")
    cached_at = db.kv_get("version:checked_at")
    now = time.time()

    latest = cached
    # Re-check every 6 hours
    if not cached or not cached_at or (now - float(cached_at)) > 21600:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.github.com/repos/deepfounder-ai/qwe-qwe/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "qwe-qwe"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                latest = data.get("tag_name", "").lstrip("v")
                if latest:
                    db.kv_set("version:latest", latest)
                    db.kv_set("version:checked_at", str(now))
        except Exception as e:
            _log.debug(f"version check failed: {e}")
            latest = cached  # use stale cache on error

    update_available = False
    if latest and current:
        try:
            from packaging.version import Version
            update_available = Version(latest) > Version(current)
        except Exception:
            update_available = latest != current and latest > current

    return {"current": current, "latest": latest, "update_available": update_available}


@app.get("/api/version")
async def version_check():
    """Check current version vs latest GitHub release."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _check_version_sync)


# ── Update ──

_update_status = {"running": False, "result": None}

@app.get("/api/update/check")
async def update_check():
    """Check if update is available (uses git fetch)."""
    import updater
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, updater.check)


@app.post("/api/update")
async def trigger_update():
    """Trigger a full update. Progress broadcast via WebSocket."""
    if _update_status["running"]:
        return JSONResponse({"error": "Update already in progress"}, status_code=409)

    import updater

    def _run():
        _update_status["running"] = True
        _update_status["result"] = None

        def on_progress(step, status, detail=""):
            if _ws_loop and _ws_clients:
                msg = {"type": "update_progress", "step": step, "status": status, "detail": detail}
                try:
                    asyncio.run_coroutine_threadsafe(_broadcast(msg), _ws_loop)
                except Exception:
                    pass

        try:
            result = updater.perform_update(on_progress=on_progress)
            _update_status["result"] = result
        except Exception as e:
            _update_status["result"] = {"success": False, "error": str(e)}
        finally:
            _update_status["running"] = False

        # Broadcast final result
        if _ws_loop and _ws_clients:
            try:
                asyncio.run_coroutine_threadsafe(
                    _broadcast({"type": "update_done", **(_update_status["result"] or {})}),
                    _ws_loop
                )
            except Exception:
                pass

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


@app.get("/api/update/status")
async def update_status():
    """Get current update status."""
    return {"running": _update_status["running"], "result": _update_status["result"]}


@app.post("/api/update/restart")
async def trigger_restart():
    """Restart the server process after update."""
    import updater

    async def _delayed_restart():
        await asyncio.sleep(1)  # let response reach client
        updater.restart_process()

    asyncio.ensure_future(_delayed_restart())
    return {"restarting": True}


@app.get("/api/stats")
async def stats():
    """Agent reliability and usage stats."""
    raw = db.kv_get_prefix("stats:")
    return {k.replace("stats:", ""): int(v) for k, v in raw.items()}


@app.post("/api/stats/reset")
async def stats_reset():
    """Reset all stats counters to zero."""
    raw = db.kv_get_prefix("stats:")
    for key in raw:
        db.kv_set(key, "0")
    return {"ok": True, "reset": len(raw)}


@app.get("/api/user-profile")
async def get_user_profile():
    """Get user profile data."""
    raw = db.kv_get_prefix("user:")
    return {k.replace("user:", ""): v for k, v in raw.items()}


@app.post("/api/user-profile")
async def update_user_profile(request: Request):
    """Update or delete user profile fields."""
    req = await request.json()
    if "delete" in req:
        key = req["delete"].strip().lower().replace(" ", "_")
        db.kv_set(f"user:{key}", "")
        # Actually delete by setting empty — or use raw SQL
        db.execute("DELETE FROM kv WHERE key=?", (f"user:{key}",))
        return {"ok": True, "deleted": key}
    key = req.get("key", "").strip().lower().replace(" ", "_")
    value = req.get("value", "").strip()
    if not key:
        return JSONResponse({"error": "key required"}, status_code=400)
    db.kv_set(f"user:{key}", value)
    return {"ok": True, "key": key, "value": value}


@app.get("/api/heartbeat")
async def get_heartbeat():
    """Get heartbeat config and items."""
    enabled = db.kv_get("heartbeat:enabled") != "0"  # on by default
    interval = config.get("heartbeat_interval_min")
    raw = db.kv_get("heartbeat:items")
    items = json.loads(raw) if raw else []
    return {"enabled": enabled, "interval_min": interval, "items": items}


@app.post("/api/heartbeat")
async def update_heartbeat(request: Request):
    """Manage heartbeat: add/remove items, toggle on/off."""
    import scheduler
    req = await request.json()
    action = req.get("action", "")

    if action == "add":
        text = req.get("text", "").strip()[:500]  # limit length
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        raw = db.kv_get("heartbeat:items")
        items = json.loads(raw) if raw else []
        items.append(text)
        db.kv_set("heartbeat:items", json.dumps(items))
        return {"ok": True, "items": items}

    if action == "remove":
        idx = req.get("index", -1)
        raw = db.kv_get("heartbeat:items")
        items = json.loads(raw) if raw else []
        if 0 <= idx < len(items):
            removed = items.pop(idx)
            db.kv_set("heartbeat:items", json.dumps(items))
            return {"ok": True, "removed": removed, "items": items}
        return JSONResponse({"error": "invalid index"}, status_code=400)

    if action == "toggle":
        current = db.kv_get("heartbeat:enabled") != "0"  # on by default
        new_val = not current
        db.kv_set("heartbeat:enabled", "1" if new_val else "0")
        if new_val:
            scheduler._register_heartbeat()
        else:
            scheduler._unregister_heartbeat()
        return {"ok": True, "enabled": new_val}

    return JSONResponse({"error": "unknown action"}, status_code=400)


@app.get("/api/discover")
async def discover_servers():
    """Auto-discover LLM servers on localhost."""
    import discovery
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, discovery.discover)
    return {"servers": results}


@app.get("/api/setup")
async def setup_status():
    """Check if first-run setup is complete."""
    val = db.kv_get("setup_complete")
    return {"complete": val is not None and val != ""}


@app.post("/api/setup")
async def setup_save(request: Request):
    """Save first-run onboarding data."""
    import json as _json
    try:
        req = await request.json()
    except Exception as e:
        logger.event("setup_error", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=400)

    if "tz_offset" in req:
        offset = int(req["tz_offset"])
        if not -12 <= offset <= 14:
            return JSONResponse({"error": f"Invalid timezone offset: {offset}"}, status_code=400)
        config.TZ_OFFSET = offset
        db.kv_set("timezone", str(offset))
    if req.get("tz_name"):
        db.kv_set("timezone_name", req["tz_name"])

    if req.get("user_name"):
        db.kv_set("user_name", req["user_name"].strip())
    if req.get("agent_name"):
        db.kv_set("soul:name", req["agent_name"].strip())
    if req.get("language"):
        db.kv_set("soul:language", req["language"].strip())

    # Provider setup — set key/endpoint BEFORE switching (switch validates key)
    if req.get("provider"):
        prov = req["provider"]
        if req.get("api_key"):
            providers.set_key(prov, req["api_key"])
        if req.get("endpoint"):
            import json as _j
            p = providers.get_provider(prov)
            p["url"] = req["endpoint"]
            db.kv_set(f"provider:config:{prov}", _j.dumps(p))
        # Now switch (key is already set)
        result = providers.switch(prov)
        logger.event("provider_switch", provider=prov, result=result)
        if req.get("model"):
            providers.set_model(req["model"])

    traits = req.get("traits", {})
    for key, val in traits.items():
        # Accept both levels ("low"/"moderate"/"high") and legacy numbers
        if isinstance(val, str) and val in ("low", "moderate", "high"):
            db.kv_set(f"soul:{key}", val)
        elif isinstance(val, (int, float)):
            # Convert numeric to level
            n = max(0, min(10, int(val)))
            level = "low" if n <= 3 else "moderate" if n <= 6 else "high"
            db.kv_set(f"soul:{key}", level)

    db.kv_set("setup_complete", "1")
    logger.event("setup_complete", user=req.get("user_name"), provider=req.get("provider"))
    return {"ok": True}


@app.get("/api/network")
async def network_status():
    """Get network access status."""
    lan_val = db.kv_get("network:lan_access")
    lan = lan_val != "0"
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except Exception:
        ip = "unknown"
    return {"lan_access": lan, "ip": ip, "port": _current_port}


@app.post("/api/network")
async def network_toggle(request: Request):
    """Toggle LAN access."""
    req = await request.json()
    lan = bool(req.get("lan_access", False))
    db.kv_set("network:lan_access", "1" if lan else "0")
    return {"lan_access": lan, "restart_required": True}


@app.get("/api/history")
async def history(limit: int = 20, thread_id: str | None = None):
    """Recent conversation history for a thread."""
    msgs = db.get_recent_messages(limit=limit, thread_id=thread_id)
    result = []
    for m in msgs:
        if m["role"] not in ("user", "assistant") or not m.get("content"):
            continue
        entry = {"role": m["role"], "content": m["content"]}
        if m.get("meta"):
            entry["meta"] = m["meta"]
        result.append(entry)
    return result


@app.get("/api/logs")
async def logs(file: str = "qwe-qwe.log", lines: int = 50):
    """Tail log files."""
    logs_dir = config.LOGS_DIR.resolve()
    log_path = (logs_dir / file).resolve()
    # Prevent path traversal
    if not str(log_path).startswith(str(logs_dir)):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not log_path.exists():
        return {"lines": []}
    # Read only the tail (avoid loading huge files into memory)
    all_lines = log_path.read_text(errors="replace").splitlines()
    return {"lines": all_lines[-lines:]}


@app.get("/api/secrets")
async def list_secrets():
    """List secret keys (no values!)."""
    import vault
    return vault.list_keys()


@app.delete("/api/secrets/{key}")
async def delete_secret(key: str):
    """Delete a secret."""
    import vault
    return {"result": vault.delete(key)}


@app.post("/api/abort")
async def abort_generation():
    """Abort current agent generation."""
    _abort_event.set()
    return {"ok": True}


@app.get("/api/thinking")
async def get_thinking():
    return {"enabled": db.kv_get("thinking_enabled") == "true"}


@app.post("/api/thinking")
async def set_thinking(data: dict):
    val = bool(data.get("enabled", False))
    db.kv_set("thinking_enabled", str(val).lower())
    return {"enabled": val}


@app.get("/api/soul")
async def get_soul():
    """Get soul config with trait descriptions."""
    s = soul.load()
    descs = soul.get_trait_descriptions()
    return {"values": s, "traits": descs}


@app.post("/api/soul")
async def set_soul(data: dict):
    """Update soul traits."""
    results = {}
    for key, value in data.items():
        results[key] = soul.save(key, value)
    return results


@app.post("/api/soul/traits")
async def add_soul_trait(data: dict):
    """Add a custom trait."""
    name = data.get("name", "")
    low = data.get("low", "low")
    high = data.get("high", "high")
    value = data.get("value", "moderate")
    return {"result": soul.add_trait(name, low, high, value)}


@app.delete("/api/soul/traits/{name}")
async def remove_soul_trait(name: str):
    """Remove a custom trait."""
    return {"result": soul.remove_trait(name)}


# ── Settings API ──
@app.get("/api/settings")
async def get_settings():
    """Get all editable settings."""
    return config.get_all()


@app.post("/api/settings")
async def update_settings(request: Request):
    """Update one or more settings. Body: {"key": value, ...}"""
    data = await request.json()
    results = {}
    for key, value in data.items():
        results[key] = config.set(key, value)
    return {"results": results}


# ── Provider/Model endpoints ──

@app.get("/api/providers")
async def get_providers():
    """List all providers with status."""
    return providers.list_all()


@app.get("/api/models")
async def get_models(provider: str | None = None):
    """Fetch available models from a provider."""
    return {"models": providers.fetch_models(provider), "provider": provider or providers.get_active_name()}


@app.post("/api/model")
async def set_model(data: dict):
    """Switch model and/or provider."""
    results = []
    if "provider" in data:
        results.append(providers.switch(data["provider"]))
    if "model" in data:
        results.append(providers.set_model(data["model"]))
    return {"results": results, "model": providers.get_model(), "provider": providers.get_active_name()}


@app.post("/api/provider")
async def add_provider(data: dict):
    """Add or update a provider."""
    name = data.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if "key" in data and "url" not in data:
        # Just setting a key
        return {"result": providers.set_key(name, data["key"])}
    url = data.get("url", "")
    key = data.get("key", "")
    models = data.get("models", [])
    return {"result": providers.add(name, url, key, models)}


# ── Cron/Tasks endpoints ──

@app.get("/api/cron")
async def list_cron():
    """List scheduled tasks."""
    import scheduler
    return scheduler.list_tasks()


@app.post("/api/cron")
async def add_cron(data: dict):
    """Add a scheduled task."""
    import scheduler
    result = scheduler.add(data.get("name",""), data.get("task",""), data.get("schedule",""))
    return result


@app.delete("/api/cron/{task_id}")
async def remove_cron(task_id: int):
    """Remove a scheduled task."""
    import scheduler
    return {"result": scheduler.remove(task_id)}


@app.get("/api/tasks")
async def list_tasks():
    """Get background task results."""
    import tasks as t
    return {"pending": t.pending_count(), "results": t.get_results(clear=False)}


# ── Knowledge Upload endpoints ──


def _emit_knowledge(data: dict):
    """Broadcast knowledge indexing event to WS clients."""
    if _ws_loop and _ws_clients:
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(data), _ws_loop)
        except Exception:
            pass


def _run_knowledge_index(task_id: int, files: list[dict]):
    """Background knowledge indexing thread."""
    global _knowledge_task
    import rag

    errors = []
    results = []

    def progress_cb(current, total, filepath, phase, detail=""):
        with _knowledge_lock:
            if _knowledge_task:
                _knowledge_task.update({"current": current, "total": total, "file": Path(filepath).name, "phase": phase})
        _emit_knowledge({
            "type": "knowledge_progress",
            "current": current, "total": total,
            "file": Path(filepath).name,
            "phase": phase, "detail": detail
        })

    def phase_cb(phase_type, count, estimate_sec):
        _emit_knowledge({
            "type": "knowledge_gpu_warning",
            "files_count": count,
            "estimate_sec": estimate_sec
        })

    try:
        results = rag.index_files_batch(files, progress_cb=progress_cb, phase_cb=phase_cb)
        errors = [r for r in results if r.get("status") not in ("indexed", "already up to date")]
    except Exception as e:
        _log.error(f"knowledge indexing failed: {e}", exc_info=True)
        errors.append({"error": str(e)})

    # Calculate totals
    total_chunks = sum(r.get("chunks", 0) for r in results)

    with _knowledge_lock:
        if _knowledge_task:
            _knowledge_task["status"] = "done"

    _emit_knowledge({
        "type": "knowledge_done",
        "files": len(results),
        "chunks": total_chunks,
        "errors": len(errors),
        "duration_sec": 0  # tracked client-side
    })

    # Update tasks registry
    import tasks as t
    t.update(task_id, "done", f"Indexed {len(results)} files, {total_chunks} chunks")

    with _knowledge_lock:
        _knowledge_task = None


# ── File Browser ──

@app.get("/api/files/browse")
async def file_browse(request: Request):
    """Browse directory contents for Knowledge file picker."""
    import rag

    params = request.query_params
    home = str(Path.home())
    raw_path = params.get("path", home)
    show_hidden = params.get("hidden", "false").lower() == "true"

    try:
        p = _validate_home_path(raw_path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    if not p.is_dir():
        # If it's a file, return its parent directory
        p = p.parent

    # Check depth (max 10 levels from home)
    depth = len(p.relative_to(Path.home().resolve()).parts)
    if depth > 10:
        return JSONResponse({"error": "Maximum directory depth exceeded"}, status_code=400)

    # Get indexable extensions from rag
    indexable_exts = getattr(rag, 'ALL_INDEXABLE', rag.SUPPORTED_EXTENSIONS | {'.pdf'})

    # Build items list
    items = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            name = entry.name

            # Skip hidden files unless requested
            if name.startswith('.') and not show_hidden:
                continue

            # Skip symlinks that point outside home
            if entry.is_symlink():
                try:
                    target = entry.resolve()
                    if not str(target).startswith(home):
                        continue
                except (OSError, ValueError):
                    continue

            try:
                stat = entry.stat()
            except (PermissionError, OSError):
                continue

            if entry.is_dir():
                items.append({
                    "name": name,
                    "type": "dir",
                    "size": None,
                    "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
                })
            elif entry.is_file():
                ext = entry.suffix.lower()
                items.append({
                    "name": name,
                    "type": "file",
                    "size": stat.st_size,
                    "ext": ext,
                    "indexable": ext in indexable_exts,
                    "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
                })
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    # Parent path (don't go above home)
    parent = str(p.parent) if str(p) != home else None

    return {
        "current": str(p),
        "parent": parent,
        "home": home,
        "items": items,
    }


@app.post("/api/knowledge/scan")
async def knowledge_scan(data: dict):
    """Scan a path and return file preview for indexing."""
    import rag
    path = data.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "Path required"}, status_code=400)

    try:
        p = _validate_home_path(path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    recursive = data.get("recursive", True)
    result = rag.scan_path(str(p), recursive=recursive)
    return result


@app.post("/api/knowledge/index")
async def knowledge_index(data: dict):
    """Start background indexing of selected files."""
    global _knowledge_task

    files = data.get("files", [])
    if not files:
        return JSONResponse({"error": "No files to index"}, status_code=400)

    home = str(Path.home().resolve())
    for f in files:
        fp = Path(f.get("path", "")).resolve()
        try:
            fp.relative_to(home)
        except ValueError:
            return JSONResponse({"error": f"Access denied: {f.get('path')}"}, status_code=403)

    with _knowledge_lock:
        if _knowledge_task and _knowledge_task.get("status") == "running":
            return JSONResponse({"error": "Indexing already in progress"}, status_code=409)

    import tasks as t
    task_id = t.register("knowledge_index", f"Indexing {len(files)} files")

    with _knowledge_lock:
        _knowledge_task = {
            "task_id": task_id,
            "status": "running",
            "current": 0,
            "total": len(files),
            "file": "",
            "phase": "cpu",
            "errors": []
        }

    thread = threading.Thread(target=_run_knowledge_index, args=(task_id, files), daemon=True)
    thread.start()

    return {"task_id": task_id, "status": "started", "total": len(files)}


@app.get("/api/knowledge/status")
async def knowledge_status():
    """Get current indexing status."""
    with _knowledge_lock:
        if _knowledge_task:
            return dict(_knowledge_task)
    return {"status": "idle"}


@app.get("/api/knowledge/list")
async def knowledge_list():
    """List all indexed files."""
    import rag
    return {"files": rag.list_indexed_files()}


@app.delete("/api/knowledge/file")
async def knowledge_delete(request: Request):
    """Delete a file from the index."""
    import rag
    path = request.query_params.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "Path required"}, status_code=400)

    home = str(Path.home().resolve())
    fp = Path(path).resolve()
    try:
        fp.relative_to(home)
    except ValueError:
        return JSONResponse({"error": f"Access denied: {path}"}, status_code=403)

    result = rag.delete_file(path)
    return result


# ── Skills endpoints ──

@app.get("/api/skills")
async def list_skills():
    """List all skills with status."""
    import skills
    return skills.list_all()


@app.post("/api/skills/{name}")
async def toggle_skill(name: str, data: dict):
    """Enable or disable a skill."""
    import skills
    if data.get("active"):
        return {"result": skills.enable(name)}
    else:
        return {"result": skills.disable(name)}


# ── Thread endpoints ──

@app.get("/api/threads")
async def list_threads(include_archived: bool = False):
    """List all threads with stats."""
    all_threads = threads.list_all(include_archived=include_archived)
    # Bulk-fetch thread stats in one query instead of 4N separate queries
    stats_rows = db.fetchall("""
        SELECT thread_id,
               COALESCE(SUM(LENGTH(content)), 0) / 4 AS est_tokens,
               SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_msgs,
               SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS asst_msgs,
               SUM(CASE WHEN role='tool' THEN 1 ELSE 0 END) AS tool_msgs
        FROM messages GROUP BY thread_id
    """)
    stats = {r[0]: {"est_tokens": r[1], "user_messages": r[2],
                     "assistant_messages": r[3], "tool_calls": r[4]} for r in stats_rows}
    for t in all_threads:
        s = stats.get(t["id"], {})
        t["est_tokens"] = s.get("est_tokens", 0)
        t["user_messages"] = s.get("user_messages", 0)
        t["assistant_messages"] = s.get("assistant_messages", 0)
        t["tool_calls"] = s.get("tool_calls", 0)
    return all_threads


@app.post("/api/threads")
async def create_thread(data: dict):
    """Create a new thread."""
    name = data.get("name", "New Thread")
    t = threads.create(name, meta=data.get("meta"))
    # Seed message — branch from existing conversation
    seed = data.get("seed_message")
    if seed:
        db.save_message("assistant", seed, thread_id=t["id"])
    return t


@app.get("/api/threads/{thread_id}/stats")
async def thread_stats(thread_id: str):
    """Get stats for a specific thread."""
    t = threads.get(thread_id)
    if not t:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Count user/assistant messages
    user_msgs = db.fetchone("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='user'", (thread_id,))[0]
    asst_msgs = db.fetchone("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='assistant'", (thread_id,))[0]
    tool_msgs = db.fetchone("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'", (thread_id,))[0]
    # First and last message time
    first = db.fetchone("SELECT ts FROM messages WHERE thread_id=? ORDER BY id ASC LIMIT 1", (thread_id,))
    last = db.fetchone("SELECT ts FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,))
    # Estimate tokens from content length (rough: 1 token ≈ 4 chars)
    row = db.fetchone("SELECT COALESCE(SUM(LENGTH(content)),0) FROM messages WHERE thread_id=?", (thread_id,))
    est_tokens = row[0] // 4 if row else 0

    return {
        "thread_id": thread_id, "name": t["name"],
        "user_messages": user_msgs, "assistant_messages": asst_msgs,
        "tool_calls": tool_msgs, "total_messages": t["messages"],
        "est_tokens": est_tokens,
        "created_at": t["created_at"],
        "first_message": first[0] if first else None,
        "last_message": last[0] if last else None,
        "model": t.get("meta", {}).get("model"),
    }


@app.post("/api/threads/{thread_id}/model")
async def set_thread_model(thread_id: str, request: Request):
    """Set a model override for a specific thread."""
    req = await request.json()
    model = req.get("model", "")
    t = threads.get(thread_id)
    if not t:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = t.get("meta", {})
    if model:
        meta["model"] = model
    else:
        meta.pop("model", None)  # clear override
    import json as _j
    db.execute("UPDATE threads SET meta=? WHERE id=?", (_j.dumps(meta), thread_id))
    return {"ok": True, "model": model or None}


@app.get("/api/threads/active")
async def active_thread():
    """Get the active thread."""
    tid = threads.get_active_id()
    return threads.get(tid) or {"id": tid, "name": "Unknown"}


@app.post("/api/threads/{thread_id}/switch")
async def switch_thread(thread_id: str):
    """Switch active thread."""
    result = threads.switch(thread_id)
    return {"result": result, "active": threads.get_active_id()}


@app.put("/api/threads/{thread_id}")
async def update_thread(thread_id: str, data: dict):
    """Rename or update a thread."""
    results = {}
    if "name" in data:
        results["rename"] = threads.rename(thread_id, data["name"])
    if data.get("archived"):
        results["archive"] = threads.archive(thread_id)
    return results


@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str):
    """Delete a thread and its messages."""
    return {"result": threads.delete(thread_id)}


# ── WebSocket chat ──

async def _ws_send_safe(ws: WebSocket, data: dict) -> bool:
    """Send JSON to a WebSocket, returning False if the connection is dead."""
    try:
        await ws.send_json(data)
        return True
    except (WebSocketDisconnect, ConnectionResetError, RuntimeError, OSError):
        return False
    except Exception:
        _log.debug("ws send failed", exc_info=True)
        return False


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    global _ws_loop

    # Auth check for WebSocket (middleware doesn't cover WS)
    if _AUTH_PASSWORD:
        cookie = ws.cookies.get(_AUTH_COOKIE, "")
        if not hmac.compare_digest(cookie, _AUTH_TOKEN):
            await ws.close(code=4001, reason="unauthorized")
            return

    await ws.accept()
    with _ws_lock:
        _ws_clients.add(ws)
    _ws_loop = asyncio.get_event_loop()
    _log.info(f"websocket client connected ({len(_ws_clients)} total)")

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                user_input = msg.get("text", "").strip()
                thread_id = msg.get("thread_id")  # optional — None uses active
                image_b64 = msg.get("image_b64")  # optional base64 image
            except json.JSONDecodeError:
                user_input = data.strip()
                thread_id = None
                image_b64 = None

            if not user_input and not image_b64:
                continue

            # Save image to uploads/ so it persists in history
            image_path = None
            if image_b64:
                try:
                    import uuid as _uuid
                    img_id = str(_uuid.uuid4())[:8]
                    img_file = UPLOADS_DIR / f"{img_id}.png"
                    img_file.write_bytes(base64.b64decode(image_b64))
                    image_path = f"/uploads/{img_id}.png"
                except Exception as e:
                    _log.warning(f"failed to save ws image: {e}")

            _log.info(f"ws message: thread={thread_id or 'active'} | {user_input[:100]}" +
                       (" [+image]" if image_b64 else ""))

            # Check if model needs loading
            loading_msg = None
            if providers.get_active_name() in ("lmstudio", "ollama"):
                import requests as _req
                try:
                    p = providers.get_provider()
                    api_base = p.get("url", "").rstrip("/").replace("/v1", "")
                    model = providers.get_model()
                    r = _req.get(f"{api_base}/api/v1/models", timeout=5)
                    if r.ok:
                        models_data = r.json().get("models", [])
                        model_loaded = any(
                            m.get("key") == model and m.get("loaded_instances")
                            for m in models_data
                        )
                        if not model_loaded:
                            loading_msg = f"Loading model {model}..."
                except Exception:
                    pass

            # Send status — abort if client disconnected
            if not await _ws_send_safe(ws, {"type": "status", "text": loading_msg or "thinking..."}):
                break

            try:
                # Run agent in thread pool (it's blocking)
                loop = asyncio.get_event_loop()
                import functools
                result = await loop.run_in_executor(
                    None, functools.partial(_run_agent_sync, user_input,
                                            thread_id, image_b64=image_b64,
                                            image_path=image_path)
                )

                # Send reply — abort if client disconnected
                if not await _ws_send_safe(ws, {
                    "type": "reply",
                    "text": result["reply"],
                    "thinking": result.get("thinking", ""),
                    "tools": result["tools"],
                    "duration_ms": result["duration_ms"],
                    "context_hits": result["context_hits"],
                    "thread_id": result["thread_id"],
                }):
                    break

            except Exception as e:
                _log.error(f"ws agent error: {e}", exc_info=True)
                user_msg = _friendly_error(e)
                if not await _ws_send_safe(ws, {"type": "error", "text": user_msg}):
                    break

    except WebSocketDisconnect:
        pass
    except (ConnectionResetError, RuntimeError, OSError):
        _log.debug("websocket connection reset")
    except Exception as e:
        _log.error(f"websocket error: {e}", exc_info=True)
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)
        _log.info(f"websocket client disconnected ({len(_ws_clients)} left)")


# ── Broadcast to WS clients ──

async def _broadcast(msg: dict):
    """Send JSON to all connected WebSocket clients."""
    dead = set()
    with _ws_lock:
        clients = list(_ws_clients)  # snapshot under lock
    for ws in clients:
        if not await _ws_send_safe(ws, msg):
            dead.add(ws)
    if dead:
        with _ws_lock:
            _ws_clients -= dead


def _cron_callback(name: str, task: str, result: str):
    """Called from scheduler thread when a cron task completes."""
    # Heartbeat: suppress silent OK results
    if name == "__heartbeat__" and "HEARTBEAT_OK" in result:
        _log.debug("heartbeat OK — silent")
        return

    # WebSocket notification
    if _ws_loop and _ws_clients:
        msg = {
            "type": "cron",
            "name": name,
            "task": task,
            "text": result,
        }
        asyncio.run_coroutine_threadsafe(_broadcast(msg), _ws_loop)

    # Telegram notification
    if telegram_bot.is_verified() and telegram_bot._running:
        owner = telegram_bot.get_owner_id()
        if owner:
            truncated = result[:500] + ("..." if len(result) > 500 else "")
            # If result already has reminder format, send as-is; otherwise wrap with task name
            if result.startswith("🔔"):
                telegram_bot.send_message(owner, truncated)
            else:
                telegram_bot.send_message(owner, f"⏰ **{name}**\n{truncated}")


# Register cron callback
import scheduler
scheduler.on_complete(_cron_callback)


# ── Compaction notifications ──
import agent as _agent

def _compaction_callback(event: str, data: dict):
    """Notify WS clients and Telegram about compaction events."""
    # WebSocket notification
    if _ws_loop and _ws_clients:
        ws_msg = {"type": "compaction", "event": event, **data}
        asyncio.run_coroutine_threadsafe(_broadcast(ws_msg), _ws_loop)

    # Telegram notification — send to the same chat/topic where compaction happened
    if event == "start":
        _tg_notify_thread(data, f"🔄 Compacting memory: {data.get('messages', 0)} messages (~{data.get('tokens', 0)} tokens)...")
    elif event == "summary":
        summary = data.get("summary", "")[:200]
        _tg_notify_thread(data, f"🧠 Saved to memory:\n_{summary}_")
    elif event == "done":
        _tg_notify_thread(data, f"✅ Compaction done. {data.get('remaining', 0)} messages remaining.")
    elif event == "error":
        _tg_notify_thread(data, f"⚠️ Compaction error: {data.get('error', '')[:100]}")


def _tg_notify_thread(data: dict, text: str):
    """Send notification to the Telegram chat/topic where the thread lives."""
    if not telegram_bot.is_verified() or not telegram_bot._running:
        return

    thread_id = data.get("thread_id")
    chat_id = None
    topic_id = None

    # Try to get Telegram chat/topic from thread metadata
    if thread_id:
        t = threads.get(thread_id)
        if t and t.get("meta"):
            chat_id = t["meta"].get("telegram_chat_id")
            topic_id = t["meta"].get("telegram_topic_id")

    # Fallback to owner DM
    if not chat_id:
        chat_id = telegram_bot.get_owner_id()

    if chat_id:
        telegram_bot.send_message(chat_id, text, topic_id=topic_id)


_agent.on_compaction(_compaction_callback)

# ── Telegram Bot ──
import telegram_bot


def _telegram_handler(chat_id: int, text: str, user_id: int, username: str,
                      thread_id: str | None = None, image_b64: str | None = None) -> str:
    """Handle incoming Telegram message → agent → response."""
    import agent
    tid = thread_id or db.kv_get("telegram:thread_id") or None
    result = agent.run(text, thread_id=tid, source="telegram", image_b64=image_b64)

    # Also broadcast to WebSocket
    if _ws_loop and _ws_clients:
        ws_msg = {
            "type": "telegram",
            "from": f"@{username}" if username else str(user_id),
            "text": text,
            "reply": result.reply,
        }
        asyncio.run_coroutine_threadsafe(_broadcast(ws_msg), _ws_loop)

    return result.reply


# ── Inference setup endpoints ──

@app.get("/api/inference/status")
async def inference_status():
    """Detect hardware and check Ollama status."""
    import inference_setup
    gpu = inference_setup.detect_gpu()
    recommended = inference_setup.recommend_model(gpu)
    ollama_installed = inference_setup._check_ollama_installed()
    ollama_running = inference_setup._check_ollama_running() if ollama_installed else False

    # List available models from Ollama if running
    available_models = []
    if ollama_running:
        try:
            import requests as _req
            r = _req.get("http://localhost:11434/api/tags", timeout=3)
            if r.ok:
                available_models = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            pass

    models = [
        {"tag": "qwen3.5:0.8b", "size": "0.8B", "ram": "~1GB", "desc": "Minimal, very fast"},
        {"tag": "qwen3.5:2b", "size": "2B", "ram": "~2.7GB", "desc": "Light, basic tasks"},
        {"tag": "qwen3.5:4b", "size": "4B", "ram": "~3.4GB", "desc": "Good balance"},
        {"tag": "qwen3.5:9b", "size": "9B", "ram": "~6.6GB", "desc": "Best quality/speed ratio"},
        {"tag": "qwen3.5:27b", "size": "27B", "ram": "~17GB", "desc": "High quality, 24GB+"},
        {"tag": "qwen3.5:35b", "size": "35B", "ram": "~24GB", "desc": "Maximum, 48GB+"},
    ]

    return {
        "gpu": gpu,
        "recommended": recommended,
        "ollama_installed": ollama_installed,
        "ollama_running": ollama_running,
        "available_models": available_models,
        "models": models,
    }


_pull_status: dict = {}  # model -> {status, progress, detail}


@app.post("/api/inference/pull")
async def inference_pull(request: Request):
    """Pull a model via Ollama with progress tracking."""
    data = await request.json()
    model = data.get("model", "")
    if not model:
        return JSONResponse({"error": "model required"}, status_code=400)

    import inference_setup
    if not inference_setup._check_ollama_installed():
        return JSONResponse({"error": "Ollama not installed. Run: qwe-qwe --setup-inference"}, status_code=400)
    if not inference_setup._check_ollama_running():
        inference_setup.start_ollama()

    _pull_status[model] = {"status": "pulling", "progress": 0, "detail": "Starting..."}

    import threading
    def _do_pull():
        import requests as _req
        try:
            resp = _req.post("http://localhost:11434/api/pull",
                             json={"name": model}, stream=True, timeout=600)
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    total = d.get("total", 0)
                    completed = d.get("completed", 0)
                    pct = int(completed / total * 100) if total else 0
                    _pull_status[model] = {
                        "status": "pulling", "progress": pct,
                        "detail": f"{d.get('status', '')} {pct}%"
                    }
                except Exception:
                    pass
            _pull_status[model] = {"status": "done", "progress": 100, "detail": "Complete"}
        except Exception as e:
            _pull_status[model] = {"status": "error", "progress": 0, "detail": str(e)[:100]}

    threading.Thread(target=_do_pull, daemon=True).start()
    return {"ok": True, "message": f"Pulling {model}..."}


@app.get("/api/inference/pull-status")
async def inference_pull_status(model: str = ""):
    """Get pull progress for a model."""
    return _pull_status.get(model, {"status": "idle", "progress": 0, "detail": ""})


@app.post("/api/inference/configure")
async def inference_configure(request: Request):
    """Configure qwe-qwe to use Ollama with selected model."""
    data = await request.json()
    model = data.get("model", "")
    if not model:
        return JSONResponse({"error": "model required"}, status_code=400)

    import inference_setup
    inference_setup.configure_provider(model)
    return {"ok": True, "message": f"Configured: ollama / {model}"}


@app.get("/api/telegram/status")
async def telegram_status():
    return telegram_bot.status()


@app.post("/api/telegram/config")
async def telegram_config(request: Request):
    req = await request.json()
    if "token" in req:
        telegram_bot.set_token(req["token"])
        me = telegram_bot.get_me(req["token"])
        if not me:
            return JSONResponse({"error": "Invalid token"}, status_code=400)
        return {"ok": True, "username": me.get("username")}
    if "group_mode" in req:
        telegram_bot.set_group_mode(req["group_mode"])
    if "topics_enabled" in req:
        telegram_bot.set_topics_enabled(bool(req["topics_enabled"]))
    if "allowed_groups" in req:
        telegram_bot.set_allowed_groups(req["allowed_groups"])
    return {"ok": True}


@app.post("/api/telegram/toggle")
async def telegram_toggle(request: Request):
    req = await request.json()
    enabled = bool(req.get("enabled", False))
    telegram_bot.set_enabled(enabled)
    if enabled:
        telegram_bot.start(on_message=_telegram_handler)
    else:
        telegram_bot.stop()
    return {"enabled": enabled, "running": telegram_bot._running}


@app.post("/api/telegram/activate")
async def telegram_activate(request: Request):
    """Generate a new activation code. User sends this code to the bot in Telegram."""
    # Require auth even if global password is not set — activation codes are sensitive
    if _AUTH_PASSWORD:
        cookie = request.cookies.get(_AUTH_COOKIE, "")
        if not hmac.compare_digest(cookie, _AUTH_TOKEN):
            return JSONResponse({"error": "Auth required for telegram activation"}, status_code=401)
    if telegram_bot.is_verified():
        return JSONResponse({"error": "Already verified. Reset first to re-verify."}, status_code=400)
    code = telegram_bot.generate_activation_code()
    return {
        "ok": True,
        "code": code,
        "ttl_seconds": telegram_bot.ACTIVATION_TTL,
        "message": f"Send this code to the bot in Telegram: {code}"
    }


@app.post("/api/telegram/verify")
async def telegram_verify(request: Request):
    """Verify ownership with the code (legacy endpoint, redirects to activate)."""
    req = await request.json()
    code = req.get("code", "")
    if telegram_bot.verify_code(code):
        return {"ok": True, "verified": True}
    return JSONResponse({"error": "Invalid code"}, status_code=400)


@app.post("/api/telegram/reset")
async def telegram_reset():
    """Reset verification — disconnect owner."""
    db.kv_set("telegram:owner_id", "")
    db.kv_set("telegram:owner_username", "")
    telegram_bot.clear_verification()
    return {"ok": True}


# ── Run ──

_current_port = 7860

def start(host: str = "0.0.0.0", port: int = 7860):
    """Start the web server."""
    global _current_port
    _current_port = port
    import uvicorn

    # Check LAN access setting (default: on for backward compat)
    lan_val = db.kv_get("network:lan_access")
    lan = lan_val != "0"  # default on, only off if explicitly "0"
    actual_host = "0.0.0.0" if lan else "127.0.0.1"
    # CLI flag overrides
    if host != "0.0.0.0":
        actual_host = host

    # Auto-start Telegram bot if enabled
    if telegram_bot.is_enabled() and telegram_bot.get_token():
        telegram_bot.start(on_message=_telegram_handler)

    _log.info(f"starting web server on {actual_host}:{port} (LAN: {'on' if lan else 'off'})")
    if actual_host == "0.0.0.0":
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            print(f"\n  ⚡ qwe-qwe web UI → http://localhost:{port}")
            print(f"  📱 LAN access → http://{ip}:{port}\n")
        except Exception:
            print(f"\n  ⚡ qwe-qwe web UI → http://localhost:{port}\n")
    else:
        print(f"\n  ⚡ qwe-qwe web UI → http://localhost:{port}")
        print(f"  🔒 Local only (enable LAN in Settings → System)\n")

    uvicorn.run(app, host=actual_host, port=port, log_level="warning")


if __name__ == "__main__":
    start()
