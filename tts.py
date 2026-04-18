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
        api_key = (config.get("tts_api_key") or "").strip()
        api_model = (config.get("tts_api_model") or "tts-1").strip()
        api_voice = (config.get("tts_api_voice") or "alloy").strip()
        ref_audio = config.get("tts_ref_audio") or ""
        ref_text = config.get("tts_ref_text") or ""
    except Exception as e:
        _log.error("TTS config error: %s", e)
        return None

    if not api_url:
        return None

    try:
        # Detect API style from URL path
        if "audio/speech" in api_url or (api_key and "/v1" in api_url and "tts" not in api_url):
            # OpenAI-compatible /v1/audio/speech (JSON, cloud TTS)
            audio = _synthesize_openai_speech(api_url, text, api_key, api_model, api_voice)
        elif "/v1/tts" in api_url:
            # Fish Speech /v1/tts (JSON body)
            audio = _synthesize_fish(api_url, text, ref_audio, ref_text)
        elif "/tts" in api_url or api_url.rstrip("/").endswith(":8000"):
            # Generic /tts endpoint — multipart with `text` + `prompt_audio`
            audio = _synthesize_prompt_audio(api_url, text, ref_audio)
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


def _synthesize_openai_speech(url: str, text: str, api_key: str,
                              model: str = "tts-1", voice: str = "alloy") -> bytes | None:
    """OpenAI-compatible /v1/audio/speech endpoint (JSON body, audio stream)."""
    # Normalize URL: accept both base URL and full path
    url = url.rstrip("/")
    if not url.endswith("/audio/speech"):
        if url.endswith("/v1"):
            url += "/audio/speech"
        elif "/v1" not in url:
            url += "/v1/audio/speech"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
    resp = requests.post(url, json=body, headers=headers, timeout=_TIMEOUT, stream=True)
    resp.raise_for_status()
    return b"".join(resp.iter_content(chunk_size=8192))


def _synthesize_fish(url: str, text: str, ref_audio: str, ref_text: str) -> bytes | None:
    """Fish Speech /v1/tts endpoint (JSON body, binary audio response)."""
    body: dict = {
        "text": text,
        "format": "mp3",
        "temperature": 0.7,
    }

    resp = requests.post(url, json=body, timeout=_TIMEOUT, stream=True)
    resp.raise_for_status()
    return b"".join(resp.iter_content(chunk_size=8192))


# Backward-compat alias
_synthesize_openai = _synthesize_fish


def _synthesize_prompt_audio(url: str, text: str, ref_audio: str) -> bytes | None:
    """Generic /tts endpoint: multipart form with `text` and `prompt_audio` (voice cloning).

    Matches the common pattern:
      curl -F "text=..." -F "prompt_audio=@ref.wav" -F "seed=42" http://host:8000/tts
    """
    url = url.rstrip("/")
    if not url.endswith("/tts"):
        url += "/tts"

    form: dict = {"text": (None, text), "seed": (None, "42")}

    if ref_audio and Path(ref_audio).is_file():
        try:
            audio_data = Path(ref_audio).read_bytes()
            form["prompt_audio"] = (Path(ref_audio).name, audio_data, "audio/wav")
        except Exception as e:
            _log.warning("Failed to load ref audio: %s", e)

    resp = requests.post(url, files=form, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content


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
