"""Web server for qwe-qwe — FastAPI + WebSocket chat."""

import faulthandler
import sys
import signal
faulthandler.enable(file=sys.stderr)

def _signal_handler(signum, frame):
    import traceback
    import logger as _lg
    _l = _lg.get("server")
    _l.error(f"SIGNAL {signum} received!")
    _l.error("".join(traceback.format_stack(frame)))

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

import asyncio
import base64
import functools
import hashlib
import hmac
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import time
import os
import threading
import urllib.request
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import requests
# Hoisted from former per-route imports. Keeps SyntaxErrors/ImportErrors at
# startup rather than on the first API call, prevents UnboundLocalError from
# half-shadowed names (see v0.17.7 `subprocess` bug), and trims first-hit
# latency. `rag`, `tasks`, `presets`, `skills`, `mcp_client`, `scheduler`,
# `stt`, `tts`, `updater`, `discovery`, `vault` are all light at module load
# (heavy init is lazy inside their own functions).
import discovery
import mcp_client
import presets
import rag
import scheduler
import skills
import stt
import tasks
import tools
import tts
import updater
import vault

# Global abort flag — used by legacy REST callers of _run_agent_sync() that
# don't supply their own per-request event (e.g. the /api/abort endpoint).
# WebSocket sessions create their own events to avoid cross-source abort.
_abort_event = threading.Event()

# Connected WebSocket clients for broadcast (thread-safe via copy-on-iterate)
import threading as _threading
_ws_clients: set = set()
_ws_lock = _threading.Lock()
_ws_loop: asyncio.AbstractEventLoop | None = None

# Active per-session WS abort events — /api/abort fires all of these so the
# "Stop" button still works from any client, while disconnect only fires one.
_ws_abort_events: set[threading.Event] = set()
_ws_abort_lock = threading.Lock()

# Module-level state for knowledge indexing
_knowledge_task: dict | None = None  # {task_id, status, current, total, file, phase, errors}
_knowledge_lock = threading.Lock()

# Recent indexings (last 20, newest first) — powers the "Recent activity" card
# in the Memory view. Each entry: {kind, label, status, chunks, duration_sec,
# converter, errors, url/path, ts}.
from collections import deque as _deque
_knowledge_history: _deque = _deque(maxlen=20)


def _push_history(entry: dict):
    """Record a completed indexing run for /api/knowledge/recent."""
    entry.setdefault("ts", time.time())
    _knowledge_history.appendleft(entry)

# Camera frame request/response system — allows agent tools to capture frames on demand
_pending_frame_requests: dict = {}  # request_id → asyncio.Event + result

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

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


def _emit_agent_content(text: str):
    """Broadcast live content (reply) chunk from agent thread to WS clients."""
    if _ws_loop and _ws_clients:
        try:
            asyncio.run_coroutine_threadsafe(
                _broadcast({"type": "content_delta", "text": text}), _ws_loop
            )
        except Exception:
            pass


def _run_agent_sync(user_input: str, thread_id: str | None = None,
                    image_b64: str | None = None,
                    image_path: str | None = None,
                    file_meta: dict | None = None,
                    abort_event: threading.Event | None = None) -> dict:
    """Run agent.run() synchronously — called from thread pool.

    ``abort_event``: per-request event. When None, the legacy shared module
    event is used (for /api/abort + older callers). WebSocket sessions pass
    their own event so one client's disconnect doesn't abort another's turn.
    """
    import agent
    if abort_event is None:
        # Legacy path (e.g. REST callers): keep using the module global and
        # the /api/abort endpoint. Clear it so stale flags don't fire.
        _abort_event.clear()
        abort_event = _abort_event
    else:
        abort_event.clear()
    agent._abort_event = _abort_event  # retained for older code paths
    # Pass image_path / file_meta so agent can store them in user message meta
    agent._pending_image_path = image_path
    agent._pending_file = file_meta
    # Set live callbacks (only if not already set by caller, e.g. _run_with_queue)
    if not agent._status_callback:
        agent._status_callback = _emit_agent_status
    if not agent._thinking_callback:
        agent._thinking_callback = _emit_agent_thinking
    if not agent._content_callback:
        agent._content_callback = _emit_agent_content
    t0 = time.time()
    try:
        result = agent.run(user_input, thread_id=thread_id, source="web",
                           image_b64=image_b64, abort_event=abort_event)
    finally:
        # Clear so next turn without a file doesn't inherit it
        agent._pending_image_path = None
        agent._pending_file = None
    elapsed = int((time.time() - t0) * 1000)
    return {
        "reply": result.reply,
        "thinking": result.thinking,
        "tools": result.tool_calls_made,
        "duration_ms": elapsed,
        "context_hits": result.auto_context_hits,
        "thread_id": thread_id or threads.get_active_id(),
        "model_used": result.model,
        "tokens": getattr(result, "completion_tokens", 0),
        "prompt_tokens": getattr(result, "prompt_tokens", 0),
        "tok_per_sec": getattr(result, "tok_per_sec", 0),
    }


# ── Lifespan ──

def _sweep_uploads(max_age_days: int = 14, max_files: int = 10000) -> tuple[int, int]:
    """Delete files under config.UPLOADS_DIR older than ``max_age_days``.

    Protects ``uploads/kb/`` — those files back indexed knowledge sources and
    must live until the user deletes them from the KB list. Bounded at
    ``max_files`` inspected files so a pathological uploads dir can't stall
    startup. Returns (files_deleted, bytes_freed).
    """
    upl = config.UPLOADS_DIR
    if not upl.exists():
        return 0, 0
    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    bytes_freed = 0
    inspected = 0
    kb_dir = (upl / "kb").resolve()

    try:
        for p in upl.rglob("*"):
            inspected += 1
            if inspected > max_files:
                _log.warning(f"uploads sweep: hit {max_files}-file cap, stopping")
                break
            if not p.is_file():
                continue
            # Skip knowledge-base source files — those live until the user
            # removes the KB entry.
            try:
                rp = p.resolve()
                if kb_dir in rp.parents or rp == kb_dir:
                    continue
            except Exception:
                pass
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                continue
            try:
                size = st.st_size
                p.unlink()
                deleted += 1
                bytes_freed += size
            except OSError as e:
                _log.warning(f"uploads sweep: could not delete {p}: {e}")
    except Exception as e:
        _log.warning(f"uploads sweep failed: {e}")
    return deleted, bytes_freed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — load timezone before anything else
    tz_val = db.kv_get("tz_offset") or db.kv_get("timezone")
    if tz_val:
        try:
            config.TZ_OFFSET = int(tz_val)
        except (ValueError, TypeError):
            pass
    # Sweep stale uploads (> 14 days old). Runs once at startup, cheap.
    try:
        n, b = _sweep_uploads()
        if n:
            _log.info(f"uploads sweep: deleted {n} file(s), freed {b} bytes")
    except Exception as e:
        _log.warning(f"uploads sweep error: {e}")
    # Restore preset workspace + thread if a preset was active before restart
    try:
        active_preset = presets.get_active()
        if active_preset:
            presets.ensure_preset_workspace(active_preset)
    except Exception:
        pass
    scheduler.start()
    # Start MCP servers
    try:
        mcp_client.start_all()
    except Exception as e:
        _log.warning(f"MCP startup: {e}")
    # Auto-start Telegram bot if enabled and has token
    try:
        if telegram_bot.is_enabled() and telegram_bot.get_token():
            telegram_bot.start(on_message=_telegram_handler)
    except Exception as e:
        _log.warning(f"Telegram startup: {e}")
    _log.info("web server started")
    yield
    # Shutdown
    try:
        telegram_bot.stop()
    except Exception:
        pass
    try:
        mcp_client.stop_all()
    except Exception:
        pass
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


