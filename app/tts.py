import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

# Creates a group of FastAPI routes that start with /tts
router = APIRouter(prefix="/tts", tags=["tts"])


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice_id: Optional[str] = None

# Functions read .env values safely

def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip().strip('"').strip("'"))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip().strip('"').strip("'"))
    except ValueError:
        return default


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip().strip('"').strip("'")


# Check if ElevenLabs is ready
def elevenlabs_configured() -> bool:
    return bool(_env("ELEVENLABS_API_KEY") and _env("ELEVENLABS_VOICE_ID"))

# Audio caching (if same text is spoken again)
def _cache_dir() -> Path:
    # Use project-local cache by default. If that fails, fall back to /tmp.
    raw = _env("ELEVENLABS_CACHE_DIR", ".cache/elevenlabs_tts")
    try:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        path = Path("/tmp/ai_receptionist_elevenlabs_tts")
        path.mkdir(parents=True, exist_ok=True)
        return path

# Creates a unique filenmae for each audio request
def _cache_key(text: str, voice_id: str, model_id: str, output_format: str) -> str:
    raw = json.dumps(
        {
            "text": text,
            "voice_id": voice_id,
            "model_id": model_id,
            "output_format": output_format,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Generic ElevenLabs request helper
def _elevenlabs_request(
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    timeout: float = 15,
    accept: str = "application/json",
):
    """Call ElevenLabs and return (status, content_type, bytes).

    This helper intentionally returns detailed upstream errors so the demo UI
    can show whether the issue is an invalid key, quota, model access, etc.
    """
    api_key = _env("ELEVENLABS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY is not configured")

    url = "https://api.elevenlabs.io" + path
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "xi-api-key": api_key,
        "Accept": accept,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    req = urllib_request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            return resp.status, content_type, data
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        # Preserve the upstream status code for debugging instead of hiding it as generic 500.
        raise HTTPException(status_code=exc.code, detail=f"ElevenLabs API error {exc.code}: {detail}")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"ElevenLabs request failed: {type(exc).__name__}: {str(exc)[:500]}",
        )


# Parse ElevenLabs voice-list responses
def _parse_json_response(data: bytes, content_type: str, source: str) -> dict:
    """Parse JSON and include a body preview if ElevenLabs returns text/HTML."""
    raw_text = data.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw_text)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Could not parse ElevenLabs {source} response",
                "content_type": content_type,
                "body_preview": raw_text[:1000],
            },
        )

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"ElevenLabs {source} response was not a JSON object",
                "content_type": content_type,
                "body_preview": raw_text[:1000],
            },
        )

    return parsed


def _extract_voices(parsed: dict) -> list[dict]:
    """Support both newer v2 and legacy v1 voices response shapes."""
    voices = parsed.get("voices")
    if isinstance(voices, list):
        return voices

    # Defensive fallback in case an SDK/proxy shape wraps data differently.
    data = parsed.get("data")
    if isinstance(data, dict) and isinstance(data.get("voices"), list):
        return data["voices"]
    if isinstance(data, list):
        return data

    raise HTTPException(
        status_code=500,
        detail={
            "message": "ElevenLabs voices response did not contain a voices list",
            "keys": list(parsed.keys()),
            "body_preview": json.dumps(parsed)[:1000],
        },
    )


def fetch_elevenlabs_voices() -> list[dict]:
    """Fetch available ElevenLabs voices.

    Prefer the newer /v2/voices endpoint. If that is unavailable for an account
    or returns a legacy-style issue, fall back to /v1/voices.
    """
    # v2 endpoint supports pagination; page_size keeps the response small.
    v2_path = "/v2/voices?" + urlencode({"page_size": 100})
    try:
        _status, content_type, data = _elevenlabs_request(v2_path, timeout=15)
        parsed = _parse_json_response(data, content_type, "v2 voices")
        return _extract_voices(parsed)
    except HTTPException as v2_error:
        # Some accounts or older deployments may still behave better on v1.
        # Try v1 before surfacing the v2 error.
        try:
            _status, content_type, data = _elevenlabs_request("/v1/voices", timeout=15)
            parsed = _parse_json_response(data, content_type, "v1 voices")
            return _extract_voices(parsed)
        except HTTPException:
            raise v2_error


