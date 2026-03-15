"""Web server for qwe-qwe — FastAPI + WebSocket chat."""

import asyncio
import json
import re
import time
import os
import threading
from pathlib import Path
from contextlib import asynccontextmanager

# Global abort flag
_abort_event = threading.Event()

# Connected WebSocket clients for broadcast
_ws_clients: set = set()
_ws_loop: asyncio.AbstractEventLoop | None = None

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

# ── Agent runner in thread pool (agent.run is sync/blocking) ──

def _run_agent_sync(user_input: str, thread_id: str | None = None,
                    image_b64: str | None = None) -> dict:
    """Run agent.run() synchronously — called from thread pool."""
    import agent
    _abort_event.clear()
    agent._abort_event = _abort_event  # share abort flag with agent
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

app = FastAPI(title="qwe-qwe", version="0.2.0", lifespan=lifespan)

# ── Optional auth (set QWE_PASSWORD env to enable) ──
_AUTH_PASSWORD = os.environ.get("QWE_PASSWORD", "")
_AUTH_COOKIE = "qwe_auth"

# ── Rate limiting (in-memory, per-IP) ──
_rate_log: dict[str, list[float]] = {}  # ip -> [timestamps]
_RATE_LIMIT = 30  # requests per minute


def _check_rate_limit(ip: str) -> bool:
    """Returns True if request is allowed."""
    now = time.time()
    timestamps = _rate_log.get(ip, [])
    # Remove entries older than 60s
    timestamps = [t for t in timestamps if now - t < 60]
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
            cookie = request.cookies.get(_AUTH_COOKIE)
            if cookie != _AUTH_PASSWORD:
                # Serve login page instead
                return await call_next(request)

        # API/WS require auth cookie
        if path.startswith("/api/") or path.startswith("/ws"):
            cookie = request.cookies.get(_AUTH_COOKIE)
            if cookie != _AUTH_PASSWORD:
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
    if password == _AUTH_PASSWORD:
        response = JSONResponse({"ok": True})
        response.set_cookie(_AUTH_COOKIE, _AUTH_PASSWORD,
                            max_age=86400, httponly=True, samesite="lax")
        return response
    return JSONResponse({"error": "wrong password"}, status_code=401)


# Static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Uploads directory
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)


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
        data = await file.read()
        filename = file.filename or "image.png"
    else:
        # Raw base64 JSON: {"data": "base64...", "filename": "image.png"}
        body = await request.json()
        data = base64.b64decode(body.get("data", ""))
        filename = body.get("filename", "image.png")

    # Validate it's an image
    ext = Path(filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return JSONResponse({"error": "unsupported image format"}, status_code=400)

    # Save
    image_id = str(uuid.uuid4())[:8]
    save_path = UPLOADS_DIR / f"{image_id}{ext}"
    save_path.write_bytes(data)

    # Return base64 for immediate use
    b64 = base64.b64encode(data).decode()
    _log.info(f"image uploaded: {image_id} ({len(data)} bytes)")
    return {"image_id": image_id, "b64": b64, "path": str(save_path)}


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
        conn = db._get_conn()
        conn.execute("DELETE FROM kv WHERE key=?", (f"user:{key}",))
        conn.commit()
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
        text = req.get("text", "").strip()
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
        if isinstance(val, (int, float)):
            db.kv_set(f"soul:{key}", str(max(0, min(10, int(val)))))

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
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
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
    return [{"role": m["role"], "content": m.get("content", "")} for m in msgs
            if m["role"] in ("user", "assistant") and m.get("content")]


@app.get("/api/logs")
async def logs(file: str = "qwe-qwe.log", lines: int = 50):
    """Tail log files."""
    log_path = Path(__file__).parent / "logs" / file
    if not log_path.exists():
        return {"lines": []}
    all_lines = log_path.read_text().splitlines()
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
    value = data.get("value", 5)
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
    conn = db._get_conn()
    for t in all_threads:
        tid = t["id"]
        row = conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM messages WHERE thread_id=?", (tid,)).fetchone()
        t["est_tokens"] = row[0] // 4 if row else 0
        t["user_messages"] = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='user'", (tid,)).fetchone()[0]
        t["assistant_messages"] = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='assistant'", (tid,)).fetchone()[0]
        t["tool_calls"] = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'", (tid,)).fetchone()[0]
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
    conn = db._get_conn()
    # Count user/assistant messages
    user_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='user'", (thread_id,)).fetchone()[0]
    asst_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='assistant'", (thread_id,)).fetchone()[0]
    tool_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'", (thread_id,)).fetchone()[0]
    # First and last message time
    first = conn.execute("SELECT ts FROM messages WHERE thread_id=? ORDER BY id ASC LIMIT 1", (thread_id,)).fetchone()
    last = conn.execute("SELECT ts FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone()
    # Estimate tokens from content length (rough: 1 token ≈ 4 chars)
    row = conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()
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
    conn = db._get_conn()
    import json as _j
    conn.execute("UPDATE threads SET meta=? WHERE id=?", (_j.dumps(meta), thread_id))
    conn.commit()
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
    await ws.accept()
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
                                            thread_id, image_b64=image_b64)
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
                if not await _ws_send_safe(ws, {"type": "error", "text": str(e)}):
                    break

    except WebSocketDisconnect:
        pass
    except (ConnectionResetError, RuntimeError, OSError):
        _log.debug("websocket connection reset")
    except Exception as e:
        _log.error(f"websocket error: {e}", exc_info=True)
    finally:
        _ws_clients.discard(ws)
        _log.info(f"websocket client disconnected ({len(_ws_clients)} left)")


# ── Broadcast to WS clients ──

async def _broadcast(msg: dict):
    """Send JSON to all connected WebSocket clients."""
    dead = set()
    for ws in _ws_clients:
        if not await _ws_send_safe(ws, msg):
            dead.add(ws)
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
async def telegram_activate():
    """Generate a new activation code. User sends this code to the bot in Telegram."""
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
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
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
