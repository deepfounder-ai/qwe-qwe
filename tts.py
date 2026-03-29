"""Text-to-Speech module — Fish Audio S2 Pro API."""

from __future__ import annotations

import logging

import requests

_log = logging.getLogger("tts")

_DEFAULT_API_URL = "https://api.fish.audio/v1/tts"
_MAX_TEXT_LENGTH = 2000
_TIMEOUT = 30


def is_available() -> bool:
    """True if TTS is configured and enabled."""
    try:
        import config
        enabled = str(config.get("tts_enabled")) == "1"
        has_key = bool(config.get("tts_api_key"))
        return enabled and has_key
    except Exception:
        return False


def synthesize(text: str, format: str = "mp3") -> bytes | None:
    """Synthesize text to audio bytes via Fish Audio S2 Pro.

    Returns audio bytes on success, None on failure.
    Never raises — errors are logged silently so text responses are not blocked.
    """
    if not text or not text.strip():
        return None

    try:
        import config
        api_key = config.get("tts_api_key")
        api_url = config.get("tts_api_url") or _DEFAULT_API_URL
        voice_id = config.get("tts_voice_id") or ""
    except Exception as e:
        _log.error("TTS config error: %s", e)
        return None

    if not api_key:
        return None

    # Truncate long texts
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH] + "..."

    body: dict = {
        "text": text,
        "format": format,
        "temperature": 0.7,
    }
    if voice_id:
        body["reference_id"] = voice_id

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": "s2-pro",
    }

    try:
        resp = requests.post(api_url, json=body, headers=headers,
                             timeout=_TIMEOUT, stream=True)
        resp.raise_for_status()
        audio = b"".join(resp.iter_content(chunk_size=8192))
        if not audio:
            _log.warning("TTS returned empty audio")
            return None
        _log.info("TTS synthesized %d bytes (%s)", len(audio), format)
        return audio
    except requests.RequestException as e:
        _log.error("TTS API error: %s", e)
        return None
