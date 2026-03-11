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

_log = logger.get("server")

# ── Agent runner in thread pool (agent.run is sync/blocking) ──

def _run_agent_sync(user_input: str) -> dict:
    """Run agent.run() synchronously — called from thread pool."""
    import agent
    t0 = time.time()
    result = agent.run(user_input)
    elapsed = int((time.time() - t0) * 1000)
    return {
        "reply": result.reply,
        "tools": result.tool_calls_made,
        "duration_ms": elapsed,
        "context_hits": result.auto_context_hits,
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
        return FileResponse(index_path)
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

    return {
        "agent": s["name"],
        "model": providers.get_model(),
        "provider": providers.get_active_name(),
        "language": s["language"],
        "soul": {k: v for k, v in s.items() if k not in ("name", "language")},
        "tokens": s_compl,
        "turns": s_turns,
        "memories": mem_count,
        "skills": active_skills,
    }


@app.get("/api/history")
async def history(limit: int = 20):
    """Recent conversation history."""
    msgs = db.get_recent_messages(limit=limit)
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


@app.get("/api/soul")
async def get_soul():
    """Get soul config."""
    return soul.load()


@app.post("/api/soul")
async def set_soul(data: dict):
    """Update soul traits."""
    results = {}
    for key, value in data.items():
        results[key] = soul.save(key, value)
    return results


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
            except json.JSONDecodeError:
                user_input = data.strip()

            if not user_input:
                continue

            _log.info(f"ws message: {user_input[:100]}")

            # Send "thinking" status
            await ws.send_json({"type": "status", "text": "thinking..."})

            try:
                # Run agent in thread pool (it's blocking)
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, _run_agent_sync, user_input)

                # Send reply
                await ws.send_json({
                    "type": "reply",
                    "text": result["reply"],
                    "tools": result["tools"],
                    "duration_ms": result["duration_ms"],
                    "context_hits": result["context_hits"],
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
