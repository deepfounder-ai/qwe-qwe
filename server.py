"""Web server for qwe-qwe — FastAPI + WebSocket chat."""

import asyncio
import json
import re
import time
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
    """List all threads."""
    return threads.list_all(include_archived=include_archived)


@app.post("/api/threads")
async def create_thread(data: dict):
    """Create a new thread."""
    name = data.get("name", "New Thread")
    t = threads.create(name, meta=data.get("meta"))
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
    return {
        "thread_id": thread_id, "name": t["name"],
        "user_messages": user_msgs, "assistant_messages": asst_msgs,
        "tool_calls": tool_msgs, "total_messages": t["messages"],
        "created_at": t["created_at"],
        "first_message": first[0] if first else None,
        "last_message": last[0] if last else None,
    }


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
    await ws.accept()
    _log.info("websocket client connected")

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
        _log.info("websocket client disconnected")
    except Exception as e:
        _log.error(f"websocket error: {e}", exc_info=True)


# ── Run ──

def start(host: str = "0.0.0.0", port: int = 7860):
    """Start the web server."""
    import uvicorn
    _log.info(f"starting web server on {host}:{port}")
    print(f"\n  ⚡ qwe-qwe web UI → http://localhost:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    start()
