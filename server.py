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

def _run_agent_sync(user_input: str, thread_id: str | None = None) -> dict:
    """Run agent.run() synchronously — called from thread pool."""
    import agent
    _abort_event.clear()
    agent._abort_event = _abort_event  # share abort flag with agent
    t0 = time.time()
    result = agent.run(user_input, thread_id=thread_id)
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

# Static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Routes ──

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
            except json.JSONDecodeError:
                user_input = data.strip()
                thread_id = None

            if not user_input:
                continue

            _log.info(f"ws message: thread={thread_id or 'active'} | {user_input[:100]}")

            # Send "thinking" status
            await ws.send_json({"type": "status", "text": "thinking..."})

            try:
                # Run agent in thread pool (it's blocking)
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, _run_agent_sync, user_input, thread_id
                )

                # Send reply
                await ws.send_json({
                    "type": "reply",
                    "text": result["reply"],
                    "thinking": result.get("thinking", ""),
                    "tools": result["tools"],
                    "duration_ms": result["duration_ms"],
                    "context_hits": result["context_hits"],
                    "thread_id": result["thread_id"],
                })

            except Exception as e:
                _log.error(f"ws agent error: {e}", exc_info=True)
                await ws.send_json({"type": "error", "text": str(e)})

    except WebSocketDisconnect:
        _ws_clients.discard(ws)
        _log.info(f"websocket client disconnected ({len(_ws_clients)} left)")
    except Exception as e:
        _ws_clients.discard(ws)
        _log.error(f"websocket error: {e}", exc_info=True)


# ── Broadcast to WS clients ──

async def _broadcast(msg: dict):
    """Send JSON to all connected WebSocket clients."""
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


def _cron_callback(name: str, task: str, result: str):
    """Called from scheduler thread when a cron task completes."""
    if not _ws_loop or not _ws_clients:
        return
    msg = {
        "type": "cron",
        "name": name,
        "task": task,
        "text": result,
    }
    asyncio.run_coroutine_threadsafe(_broadcast(msg), _ws_loop)


# Register cron callback
import scheduler
scheduler.on_complete(_cron_callback)

# ── Telegram Bot ──
import telegram_bot


def _telegram_handler(chat_id: int, text: str, user_id: int, username: str) -> str:
    """Handle incoming Telegram message → agent → response."""
    import agent
    # Use a dedicated thread for Telegram or default
    tid = db.kv_get("telegram:thread_id")
    result = agent.run(text, thread_id=tid or None)

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
        # Verify token
        me = telegram_bot.get_me(req["token"])
        if not me:
            return JSONResponse({"error": "Invalid token"}, status_code=400)
        return {"ok": True, "username": me.get("username")}
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