# ── File text extraction ──

_TEXT_EXTENSIONS = {
    ".txt", ".py", ".js", ".ts", ".md", ".json", ".csv", ".log",
    ".html", ".css", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".sh", ".bat", ".sql", ".xml", ".env", ".gitignore",
}

from dataclasses import dataclass as _dataclass


@_dataclass
class StagedUpload:
    """Result of `_stage_upload()` — one file + any extra string form fields."""
    path: Path
    name: str       # original filename (not sanitized)
    size: int
    ext: str
    extras: dict    # other form fields (string values only) — NO references
                    # to Starlette UploadFile / form objects, so there is no
                    # resource leak after this returns.


def _safe_print(text: str) -> None:
    """Print with fallback for terminals that can't render unicode (e.g. cp1251)."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="ignore").decode("ascii"))


def _get_lan_ip() -> str | None:
    """Discover this machine's LAN IP via a UDP connect to 8.8.8.8."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


async def _stage_upload(request: Request, subdir: str, default_name: str = "file"
                        ) -> StagedUpload | JSONResponse:
    """Validate a multipart upload, sanitize the filename, stage it under
    ``UPLOADS_DIR/<subdir>/<uuid>_<name>``, and return a ``StagedUpload``.

    Returns a JSONResponse with an appropriate status code on failure so
    callers can simply::

        staged = await _stage_upload(request, "presets")
        if isinstance(staged, JSONResponse):
            return staged
        # staged is a StagedUpload

    Form parsing + file reading are both confined to this helper. Once it
    returns, no Starlette form or UploadFile objects remain reachable, so
    their temp files are released promptly.
    """
    content_type = request.headers.get("content-type", "")
    if "multipart" not in content_type:
        return JSONResponse({"error": "multipart/form-data required"}, status_code=400)

    form = await request.form()
    try:
        file = form.get("file")
        if not file:
            return JSONResponse({"error": "no file"}, status_code=400)

        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"error": f"file too large (max {_MAX_UPLOAD_BYTES // 1024 // 1024}MB)"},
                status_code=413,
            )

        fname_raw = getattr(file, "filename", None) or default_name
        # Path(fname_raw).name strips directory traversal from browser-supplied names.
        fname_safe = re.sub(r'[^\w.\-]+', '_', Path(fname_raw).name)[:100] or default_name
        stem = Path(fname_safe).stem
        ext = Path(fname_safe).suffix or ""

        target_dir = UPLOADS_DIR / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        doc_id = uuid.uuid4().hex[:8]
        save_path = target_dir / f"{doc_id}_{stem}{ext}"
        save_path.write_bytes(data)

        # Extract extra string form fields (ignore any additional UploadFile
        # entries — callers never need them).
        extras = {
            k: str(v)
            for k, v in form.multi_items()
            if k != "file" and isinstance(v, str)
        }

        return StagedUpload(
            path=save_path,
            name=fname_raw,
            size=len(data),
            ext=ext,
            extras=extras,
        )
    finally:
        # Release any UploadFile temp-file handles the form holds.
        try:
            await form.close()
        except Exception:
            pass


def _extract_file_text(filepath: Path, max_chars: int = 8000) -> str:
    """Extract text content from a file. Supports text files and PDFs."""
    ext = filepath.suffix.lower()
    try:
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(filepath))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            except ImportError:
                return "[PDF reading requires pypdf: pip install pypdf]"
        elif ext in _TEXT_EXTENSIONS:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        else:
            return f"[Unsupported file type: {ext}]"

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...(truncated from {len(text)} chars)"
        return text
    except Exception as e:
        return f"[Error reading file: {e}]"


# ── Routes ──

@app.post("/api/upload")
async def upload_image(request: Request):
    """Upload an image file. Returns image_id for use in chat."""
    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
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


@app.post("/api/transcribe")
async def transcribe_audio(request: Request):
    """Transcribe audio to text via STT."""
    if not stt.is_available():
        return JSONResponse({"error": "STT not available. Install: pip install faster-whisper"}, status_code=503)

    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse({"error": "no file"}, status_code=400)
        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "file too large"}, status_code=413)
        filename = file.filename or "audio.webm"
    else:
        body = await request.json()
        data = base64.b64decode(body.get("audio_b64", ""))
        if len(data) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "file too large"}, status_code=413)
        filename = f"audio.{body.get('format', 'webm')}"

    fmt = Path(filename).suffix.lstrip(".") or "webm"
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(
            None, functools.partial(stt.transcribe, data, format=fmt)
        )
    except Exception as e:
        _log.error(f"transcription error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

    if text.startswith("[STT Error]"):
        return JSONResponse({"error": text}, status_code=500)
    return {"text": text}


@app.post("/api/tts")
async def text_to_speech(request: Request):
    """Synthesize text to audio via Fish Audio TTS."""
    if not tts.is_available():
        return JSONResponse({"error": "TTS not configured"}, status_code=503)

    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)

    loop = asyncio.get_event_loop()
    audio = await loop.run_in_executor(
        None, functools.partial(tts.synthesize, text, format="wav")
    )
    if not audio:
        return JSONResponse({"error": "TTS synthesis failed"}, status_code=500)

    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Content-Disposition": "inline; filename=voice.mp3"})


@app.get("/api/voice/status")
async def voice_status():
    """Check STT/TTS availability with details."""
    # Check faster-whisper
    has_whisper = stt._check_faster_whisper()
    # Check audio decoder (ffmpeg CLI or bundled PyAV)
    has_ffmpeg_cli = shutil.which("ffmpeg") is not None
    try:
        import av  # noqa: F401  # lazy: PyAV is optional, only used for ffmpeg fallback
        has_pyav = True
    except ImportError:
        has_pyav = False
    has_ffmpeg = has_ffmpeg_cli or has_pyav  # either works
    return {
        "stt": stt.is_available(),
        "tts": tts.is_available(),
        "has_whisper": has_whisper,
        "has_ffmpeg": has_ffmpeg,
        "has_pyav": has_pyav,
        "has_ffmpeg_cli": has_ffmpeg_cli,
        "stt_model": config.get("stt_model"),
        "stt_language": config.get("stt_language"),
        "stt_backend": config.get("stt_backend"),
        "stt_api_url": config.get("stt_api_url"),
        "stt_api_model": config.get("stt_api_model"),
        "stt_openai_key": bool(config.get("stt_openai_key")),
        "tts_enabled": str(config.get("tts_enabled")) == "1",
        "tts_api_url": config.get("tts_api_url"),
        "tts_api_model": config.get("tts_api_model"),
        "tts_api_voice": config.get("tts_api_voice"),
        "tts_api_key": bool(config.get("tts_api_key")),
        "tts_ref_audio": config.get("tts_ref_audio"),
        "tts_ref_text": config.get("tts_ref_text"),
    }


