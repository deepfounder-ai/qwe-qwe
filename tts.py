"""Text-to-Speech module — s2.cpp / Fish Speech HTTP API."""

from __future__ import annotations

import logging
from pathlib import Path

import requests

_log = logging.getLogger("tts")

_MAX_TEXT_LENGTH = 2000
_TIMEOUT = 60


def is_available() -> bool:
    """True if TTS is enabled and API URL is configured."""
    try:
        import config
        if str(config.get("tts_enabled")) != "1":
            return False
        url = config.get("tts_api_url")
        return bool(url)
    except Exception:
        return False


def synthesize(text: str, format: str = "wav") -> bytes | None:
    """Synthesize text to audio bytes via TTS HTTP server.

    Supports two API styles:
    - OpenAI-compatible /v1/tts (JSON body)
    - s2.cpp /generate (multipart form-data)

    Returns audio bytes on success, None on failure.
    Never raises — errors are logged silently so text responses are not blocked.
    """
    if not text or not text.strip():
        return None

    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH]

    try:
        import config
        api_url = (config.get("tts_api_url") or "").strip()
        ref_audio = config.get("tts_ref_audio") or ""
        ref_text = config.get("tts_ref_text") or ""
    except Exception as e:
        _log.error("TTS config error: %s", e)
        return None

    if not api_url:
        return None

    try:
        # Detect API style from URL path
        if "/v1/" in api_url:
            # OpenAI-compatible endpoint — JSON body
            audio = _synthesize_openai(api_url, text, ref_audio, ref_text)
        else:
            # s2.cpp raw endpoint — multipart form-data
            audio = _synthesize_s2cpp(api_url, text, ref_audio, ref_text)

        if not audio:
            _log.warning("TTS returned empty audio")
            return None
        _log.info("TTS synthesized %d bytes", len(audio))
        return audio

    except requests.ConnectionError:
        _log.error("TTS server not reachable at %s", api_url)
        return None
    except requests.RequestException as e:
        _log.error("TTS API error: %s", e)
        return None


def _synthesize_openai(url: str, text: str, ref_audio: str, ref_text: str) -> bytes | None:
    """Fish Speech /v1/tts endpoint (JSON body, binary audio response)."""
    body: dict = {
        "text": text,
        "format": "mp3",
        "temperature": 0.7,
    }

    resp = requests.post(url, json=body, timeout=_TIMEOUT, stream=True)
    resp.raise_for_status()
    return b"".join(resp.iter_content(chunk_size=8192))


def _synthesize_s2cpp(url: str, text: str, ref_audio: str, ref_text: str) -> bytes | None:
    """s2.cpp /generate endpoint (multipart form-data, WAV response)."""
    url = url.rstrip("/")
    if not url.endswith("/generate"):
        url += "/generate"

    form: dict = {"text": (None, text)}

    if ref_audio and Path(ref_audio).is_file():
        try:
            audio_data = Path(ref_audio).read_bytes()
            form["reference"] = ("reference.wav", audio_data, "audio/wav")
            if ref_text:
                form["reference_text"] = (None, ref_text)
        except Exception as e:
            _log.warning("Failed to load ref audio: %s", e)

    resp = requests.post(url, files=form, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content
