"""Speech-to-Text module — faster-whisper (local) with OpenAI Whisper API fallback."""

from __future__ import annotations

import io
import logging
import subprocess
import tempfile
from pathlib import Path

_log = logging.getLogger("stt")

_model = None          # lazy-loaded WhisperModel
_HAS_FASTER_WHISPER = None  # cached import check


def _check_faster_whisper() -> bool:
    global _HAS_FASTER_WHISPER
    if _HAS_FASTER_WHISPER is None:
        try:
            import faster_whisper  # noqa: F401
            _HAS_FASTER_WHISPER = True
        except ImportError:
            _HAS_FASTER_WHISPER = False
    return _HAS_FASTER_WHISPER


def is_available() -> bool:
    """True if any STT backend is usable."""
    if _check_faster_whisper():
        return True
    try:
        import config
        return bool(config.get("stt_openai_key"))
    except Exception:
        return False


def transcribe(audio_bytes: bytes, format: str = "ogg",
               language: str | None = None) -> str:
    """Transcribe audio bytes to text.

    Returns transcribed text, or a string starting with ``[STT Error]``.
    Never raises.
    """
    if not audio_bytes:
        return "[STT Error] empty audio"

    try:
        import config
        lang = language or config.get("stt_language") or None
    except Exception:
        lang = language

    # Try local faster-whisper first
    if _check_faster_whisper():
        try:
            wav = _convert_to_wav(audio_bytes, format)
            return _transcribe_local(wav, lang)
        except Exception as e:
            _log.error("local STT failed: %s", e)
            # fall through to OpenAI

    # Fallback: OpenAI Whisper API
    try:
        import config
        api_key = config.get("stt_openai_key")
        if api_key:
            return _transcribe_openai(audio_bytes, format, lang, api_key)
    except Exception as e:
        _log.error("OpenAI STT failed: %s", e)

    return "[STT Error] no STT backend available. Install faster-whisper: pip install faster-whisper"


# ── internal helpers ────────────────────────────────────────────

def _convert_to_wav(audio_bytes: bytes, format: str) -> bytes:
    """Convert any audio to 16kHz mono WAV using ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=f".{format}", delete=False) as inp:
        inp.write(audio_bytes)
        inp_path = inp.name
    out_path = inp_path + ".wav"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", inp_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")[:500]
            raise RuntimeError(f"ffmpeg exit {proc.returncode}: {stderr}")
        return Path(out_path).read_bytes()
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found. Install it: brew install ffmpeg / apt install ffmpeg")
    finally:
        Path(inp_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def _get_model():
    """Lazy-load the faster-whisper model."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        try:
            import config
            model_size = config.get("stt_model") or "base"
        except Exception:
            model_size = "base"
        _log.info("loading whisper model: %s", model_size)
        _model = WhisperModel(model_size, compute_type="int8")
    return _model


def _transcribe_local(wav_bytes: bytes, language: str | None) -> str:
    """Transcribe WAV bytes using faster-whisper."""
    model = _get_model()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name
    try:
        kwargs = {}
        if language:
            kwargs["language"] = language
        segments, _info = model.transcribe(tmp_path, beam_size=5, **kwargs)
        text = " ".join(seg.text.strip() for seg in segments)
        return text.strip() or "[STT Error] no speech detected"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _transcribe_openai(audio_bytes: bytes, format: str,
                       language: str | None, api_key: str) -> str:
    """Transcribe via OpenAI Whisper API."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    buf = io.BytesIO(audio_bytes)
    buf.name = f"audio.{format}"
    kwargs = {"model": "whisper-1", "file": buf}
    if language:
        kwargs["language"] = language
    resp = client.audio.transcriptions.create(**kwargs)
    text = resp.text.strip()
    return text or "[STT Error] no speech detected"