@app.post("/api/voice/install-whisper")
async def install_whisper():
    """Install faster-whisper via pip."""
    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: subprocess.run(
                [sys.executable, "-m", "pip", "install", "faster-whisper"],
                capture_output=True, text=True, timeout=300
            )
        )
        if proc.returncode == 0:
            # Reset cached import check
            stt._HAS_FASTER_WHISPER = None
            stt._check_faster_whisper()
            return {"ok": True, "message": "faster-whisper installed successfully"}
        return {"ok": False, "error": proc.stderr[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/voice/mode")
async def get_voice_mode():
    """Get voice mode state."""
    enabled = db.kv_get("voice_mode:web") == "1"
    return {"enabled": enabled}


@app.post("/api/voice/mode")
async def toggle_voice_mode():
    """Toggle voice mode for web UI."""
    current = db.kv_get("voice_mode:web") == "1"
    new_val = not current
    if new_val and not tts.is_available():
        return {"enabled": False, "error": "TTS not available. Configure model in Settings → Voice."}
    db.kv_set("voice_mode:web", "1" if new_val else "0")
    return {"enabled": new_val}


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

    active_skills = sorted(skills.get_active())

    # Ground-truth core tools from tools.TOOLS — don't let the UI hardcode a
    # different list that drifts from reality.
    core_tools = sorted(t["function"]["name"] for t in tools.TOOLS)

    active_thread = threads.get(threads.get_active_id())

    # Effective context limit is whichever is tighter:
    #   - context_budget: AGENT-side ceiling. Triggers compaction + tool-result
    #     truncation. Once you pass this, the agent starts summarizing old
    #     messages / clipping tool output. This is the PRACTICAL limit for
    #     "when does context run out".
    #   - model_context:  MODEL-side hard cap. Going past this means the
    #     provider errors out. Usually much bigger than context_budget.
    #
    # The UI gauge uses context_budget as the primary denominator. We also
    # expose model_context (override > auto-detect > unknown) so the UI can
    # show a secondary number + warn when misconfigured (budget > model).
    context_budget = int(config.get("context_budget") or 24000)

    override = int(config.get("model_context") or 0)
    detected = 0
    if override <= 0:
        try:
            detected = providers.detect_context_length()
        except Exception:
            detected = 0
    model_ctx = override or detected

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
        "core_tools": core_tools,
        "context_budget": context_budget,
        "model_context": model_ctx,
        "model_context_source": "override" if override else ("detected" if detected else "unknown"),
    }


def _check_version_sync() -> dict:
    """Check current version vs latest GitHub release (sync, for thread pool)."""
    # Read version from pyproject.toml (not importlib.metadata which caches stale values)
    try:
        current = updater._current_version()
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
            # lazy: `packaging` isn't a declared dep — fall through to string
            # compare if missing.
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
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, updater.check)


@app.post("/api/update")
async def trigger_update():
    """Trigger a full update. Progress broadcast via WebSocket."""
    if _update_status["running"]:
        return JSONResponse({"error": "Update already in progress"}, status_code=409)

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

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


@app.get("/api/update/status")
async def update_status():
    """Get current update status."""
    return {"running": _update_status["running"], "result": _update_status["result"]}


@app.post("/api/update/restart")
async def trigger_restart():
    """Restart the server process after update."""
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
            p = providers.get_provider(prov)
            p["url"] = req["endpoint"]
            db.kv_set(f"provider:config:{prov}", json.dumps(p))
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
    return {"lan_access": lan, "ip": _get_lan_ip() or "unknown", "port": _current_port}


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
    return vault.list_keys()


@app.post("/api/secrets")
async def put_secret(data: dict):
    """Encrypt and store a secret. Body: ``{"key": "name", "value": "token"}``."""
    key = (data.get("key") or "").strip()
    value = data.get("value", "")
    if not key:
        return JSONResponse({"error": "key required"}, status_code=400)
    if value is None or value == "":
        return JSONResponse({"error": "value required"}, status_code=400)
    return {"result": vault.save(key, value)}


@app.delete("/api/secrets/{key}")
async def delete_secret(key: str):
    """Delete a secret."""
    return {"result": vault.delete(key)}


@app.post("/api/abort")
async def abort_generation():
    """Abort current agent generation.

    Fires every active WS session's per-request abort event plus the legacy
    shared one (so REST-only callers of _run_agent_sync still work). Does not
    discriminate between sessions — matches the original behaviour.
    """
    with _ws_abort_lock:
        for evt in list(_ws_abort_events):
            evt.set()
    _abort_event.set()
    return {"ok": True}


@app.get("/api/thinking")
async def get_thinking():
    val = db.kv_get("thinking_enabled")
    # Default to enabled if not set
    enabled = val != "false" if val is not None else True
    return {"enabled": enabled}


@app.post("/api/thinking")
async def set_thinking(data: dict):
    val = bool(data.get("enabled", False))
    db.kv_set("thinking_enabled", str(val).lower())
    return {"enabled": val}


