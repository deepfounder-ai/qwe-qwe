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


def _has_audio_decoder() -> bool:
    """Check if ffmpeg CLI or PyAV is available for audio decoding."""
    import shutil
    if shutil.which("ffmpeg") is not None:
        return True
    try:
        import av  # noqa: F401
        return True
    except ImportError:
        return False


def is_available() -> bool:
    """True if any STT backend is usable."""
    try:
        import config
        backend = (config.get("stt_backend") or "auto").lower()
        has_api_key = bool(config.get("stt_openai_key"))
    except Exception:
        backend = "auto"
        has_api_key = False

    if backend == "api":
        return has_api_key
    if backend == "local":
        return _check_faster_whisper() and _has_audio_decoder()
    # auto: any works
    if has_api_key:
        return True
    return _check_faster_whisper() and _has_audio_decoder()


def transcribe(audio_bytes: bytes, format: str = "ogg",
               language: str | None = None) -> str:
    """Transcribe audio bytes to text.

    Returns transcribed text, or a string starting with ``[STT Error]``.
    Never raises.
    """
    if not audio_bytes:
        return "[STT Error] empty audio"

    # Validate format against allowlist
    _ALLOWED_FORMATS = {"ogg", "mp3", "wav", "webm", "m4a", "flac", "opus", "oga"}
    format = format.lower().strip(".")
    if format not in _ALLOWED_FORMATS:
        return f"[STT Error] unsupported format: {format}"

    try:
        import config
        lang = language or config.get("stt_language") or None
        backend = (config.get("stt_backend") or "auto").lower()
        api_key = config.get("stt_openai_key") or ""
        api_url = (config.get("stt_api_url") or "").strip() or None
        api_model = config.get("stt_api_model") or "whisper-1"
    except Exception:
        lang = language
        backend = "auto"
        api_key = ""
        api_url = None
        api_model = "whisper-1"

    # Resolve backend: "auto" picks API if key is set, else local
    if backend == "auto":
        backend = "api" if api_key else "local"

    # Try API first if requested
    if backend == "api":
        if not api_key:
            return "[STT Error] API backend requires stt_openai_key"
        try:
            return _transcribe_api(audio_bytes, format, lang, api_key, api_url, api_model)
        except Exception as e:
            _log.error("API STT failed: %s", e)
            # Fall through to local if possible
            if not _check_faster_whisper():
                return f"[STT Error] API failed: {e}"

    # Local backend (or API fallback)
    if _check_faster_whisper():
        try:
            wav = _convert_to_wav(audio_bytes, format)
            return _transcribe_local(wav, lang)
        except Exception as e:
            _log.error("local STT failed: %s", e)
            # Last resort: try API if configured
            if api_key:
                try:
                    return _transcribe_api(audio_bytes, format, lang, api_key, api_url, api_model)
                except Exception as e2:
                    _log.error("API fallback also failed: %s", e2)

    return "[STT Error] no STT backend available. Install faster-whisper: pip install faster-whisper"


# ── internal helpers ────────────────────────────────────────────

def _convert_to_wav(audio_bytes: bytes, format: str) -> bytes:
    """Convert any audio to 16kHz mono WAV.
    Tries ffmpeg CLI first; falls back to PyAV (bundled ffmpeg libs)."""
    import shutil
    if shutil.which("ffmpeg") is not None:
        return _convert_via_ffmpeg_cli(audio_bytes, format)
    return _convert_via_pyav(audio_bytes, format)


def _convert_via_ffmpeg_cli(audio_bytes: bytes, format: str) -> bytes:
    """Convert using external ffmpeg binary."""
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
    finally:
        Path(inp_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def _convert_via_pyav(audio_bytes: bytes, format: str) -> bytes:
    """Convert using PyAV (bundled ffmpeg libs — no external binary needed)."""
    try:
        import av
    except ImportError:
        raise RuntimeError(
            "No audio decoder available. Install one of:\n"
            "  pip install av          # bundled ffmpeg libs (recommended)\n"
            "  or install ffmpeg and add to PATH"
        )

    import wave
    out_buf = io.BytesIO()
    with av.open(io.BytesIO(audio_bytes), format=format if format != "oga" else "ogg") as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        samples = []
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                samples.append(bytes(out_frame.planes[0]))
        # Flush resampler
        for out_frame in resampler.resample(None):
            samples.append(bytes(out_frame.planes[0]))

    pcm = b"".join(samples)
    with wave.open(out_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    return out_buf.getvalue()


def _get_model():
    """Lazy-load the faster-whisper model on CPU (works everywhere).

    Users with CUDA + cuDNN installed can set env QWE_STT_DEVICE=cuda for GPU.
    """
    global _model
    if _model is None:
        import os
        from faster_whisper import WhisperModel
        try:
            import config
            model_size = config.get("stt_model") or "base"
        except Exception:
            model_size = "base"
        device = os.environ.get("QWE_STT_DEVICE", "cpu")
        compute_type = "int8_float16" if device == "cuda" else "int8"
        _log.info("loading whisper model: %s (%s)", model_size, device)
        try:
            _model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as e:
            if device != "cpu":
                _log.warning("whisper %s failed (%s), falling back to CPU", device, e)
                _model = WhisperModel(model_size, device="cpu", compute_type="int8")
            else:
                raise
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


def _transcribe_api(audio_bytes: bytes, format: str,
                    language: str | None, api_key: str,
                    api_url: str | None = None, model: str = "whisper-1") -> str:
    """Transcribe via OpenAI-compatible API (OpenAI, Groq, self-hosted, etc.)."""
    from openai import OpenAI
    client_kwargs = {"api_key": api_key}
    if api_url:
        client_kwargs["base_url"] = api_url
    client = OpenAI(**client_kwargs)
    buf = io.BytesIO(audio_bytes)
    buf.name = f"audio.{format}"
    kwargs = {"model": model, "file": buf}
    if language:
        kwargs["language"] = language
    resp = client.audio.transcriptions.create(**kwargs)
    text = resp.text.strip()
    return text or "[STT Error] no speech detected"


# Backward-compat alias
_transcribe_openai = _transcribe_api