@router.get("/config")
def get_tts_config():
    """Expose safe TTS status for the demo UI; never returns the API key."""
    return {
        "provider": "elevenlabs",
        "configured": elevenlabs_configured(),
        "api_key_configured": bool(_env("ELEVENLABS_API_KEY")),
        "voice_id_configured": bool(_env("ELEVENLABS_VOICE_ID")),
        "voice_id_preview": (_env("ELEVENLABS_VOICE_ID")[:6] + "...") if _env("ELEVENLABS_VOICE_ID") else None,
        "model": _env("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        "output_format": _env("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128"),
        "cache_enabled": _truthy(os.getenv("ELEVENLABS_TTS_CACHE"), True),
    }


@router.get("/voices")
def list_voices():
    """List available ElevenLabs voices so the student can verify/copy a valid voice_id."""
    voices = fetch_elevenlabs_voices()
    return {
        "voices": [
            {
                "name": v.get("name"),
                "voice_id": v.get("voice_id"),
                "category": v.get("category"),
            }
            for v in voices
        ],
        "count": len(voices),
    }


@router.get("/diagnose")
def diagnose_tts():
    """Validate the configured key/voice/model without speaking a full customer reply."""
    config = get_tts_config()
    if not config["api_key_configured"]:
        return JSONResponse(status_code=500, content={"ok": False, "error": "ELEVENLABS_API_KEY missing", "config": config})
    if not config["voice_id_configured"]:
        return JSONResponse(status_code=500, content={"ok": False, "error": "ELEVENLABS_VOICE_ID missing", "config": config})

    # Check voices endpoint first; it gives a clearer error for bad API keys.
    voices = fetch_elevenlabs_voices()
    configured_voice = _env("ELEVENLABS_VOICE_ID")
    found = any(v.get("voice_id") == configured_voice for v in voices)

    return {
        "ok": True,
        "config": config,
        "voice_found_in_account": found,
        "available_voice_count": len(voices),
        "hint": "If voice_found_in_account is false, copy a valid voice_id from /tts/voices or ElevenLabs → Voices → Copy voice ID.",
    }


def generate_elevenlabs_audio_bytes(text: str, voice_id: Optional[str] = None) -> tuple[bytes, dict[str, str]]:
    """Return MP3 bytes for text using ElevenLabs.

    Shared by the browser /tts/speak endpoint and the Twilio phone webhook.
    Raises HTTPException with a clear detail if ElevenLabs cannot generate audio.
    """
    api_key = _env("ELEVENLABS_API_KEY")
    default_voice_id = _env("ELEVENLABS_VOICE_ID")
    voice_id = (voice_id or default_voice_id or "").strip()

    if not api_key:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY is not configured")
    if not voice_id:
        raise HTTPException(status_code=500, detail="ELEVENLABS_VOICE_ID is not configured")

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    max_chars = _int_env("ELEVENLABS_MAX_CHARS", 700)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."

    model_id = _env("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    output_format = _env("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    enable_cache = _truthy(os.getenv("ELEVENLABS_TTS_CACHE"), True)

    cache_path = None
    if enable_cache:
        key = _cache_key(text, voice_id, model_id, output_format)
        cache_path = _cache_dir() / f"{key}.mp3"
        if cache_path.exists():
            return cache_path.read_bytes(), {
                "X-TTS-Provider": "elevenlabs",
                "X-TTS-Cache": "hit",
                "X-TTS-Latency-Ms": "0",
            }

    query = urlencode({"output_format": output_format})
    latency_opt = _env("ELEVENLABS_OPTIMIZE_STREAMING_LATENCY", "")
    if latency_opt:
        query += "&" + urlencode({"optimize_streaming_latency": latency_opt})

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?{query}"
    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": _float_env("ELEVENLABS_STABILITY", 0.50),
            "similarity_boost": _float_env("ELEVENLABS_SIMILARITY_BOOST", 0.75),
            "style": _float_env("ELEVENLABS_STYLE", 0.10),
            "use_speaker_boost": _truthy(os.getenv("ELEVENLABS_SPEAKER_BOOST"), True),
        },
    }

    payload = json.dumps(body).encode("utf-8")
    http_req = urllib_request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )

    started = time.perf_counter()
    try:
        with urllib_request.urlopen(http_req, timeout=_float_env("ELEVENLABS_TIMEOUT_SECONDS", 15.0)) as resp:
            content_type = resp.headers.get("Content-Type", "")
            audio_bytes = resp.read()
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise HTTPException(status_code=exc.code, detail=f"ElevenLabs TTS failed: {detail}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ElevenLabs TTS failed: {type(exc).__name__}: {str(exc)[:500]}")

    latency_ms = int((time.perf_counter() - started) * 1000)
    content_type_lower = (content_type or "").lower()

    if not audio_bytes or ("audio" not in content_type_lower and "mpeg" not in content_type_lower and "octet-stream" not in content_type_lower):
        preview = audio_bytes[:500].decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=500,
            detail=f"ElevenLabs did not return audio. Content-Type={content_type}. Body={preview}",
        )

    if cache_path is not None:
        try:
            cache_path.write_bytes(audio_bytes)
        except Exception:
            pass

    return audio_bytes, {
        "X-TTS-Provider": "elevenlabs",
        "X-TTS-Cache": "miss" if enable_cache else "disabled",
        "X-TTS-Latency-Ms": str(latency_ms),
    }


@router.post("/speak")
def speak(req: TTSRequest):
    """Generate ElevenLabs speech for a receptionist reply."""
    audio_bytes, headers = generate_elevenlabs_audio_bytes(req.text, req.voice_id)
    return Response(content=audio_bytes, media_type="audio/mpeg", headers=headers)