@app.get("/api/vision/cameras")
async def list_cameras():
    """Scan available cameras with preview snapshots."""
    loop = asyncio.get_event_loop()

    def _scan_sync():
        cameras = []
        try:
            # lazy: cv2 is a heavy optional dep (opencv-python). base64 is
            # at module top.
            import cv2
            for i in range(5):
                cap = cv2.VideoCapture(i)
                if not cap.isOpened():
                    continue
                # Warm up
                for _ in range(5):
                    cap.read()
                time.sleep(0.2)
                ret, frame = cap.read()
                cap.release()
                if not ret:
                    continue
                h, w = frame.shape[:2]
                brightness = float(frame.mean())
                # Create thumbnail preview (160x120)
                preview_b64 = None
                try:
                    scale = min(160 / w, 120 / h)
                    thumb = cv2.resize(frame, (int(w * scale), int(h * scale)))
                    _, buf = cv2.imencode('.jpg', thumb, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    preview_b64 = base64.b64encode(buf).decode()
                except Exception:
                    pass
                cameras.append({
                    "index": i,
                    "resolution": f"{w}x{h}",
                    "brightness": round(brightness, 1),
                    "usable": brightness > 3,
                    "preview": preview_b64,
                })
        except ImportError:
            pass
        return cameras

    cameras = await loop.run_in_executor(None, _scan_sync)
    current = config.get("camera_index")
    return {"cameras": cameras, "selected": current}


@app.post("/api/vision/reset")
async def vision_reset():
    """Reset persistent camera (e.g. after changing camera_index setting)."""
    tools.camera_release()
    return {"ok": True}


@app.get("/api/streaming")
async def get_streaming():
    return {"enabled": db.kv_get("streaming_enabled") != "false"}  # on by default


async def request_camera_frame(timeout: float = 5.0) -> str | None:
    """Request a camera frame from the connected WebSocket client.
    Returns base64 JPEG or None if no client/camera available.
    Called by agent tools (camera_capture) from background thread.
    """
    req_id = uuid.uuid4().hex[:8]
    event = asyncio.Event()
    _pending_frame_requests[req_id] = {"event": event, "image_b64": None}

    # Broadcast frame request to all WS clients
    await _broadcast({"type": "get_frame", "request_id": req_id})

    # Wait for response (frontend sends frame_response)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _pending_frame_requests.pop(req_id, None)
        return None

    result = _pending_frame_requests.pop(req_id, {}).get("image_b64")
    return result


def request_camera_frame_sync(timeout: float = 5.0) -> str | None:
    """Synchronous wrapper for request_camera_frame — used by agent tools."""
    if not _ws_loop or not _ws_clients:
        return None
    future = asyncio.run_coroutine_threadsafe(request_camera_frame(timeout), _ws_loop)
    try:
        return future.result(timeout=timeout + 1)
    except Exception:
        return None


@app.post("/api/streaming")
async def set_streaming(data: dict):
    val = bool(data.get("enabled", True))
    db.kv_set("streaming_enabled", str(val).lower())
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


@app.get("/api/kv/{key}")
async def kv_get(key: str):
    """Get a raw key-value pair from DB."""
    value = db.kv_get(key)
    return {"key": key, "value": value}


# Prefixes that the raw /api/kv POST endpoint MUST NOT overwrite. These are
# populated by dedicated endpoints (or by the installer) and accepting a bare
# {key, value} blob for them lets an authed user corrupt internal state —
# e.g. reassigning the Telegram owner, faking `version:latest`, poisoning
# provider credentials. Each entry is a startswith() match.
_KV_WRITE_BLOCKLIST: tuple[str, ...] = (
    "telegram:owner_id",
    "version:",
    "setup_",
    "_migrated_",
    "provider:config:",
    "setting:",
    "soul:",
)


@app.post("/api/kv")
async def kv_set(request: Request):
    """Set a raw key-value pair in DB."""
    data = await request.json()
    key = data.get("key", "")
    value = data.get("value", "")
    if not isinstance(key, str) or not key:
        return JSONResponse({"error": "key required"}, status_code=400)
    if len(key) > 200:
        return JSONResponse({"error": "key too long (max 200 chars)"}, status_code=400)
    for prefix in _KV_WRITE_BLOCKLIST:
        if key.startswith(prefix):
            return JSONResponse(
                {"error": f"key '{key}' is protected (prefix '{prefix}' reserved for internal state)"},
                status_code=403,
            )
    db.kv_set(key, value)
    return {"ok": True, "key": key}


@app.get("/api/config/export")
async def export_config_endpoint():
    """Export all settings as downloadable JSON."""
    data = config.export_config()
    filename = f"qwe-qwe-config-{time.strftime('%Y%m%d')}.json"
    return Response(
        content=json.dumps(data, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/config/import")
async def import_config_endpoint(request: Request):
    """Import settings from JSON. Accepts multipart file or JSON body."""
    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse({"error": "no file"}, status_code=400)
        raw = await file.read()
        data = json.loads(raw)
    else:
        data = await request.json()
    results = config.import_config(data)
    return {"results": results, "count": len(results)}


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
    return scheduler.list_tasks()


@app.post("/api/cron")
async def add_cron(data: dict):
    """Add a scheduled task."""
    result = scheduler.add(data.get("name",""), data.get("task",""), data.get("schedule",""))
    return result


@app.delete("/api/cron/{task_id}")
async def remove_cron(task_id: int):
    """Remove a scheduled task."""
    return {"result": scheduler.remove(task_id)}


@app.get("/api/tasks")
async def list_tasks():
    """Get background task results."""
    return {"pending": tasks.pending_count(), "results": tasks.get_results(clear=False)}


# ── Knowledge Upload endpoints ──


def _emit_knowledge(data: dict):
    """Broadcast knowledge indexing event to WS clients."""
    if _ws_loop and _ws_clients:
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(data), _ws_loop)
        except Exception:
            pass


def _run_knowledge_index(task_id: int, files: list[dict], tags: list[str] | None = None):
    """Background knowledge indexing thread."""
    global _knowledge_task

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
        results = rag.index_files_batch(files, progress_cb=progress_cb, phase_cb=phase_cb, tags=tags or None)
        errors = [r for r in results if r.get("status") not in ("indexed", "already up to date")]
    except Exception as e:
        _log.error(f"knowledge indexing failed: {e}", exc_info=True)
        errors.append({"error": str(e)})

    # Calculate totals
    total_chunks = sum(r.get("chunks", 0) for r in results)

    with _knowledge_lock:
        if _knowledge_task:
            _knowledge_task.update({
                "status": "done",
                "files_done": len(results),
                "chunks_done": total_chunks,
                "errors_count": len(errors),
            })

    _emit_knowledge({
        "type": "knowledge_done",
        "files": len(results),
        "chunks": total_chunks,
        "errors": len(errors),
        "duration_sec": 0
    })

    # Update tasks registry
    tasks.update(task_id, "done", f"Indexed {len(results)} files, {total_chunks} chunks")

    # Record in recent-activity history
    started = 0
    with _knowledge_lock:
        started = (_knowledge_task or {}).get("started_at", time.time()) if _knowledge_task else time.time()
    _push_history({
        "kind": "batch" if len(files) > 1 else "file",
        "label": (files[0].get("path") or files[0].get("url", ""))[:120] if files else "(batch)",
        "status": "done" if not errors else "partial" if results else "error",
        "chunks": total_chunks,
        "files": len(results),
        "errors_count": len(errors),
        "duration_sec": round(time.time() - started, 2),
        "converter": "markitdown",
    })

    # Keep result visible for polling clients, then clear after 10s
    def _clear_task():
        time.sleep(10)
        with _knowledge_lock:
            global _knowledge_task
            _knowledge_task = None
    threading.Thread(target=_clear_task, daemon=True).start()


# ── File Browser ──

@app.get("/api/files/browse")
async def file_browse(request: Request):
    """Browse directory contents for Knowledge file picker."""
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


# ── Business presets ────────────────────────────────────────────────────

@app.get("/api/presets")
async def presets_list():
    """List all installed presets."""
    return {"items": presets.list_installed(), "active": presets.get_active()}


# NOTE: literal-path routes MUST be declared before the parameterised
# `/api/presets/{preset_id}` catch-all — FastAPI matches in declaration order,
# so declaring `{preset_id}` first swallows `/api/presets/onboarding` etc.


@app.get("/api/presets/onboarding")
async def presets_onboarding():
    """Get onboarding content for the active preset."""
    if not presets.should_show_onboarding():
        return {"show": False}
    text = presets.get_onboarding()
    if not text:
        return {"show": False}
    info = presets.get_active_info()
    presets.mark_onboarding_shown()
    return {"show": True, "text": text, "name": info["name"] if info else ""}


@app.post("/api/presets/deactivate")
async def presets_deactivate():
    """Deactivate the current preset (restores the soul backup)."""
    current = presets.get_active()
    if not current:
        return {"ok": True, "was_active": None}
    presets.deactivate()
    return {"ok": True, "was_active": current}


@app.post("/api/presets/install")
async def presets_install(request: Request):
    """Upload and install a .qwp archive from the user's computer."""
    _log.info("preset install: upload started")
    staged = await _stage_upload(request, "presets", default_name="preset.qwp")
    if isinstance(staged, JSONResponse):
        _log.warning(f"preset install: upload rejected: {staged.body}")
        return staged
    _log.info(f"preset install: file staged at {staged.path} ({staged.size} bytes, name={staged.name})")
    overwrite = staged.extras.get("overwrite", "") in ("1", "true", "yes")

    try:
        _log.info(f"preset install: loading from {staged.path}")
        info = presets.load_any(str(staged.path))
        _log.info(f"preset install: loaded {info.id} v{info.version}, validating...")
        errors = presets.validate(info)
        if errors:
            _log.warning(f"preset install: validation failed: {errors}")
            return JSONResponse(
                {"error": "validation failed", "details": errors},
                status_code=400,
            )
        _log.info(f"preset install: validation passed, installing...")
        result = presets.install(info, overwrite=overwrite)
    except FileExistsError as e:
        _log.info(f"preset install: already exists: {e}")
        return JSONResponse(
            {"error": str(e), "code": "already_installed"},
            status_code=409,
        )
    except Exception as e:
        _log.error(f"preset install failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        try:
            staged.path.unlink(missing_ok=True)
        except Exception:
            pass

    _log.info(f"preset installed via web: {result['id']} v{result['version']}")
    return result


@app.post("/api/presets/{preset_id}/activate")
async def presets_activate(preset_id: str):
    """Activate a preset. Deactivates the current one first."""
    try:
        return presets.activate(preset_id)
    except Exception as e:
        _log.error(f"preset activate failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/presets/{preset_id}")
async def presets_info(preset_id: str):
    """Get full manifest of an installed preset. (Catch-all — must be after
    literal `/api/presets/onboarding` etc.)"""
    info = presets.get_info(preset_id)
    if not info:
        return JSONResponse({"error": "not found"}, status_code=404)
    return info


@app.delete("/api/presets/{preset_id}")
async def presets_delete(preset_id: str):
    """Uninstall a preset. Deactivates it first if active."""
    try:
        presets.uninstall(preset_id)
    except Exception as e:
        _log.error(f"preset uninstall failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "id": preset_id}


# ── Knowledge Base ──────────────────────────────────────────────────────

@app.post("/api/knowledge/upload")
async def knowledge_upload(request: Request):
    """Upload a file from the user's computer to the uploads directory.

    Returns its absolute path so the frontend can immediately stage it
    for indexing (without roundtripping through the LLM / chat).
    """
    staged = await _stage_upload(request, "kb", default_name="file.txt")
    if isinstance(staged, JSONResponse):
        return staged
    _log.info(f"kb upload: {staged.name} → {staged.path} ({staged.size} bytes)")
    return {
        "path": str(staged.path.resolve()),
        "name": staged.name,
        "size": staged.size,
        "ext": staged.ext or ".txt",
    }


@app.post("/api/knowledge/scan")
async def knowledge_scan(data: dict):
    """Scan a path and return file preview for indexing."""
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


def _url_resolves_to_private(url: str) -> str | None:
    """Return an error message if the URL resolves to a private/loopback/link-local
    address (or unspecified 0.0.0.0 / ::), else None. Uses ``socket.getaddrinfo``
    so DNS rebinding to a private IP is caught too, not just literal IPs.
    """
    try:
        host = urlparse(url).hostname
    except Exception:
        return "Invalid URL"
    if not host:
        return "URL is missing a hostname"

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        return f"Could not resolve hostname: {e}"

    for info in infos:
        addr = info[4][0]
        # Strip IPv6 zone id if present (e.g. "fe80::1%eth0")
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return (
                f"Private/loopback URLs not allowed (resolved to {addr}). "
                "Set QWE_ALLOW_PRIVATE_URLS=1 to override."
            )
    return None


@app.post("/api/knowledge/url")
async def knowledge_url(data: dict):
    """Fetch a URL, extract readable text, and index it. Runs in the background.

    Body: {"url": "https://…", "tags": ["optional","list"]}
    """
    global _knowledge_task
    url = (data.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "URL required"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "URL must start with http:// or https://"}, status_code=400)

    # SSRF guard — reject URLs that resolve to private/loopback/link-local ranges.
    # Opt out by setting QWE_ALLOW_PRIVATE_URLS=1 (e.g. for self-hosted wikis on
    # a LAN).
    if os.environ.get("QWE_ALLOW_PRIVATE_URLS", "").strip() != "1":
        err = _url_resolves_to_private(url)
        if err:
            return JSONResponse({"error": err}, status_code=403)
    tags_raw = data.get("tags", [])
    tags = [t.strip() for t in tags_raw if isinstance(t, str) and t.strip()] if tags_raw else []

    # Block if an indexing run is already live
    with _knowledge_lock:
        if _knowledge_task and _knowledge_task.get("status") == "running":
            return JSONResponse({"error": "Indexing already in progress"}, status_code=409)

    task_id = tasks.register("knowledge_url", f"Fetching {url}")
    with _knowledge_lock:
        _knowledge_task = {
            "task_id": task_id, "status": "running",
            "current": 0, "total": 1, "file": url, "phase": "fetch", "errors": [],
            "started_at": time.time(),
        }

    def _run_url():
        start = time.time()
        result: dict = {}
        ok = False
        try:
            result = rag.index_url(url, tags=tags)
            ok = result.get("status") == "indexed"
            with _knowledge_lock:
                if ok:
                    _knowledge_task.update({
                        "status": "done", "current": 1,
                        "file": result.get("path", url),
                        "phase": "done",
                    })
                else:
                    _knowledge_task["errors"].append(f"{url}: {result.get('status')}")
                    _knowledge_task.update({"status": "done", "current": 1, "phase": "done"})
        except Exception as e:
            _log.error(f"URL index error {url}: {e}", exc_info=True)
            with _knowledge_lock:
                _knowledge_task["errors"].append(f"{url}: {e}")
                _knowledge_task.update({"status": "done", "current": 1, "phase": "done"})
            result = {"status": f"error: {e}"}
        finally:
            duration = round(time.time() - start, 2)
            chunks = result.get("chunks", 0) if isinstance(result, dict) else 0
            converter = result.get("converter", "") if isinstance(result, dict) else ""
            label = result.get("title") if isinstance(result, dict) and result.get("title") else url
            # Mark the task row completed so it stops leaking as "running"
            try:
                tasks.update(task_id, "done" if ok else "error",
                             f"{chunks} chunks via {converter}" if ok else result.get("status", "failed"))
            except Exception:
                pass
            has_t = result.get("has_transcript") if isinstance(result, dict) else None
            fb = result.get("fallback_reason", "") if isinstance(result, dict) else ""
            # Mark "partial" when yt-dlp fell back to metadata-only so the UI
            # surfaces the warning without treating it as a hard failure.
            entry_status = "error" if not ok else ("partial" if has_t is False else "done")
            _push_history({
                "kind": "url",
                "label": label[:120],
                "url": url,
                "status": entry_status,
                "chunks": chunks,
                "duration_sec": duration,
                "converter": converter,
                "has_transcript": has_t,
                "fallback_reason": fb,
                "errors": list(_knowledge_task.get("errors", [])) if _knowledge_task else [],
            })

    threading.Thread(target=_run_url, daemon=True).start()
    return {"task_id": task_id, "status": "started", "url": url}


@app.post("/api/knowledge/index")
async def knowledge_index(data: dict):
    """Start background indexing of selected files (or URLs)."""
    global _knowledge_task

    files = data.get("files", [])
    tags_raw = data.get("tags", [])
    tags = [t.strip() for t in tags_raw if isinstance(t, str) and t.strip()] if tags_raw else []
    if not files:
        return JSONResponse({"error": "No files to index"}, status_code=400)

    # Accept paths under $HOME OR under UPLOADS_DIR, OR any http(s) URL
    home = Path.home().resolve()
    uploads = UPLOADS_DIR.resolve()
    for f in files:
        src = f.get("path") or f.get("url", "")
        if isinstance(src, str) and src.startswith(("http://", "https://")):
            continue  # URLs handled by index_url inside the worker
        fp = Path(src).resolve()
        ok = False
        for allowed in (home, uploads):
            try:
                fp.relative_to(allowed)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            return JSONResponse({"error": f"Access denied: {src}"}, status_code=403)

    with _knowledge_lock:
        if _knowledge_task and _knowledge_task.get("status") == "running":
            return JSONResponse({"error": "Indexing already in progress"}, status_code=409)

    task_id = tasks.register("knowledge_index", f"Indexing {len(files)} files")

    with _knowledge_lock:
        _knowledge_task = {
            "task_id": task_id,
            "status": "running",
            "current": 0,
            "total": len(files),
            "file": "",
            "phase": "cpu",
            "errors": [],
            "started_at": time.time(),
        }

    thread = threading.Thread(target=_run_knowledge_index, args=(task_id, files, tags), daemon=True)
    thread.start()

    return {"task_id": task_id, "status": "started", "total": len(files)}


@app.get("/api/knowledge/status")
async def knowledge_status():
    """Get current indexing status."""
    with _knowledge_lock:
        if _knowledge_task:
            return dict(_knowledge_task)
    return {"status": "idle"}


@app.get("/api/knowledge/recent")
async def knowledge_recent():
    """Last 20 completed indexing runs (URL, file, batch). Newest first.

    Powers the "Recent activity" card in the Memory view so you can see
    what just got indexed, how many chunks landed, and whether anything
    errored — without fishing through logs.
    """
    return {"items": list(_knowledge_history)}


@app.get("/api/knowledge/graph")
async def knowledge_graph():
    """Get entity graph data for visualization."""
    entities = mem.get_all_entities(limit=200)
    # Build nodes + links for D3 force graph
    nodes = []
    links = []
    node_names = set()
    for e in entities:
        nodes.append({
            "id": e["name"],
            "type": e["type"],
            "description": e["description"],
            "weight": e["observation_count"],
        })
        node_names.add(e["name"])

    # Build links from relations (only between existing nodes)
    for e in entities:
        for rel in e.get("relations", []):
            target = rel.get("to", "")
            if target in node_names:
                links.append({
                    "source": e["name"],
                    "target": target,
                    "rel": rel.get("rel", "related"),
                })

    return {"nodes": nodes, "links": links}


@app.post("/api/knowledge/graph/clear")
async def knowledge_graph_clear():
    """Clear all entities and wiki from the knowledge graph."""
    mem.clear_graph()
    return {"ok": True, "message": "Graph cleared — entities and wiki removed"}


@app.get("/api/knowledge/list")
async def knowledge_list():
    """List indexed files. When preset active, show only preset's files."""
    files = rag.list_indexed_files()
    try:
        active = presets.get_active()
        if active:
            tag = f"preset:{active}"
            files = [f for f in files if tag in (f.get("tags") or [])]
    except Exception:
        pass
    return {"files": files}


@app.post("/api/knowledge/search")
async def knowledge_search(data: dict):
    """Search indexed knowledge base."""
    query = data.get("query", "").strip()
    if not query:
        return JSONResponse({"error": "Query required"}, status_code=400)
    limit = min(int(data.get("limit", 10)), 50)
    tags = data.get("tags")  # optional list of tags to filter by
    if tags and isinstance(tags, list):
        tags = [t for t in tags if isinstance(t, str) and t.strip()]
    else:
        tags = None
    try:
        results = rag.search(query, limit=limit, tags=tags)
        return {"results": results}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/knowledge/file")
async def knowledge_delete(request: Request):
    """Delete a file from the index."""
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
    return skills.list_all()


@app.post("/api/skills/{name}")
async def toggle_skill(name: str, data: dict):
    """Enable or disable a skill."""
    if data.get("active"):
        return {"result": skills.enable(name)}
    else:
        return {"result": skills.disable(name)}


# ── Thread endpoints ──

@app.get("/api/threads")
async def list_threads(include_archived: bool = False):
    """List all threads with stats. Filters by active preset if one is active."""
    all_threads = threads.list_all(include_archived=include_archived)
    # Preset isolation: bidirectional filtering
    try:
        active_preset = presets.get_active()
        if active_preset:
            # Preset active → show ONLY this preset's threads
            info = presets.get_info(active_preset)
            prefix = f"Preset: {info['name']}" if info else "Preset:"
            all_threads = [t for t in all_threads if t["name"].startswith(prefix)]
        else:
            # No preset → hide ALL preset threads
            all_threads = [t for t in all_threads if not t["name"].startswith("Preset:")]
    except Exception:
        pass
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
    db.execute("UPDATE threads SET meta=? WHERE id=?", (json.dumps(meta), thread_id))
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


@app.post("/api/threads/{thread_id}/regenerate")
async def regenerate_turn(thread_id: str):
    """Erase the last user → assistant turn so the client can re-send a fresh prompt.

    Behaviour:
      1. Walk messages for this thread newest-first, collect rows since the last
         user message (inclusive). That's the "last turn" — assistant reply,
         any tool traces, and the triggering user message.
      2. Delete those rows.
      3. Return the user's original text + attachments so the client can re-submit
         via the normal WebSocket path. From the agent's perspective it's a fresh
         prompt — no "this is a regeneration" signal leaks into context.
    """
    rows = db.fetchall(
        "SELECT id, role, content, meta FROM messages WHERE thread_id=? ORDER BY id DESC",
        (thread_id,)
    )
    if not rows:
        return JSONResponse({"error": "empty thread"}, status_code=400)

    to_delete: list[int] = []
    user_text = ""
    user_meta = None
    for mid, role, content, meta_json in rows:
        to_delete.append(mid)
        if role == "user":
            user_text = content or ""
            try:
                user_meta = json.loads(meta_json) if meta_json else None
            except Exception:
                user_meta = None
            break
    if not user_text:
        return JSONResponse({"error": "no user message in recent turn"}, status_code=400)

    db.delete_messages_by_ids(to_delete)
    _log.info(f"regenerate: deleted {len(to_delete)} messages in thread {thread_id}")
    return {
        "ok": True,
        "deleted": len(to_delete),
        "user_text": user_text,
        "meta": user_meta or {},
    }


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

    # Per-session abort event — so one client's disconnect / Stop doesn't
    # abort a concurrent telegram (or other WS) turn.
    my_abort_event = threading.Event()

    # Auth check for WebSocket (middleware doesn't cover WS)
    if _AUTH_PASSWORD:
        cookie = ws.cookies.get(_AUTH_COOKIE, "")
        if not hmac.compare_digest(cookie, _AUTH_TOKEN):
            await ws.close(code=4001, reason="unauthorized")
            return

    await ws.accept()
    with _ws_lock:
        _ws_clients.add(ws)
    with _ws_abort_lock:
        _ws_abort_events.add(my_abort_event)
    _ws_loop = asyncio.get_event_loop()
    _log.info(f"websocket client connected ({len(_ws_clients)} total)")

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)

                # Handle frame responses from camera_capture requests
                if msg.get("type") == "frame_response":
                    req_id = msg.get("request_id")
                    if req_id and req_id in _pending_frame_requests:
                        _pending_frame_requests[req_id]["image_b64"] = msg.get("image_b64")
                        _pending_frame_requests[req_id]["event"].set()
                    continue

                user_input = msg.get("text", "").strip()
                thread_id = msg.get("thread_id")  # optional — None uses active
                image_b64 = msg.get("image_b64")  # optional base64 image
                document = msg.get("document")    # optional {file_b64, filename}
                live_mode = bool(msg.get("live"))  # per-request flag: force TTS reply
            except json.JSONDecodeError:
                user_input = data.strip()
                thread_id = None
                image_b64 = None
                document = None
                live_mode = False

            if not user_input and not image_b64 and not document:
                continue

            # Save image to uploads/ so it persists in history
            image_path = None
            if image_b64:
                try:
                    img_id = str(uuid.uuid4())[:8]
                    img_file = UPLOADS_DIR / f"{img_id}.png"
                    img_file.write_bytes(base64.b64decode(image_b64))
                    image_path = f"/uploads/{img_id}.png"
                except Exception as e:
                    _log.warning(f"failed to save ws image: {e}")

            # Save uploaded document to disk and pass PATH to the agent
            # (we used to inline the full file contents → context bloat on 32k models)
            file_name = None
            file_path = None
            file_size = 0
            if document:
                try:
                    doc_id = str(uuid.uuid4())[:8]
                    fname_raw = document.get("filename", "file.txt")
                    # Sanitize filename — keep original name for readability, strip paths
                    fname_safe = re.sub(r'[^\w.\-]+', '_', Path(fname_raw).name)[:80] or "file.txt"
                    ext = Path(fname_safe).suffix or ".txt"
                    stem = Path(fname_safe).stem
                    doc_file = UPLOADS_DIR / f"{doc_id}_{stem}{ext}"
                    doc_file.write_bytes(base64.b64decode(document["file_b64"]))
                    file_name = fname_raw
                    file_path = str(doc_file.resolve())
                    file_size = doc_file.stat().st_size
                    _log.info(f"document uploaded: {fname_raw} → {file_path} ({file_size} bytes)")
                except Exception as e:
                    _log.warning(f"failed to save document: {e}")

            # Reference file by PATH in the user message — agent uses read_file
            # or rag_index tools on demand. This keeps context small (path is cheap,
            # file body only loaded into context when the agent actually needs it).
            if file_path and file_name:
                size_kb = file_size / 1024
                ref = (
                    f"[File attached: {file_name} ({size_kb:.1f} KB) — saved at {file_path}. "
                    f"To view contents call read_file(path). "
                    f"To add to the knowledge base call tool_search('rag') then rag_index(path).]"
                )
                user_input = (user_input + "\n\n" if user_input else "") + ref

            _log.info(f"ws message: thread={thread_id or 'active'} | {user_input[:100]}" +
                       (" [+image]" if image_b64 else "") +
                       (f" [+doc:{file_name}]" if file_name else ""))

            # Check if model needs loading
            loading_msg = None
            if providers.get_active_name() in ("lmstudio", "ollama"):
                try:
                    p = providers.get_provider()
                    api_base = p.get("url", "").rstrip("/").replace("/v1", "")
                    model = providers.get_model()
                    r = requests.get(f"{api_base}/api/v1/models", timeout=5)
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
                # Run agent in thread pool with streaming via queue
                loop = asyncio.get_event_loop()
                stream_queue: asyncio.Queue = asyncio.Queue()

                async def _drain_stream_queue():
                    """Forward streamed chunks from agent thread to this WS client."""
                    while True:
                        msg = await stream_queue.get()
                        if msg is None:
                            break  # agent finished
                        await _ws_send_safe(ws, msg)

                # Assemble file metadata for persistence (shown in UI on reload)
                file_meta = None
                if file_path and file_name:
                    file_meta = {"path": file_path, "name": file_name, "size": file_size}

                def _run_with_queue(user_input, thread_id, image_b64, image_path, file_meta):
                    """Wrap _run_agent_sync, routing content/thinking/status to queue."""
                    import agent as _agent

                    def _queue_content(text):
                        asyncio.run_coroutine_threadsafe(
                            stream_queue.put({"type": "content_delta", "text": text}), loop)

                    def _queue_thinking(text):
                        asyncio.run_coroutine_threadsafe(
                            stream_queue.put({"type": "thinking_delta", "text": text}), loop)

                    def _queue_status(text):
                        asyncio.run_coroutine_threadsafe(
                            stream_queue.put({"type": "status", "text": text}), loop)

                    def _queue_tool_call(name, args_preview, result_preview=""):
                        asyncio.run_coroutine_threadsafe(
                            stream_queue.put({"type": "tool_call", "name": name, "args": args_preview, "result": result_preview}), loop)

                    def _queue_recall(memories):
                        asyncio.run_coroutine_threadsafe(
                            stream_queue.put({"type": "recall", "memories": memories}), loop)

                    # Set per-request callbacks (override broadcast ones)
                    # Web UI always streams
                    _agent._content_callback = _queue_content
                    _agent._thinking_callback = _queue_thinking
                    _agent._status_callback = _queue_status
                    _agent._tool_call_callback = _queue_tool_call
                    _agent._recall_callback = _queue_recall
                    try:
                        return _run_agent_sync(user_input, thread_id,
                                               image_b64=image_b64,
                                               image_path=image_path,
                                               file_meta=file_meta,
                                               abort_event=my_abort_event)
                    finally:
                        # Signal queue drain to stop
                        asyncio.run_coroutine_threadsafe(stream_queue.put(None), loop)

                # Run agent + queue drain concurrently
                agent_task = loop.run_in_executor(
                    None, functools.partial(_run_with_queue, user_input,
                                            thread_id, image_b64, image_path, file_meta))
                drain_task = asyncio.ensure_future(_drain_stream_queue())
                result = await agent_task
                await drain_task  # ensure all queued messages are sent

                # TTS: synthesize voice for reply if voice mode is on OR live mode (per-request flag)
                audio_url = None
                voice_mode = db.kv_get("voice_mode:web") == "1"
                try:
                    if (voice_mode or live_mode) and tts.is_available() and result["reply"]:
                        audio_data = await loop.run_in_executor(
                            None, functools.partial(tts.synthesize, result["reply"], format="mp3")
                        )
                        if audio_data:
                            audio_id = str(uuid.uuid4())[:8]
                            audio_file = UPLOADS_DIR / f"{audio_id}.mp3"
                            audio_file.write_bytes(audio_data)
                            audio_url = f"/uploads/{audio_id}.mp3"
                except Exception as e:
                    _log.debug(f"ws TTS skipped: {e}")

                # Send reply — abort if client disconnected
                reply_payload = {
                    "type": "reply",
                    "text": result["reply"],
                    "thinking": result.get("thinking", ""),
                    "tools": result["tools"],
                    "duration_ms": result["duration_ms"],
                    "context_hits": result["context_hits"],
                    "thread_id": result["thread_id"],
                    "tokens": result.get("tokens", 0),
                    "prompt_tokens": result.get("prompt_tokens", 0),
                    "tok_per_sec": result.get("tok_per_sec", 0),
                }
                if audio_url:
                    reply_payload["audio_url"] = audio_url
                # Attach files queued by send_file tool
                pending = tools.get_pending_files()
                if pending:
                    reply_payload["files"] = pending
                    # Persist files into the last assistant message's meta so
                    # reloads (F5 / thread switch) restore the download chips.
                    try:
                        row = db.fetchone(
                            "SELECT id, meta FROM messages WHERE thread_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                            (result["thread_id"],)
                        )
                        if row:
                            mid, meta_raw = row
                            meta_dict = json.loads(meta_raw) if meta_raw else {}
                            meta_dict["files"] = pending
                            if audio_url:
                                meta_dict["audio_url"] = audio_url
                            db.execute("UPDATE messages SET meta=? WHERE id=?",
                                       (json.dumps(meta_dict), mid))
                    except Exception as e:
                        _log.debug(f"failed to persist files to message meta: {e}")
                if not await _ws_send_safe(ws, reply_payload):
                    break

            except Exception as e:
                _log.error(f"ws agent error: {e}", exc_info=True)
                user_msg = _friendly_error(e)
                if not await _ws_send_safe(ws, {"type": "error", "text": user_msg}):
                    break

    except WebSocketDisconnect:
        # Only abort this session — not other concurrent sources (telegram, other WS).
        my_abort_event.set()
        _log.info("websocket client disconnected — aborting this session's agent turn")
    except (ConnectionResetError, RuntimeError, OSError):
        my_abort_event.set()
        _log.debug("websocket connection reset — aborting this session's agent turn")
    except Exception as e:
        _log.error(f"websocket error: {e}", exc_info=True)
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)
        with _ws_abort_lock:
            _ws_abort_events.discard(my_abort_event)
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
            _ws_clients.difference_update(dead)


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


# Register cron callback (scheduler is hoisted at module top)
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

    # Store tool names so telegram_bot can access them for enriched display
    agent._last_tools = list(result.tool_calls_made) if result.tool_calls_made else []

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

# ── MCP Server Management ──

@app.get("/api/mcp/presets")
async def mcp_presets():
    """Return the built-in MCP server preset catalogue for the UI picker."""
    return {"presets": mcp_client.get_presets()}


@app.get("/api/mcp/health")
async def mcp_health():
    """Fast MCP health summary: configured / running / tripped / tools_total."""
    return mcp_client.health_check()


@app.get("/api/mcp/servers")
async def mcp_list_servers():
    """List all configured MCP servers with status."""
    return mcp_client.list_servers()


@app.post("/api/mcp/servers")
async def mcp_add_server(request: Request):
    """Add or update an MCP server."""
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    mcp_client.add_server(
        name,
        command=data.get("command", ""),
        args=data.get("args", []),
        env=data.get("env", {}),
        url=data.get("url", ""),
        transport=data.get("transport", "stdio"),
        enabled=data.get("enabled", True),
    )
    # Auto-start if enabled
    if data.get("enabled", True):
        result = mcp_client.start_server(name)
        return {"ok": True, "message": result}
    return {"ok": True, "message": f"MCP '{name}' saved (disabled)"}


@app.delete("/api/mcp/servers/{name}")
async def mcp_remove_server(name: str):
    """Remove an MCP server."""
    return {"ok": True, "message": mcp_client.remove_server(name)}


@app.post("/api/mcp/servers/{name}/restart")
async def mcp_restart_server(name: str):
    """Restart an MCP server connection."""
    result = mcp_client.start_server(name)
    return {"ok": True, "message": result}


@app.post("/api/mcp/servers/{name}/toggle")
async def mcp_toggle_server(name: str, request: Request):
    """Enable or disable an MCP server."""
    data = await request.json()
    enabled = bool(data.get("enabled", True))
    config = mcp_client.load_config()
    if name not in config:
        return JSONResponse({"error": "not found"}, status_code=404)
    config[name]["enabled"] = enabled
    mcp_client.save_config(config)
    if enabled:
        result = mcp_client.start_server(name)
    else:
        result = mcp_client.stop_server(name)
    return {"ok": True, "message": result}


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

def _ensure_ssl_cert() -> tuple[str, str]:
    """Generate a self-signed SSL certificate if one doesn't exist.

    Returns (certfile, keyfile) paths inside DATA_DIR. The cert is valid
    for 365 days and covers localhost + the machine's LAN IPs so mobile
    browsers can connect over HTTPS and access getUserMedia (camera).
    """
    cert_path = config.DATA_DIR / "ssl" / "cert.pem"
    key_path = config.DATA_DIR / "ssl" / "key.pem"
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # lazy: `cryptography` is a heavy dep that's only needed the first time
        # SSL is enabled (once per install, then cert is cached on disk).
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime

        # Generate RSA key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Collect SANs: localhost + LAN IPs
        sans = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv6Address("::1")),
        ]
        lan_ip = _get_lan_ip()
        if lan_ip:
            try:
                sans.append(x509.IPAddress(ipaddress.IPv4Address(lan_ip)))
            except Exception:
                pass

        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "qwe-qwe"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime(2024, 1, 1))
            .not_valid_after(datetime(2124, 1, 1))
            .add_extension(
                x509.SubjectAlternativeName(sans),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _log.info(f"generated self-signed SSL cert: {cert_path}")
    except Exception as e:
        _log.error(f"SSL cert generation failed: {e}")
        raise
    return str(cert_path), str(key_path)


def start(host: str = "0.0.0.0", port: int = 7860, ssl: bool = False):
    """Start the web server.

    Args:
        ssl: When True, serve over HTTPS with a self-signed certificate.
             Required for camera access from mobile devices over LAN.
    """
    global _current_port
    _current_port = port
    # lazy: uvicorn boots a lot of machinery; only the `start` entrypoint needs it
    import uvicorn

    # Suppress noisy Windows asyncio socket-shutdown errors during WS/subprocess cleanup.
    # These are purely cosmetic — the connection has already closed, we just can't call
    # shutdown() on a socket the peer already reset. See: https://bugs.python.org/issue39010
    if sys.platform == "win32":
        _BENIGN = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

        def _quiet_exception_handler(loop, context):
            exc = context.get("exception")
            msg = context.get("message", "") or ""
            if isinstance(exc, _BENIGN):
                return
            if isinstance(exc, OSError) and "_call_connection_lost" in msg:
                return  # proactor shutdown race
            loop.default_exception_handler(context)

        # Install on every loop uvicorn (or anything else) creates.
        class _QuietPolicy(type(asyncio.get_event_loop_policy())):
            def new_event_loop(self):
                loop = super().new_event_loop()
                loop.set_exception_handler(_quiet_exception_handler)
                return loop
        try:
            asyncio.set_event_loop_policy(_QuietPolicy())
        except Exception:
            pass
        # Also patch current loop if one is already running.
        try:
            asyncio.get_event_loop().set_exception_handler(_quiet_exception_handler)
        except RuntimeError:
            pass

        # Monkey-patch the proactor transport to swallow the shutdown OSError directly —
        # belt & braces: the exception handler catches most cases, this catches the rest.
        try:
            # lazy: Windows-only internals; only patched on the win32 branch.
            from asyncio.proactor_events import _ProactorBasePipeTransport
            _orig = _ProactorBasePipeTransport._call_connection_lost

            def _quiet_call_connection_lost(self, exc):
                try:
                    return _orig(self, exc)
                except (OSError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    return None
            _ProactorBasePipeTransport._call_connection_lost = _quiet_call_connection_lost
        except Exception:
            pass

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

    # SSL setup for camera on mobile (getUserMedia requires HTTPS)
    ssl_kwargs = {}
    proto = "http"
    if ssl:
        try:
            certfile, keyfile = _ensure_ssl_cert()
            ssl_kwargs = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
            proto = "https"
        except Exception as e:
            _log.warning(f"SSL disabled: {e}")

    _log.info(f"starting web server on {actual_host}:{port} (LAN: {'on' if lan else 'off'}, SSL: {'on' if ssl_kwargs else 'off'})")
    if actual_host == "0.0.0.0":
        ip = _get_lan_ip()
        _safe_print(f"\n  ⚡ qwe-qwe web UI → {proto}://localhost:{port}")
        if ip:
            _safe_print(f"  📱 LAN access → {proto}://{ip}:{port}")
        if ssl_kwargs:
            _safe_print(f"  🔒 Self-signed cert — accept the browser warning on first connect")
        elif ip:
            _safe_print(f"  📷 For camera on mobile: restart with --ssl")
        print()
    else:
        _safe_print(f"\n  ⚡ qwe-qwe web UI → {proto}://localhost:{port}")
        print(f"  🔒 Local only (enable LAN in Settings → System)\n")

    uvicorn.run(app, host=actual_host, port=port, log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    start()
