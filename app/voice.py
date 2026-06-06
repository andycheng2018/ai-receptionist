<<<<<<< HEAD
from fastapi import APIRouter, Form, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather

from app.receptionist import handle_message


router = APIRouter()


def twiml_response(response: VoiceResponse) -> Response:
    """
    Converts Twilio VoiceResponse into XML response.
    """
    return Response(
        content=str(response),
        media_type="application/xml"
    )


def add_gather(response: VoiceResponse, prompt: str):
    """
    Adds a speech gather step.
    Twilio will speak the prompt, listen to the caller, then send the transcript
    to /voice/handle as SpeechResult.
    """
    gather = Gather(
        input="speech",
        action="/voice/handle",
        method="POST",
        speech_timeout="auto",
        timeout=5,
    )

    gather.say(
        prompt,
        voice="alice",
        language="en-US",
    )

    response.append(gather)

    # If the user says nothing, Twilio continues here.
    response.say(
        "I did not hear anything. Please say your painting request after the tone.",
        voice="alice",
        language="en-US",
    )

    response.redirect("/voice")

    return response


@router.post("/voice")
async def start_call(request: Request):
    """
    Entry point when someone calls your Twilio phone number.
    """
    response = VoiceResponse()

    greeting = (
        "Hi, thanks for calling. I am the AI receptionist for the painting business. "
        "Please tell me what painting project you need help with."
    )

    add_gather(response, greeting)

    return twiml_response(response)


@router.post("/voice/handle")
async def handle_speech(
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
):
    """
    Receives speech transcript from Twilio Gather.
    Uses CallSid as the session_id so every phone call has separate memory.
    """

    response = VoiceResponse()

    user_message = SpeechResult.strip()
    print("TWILIO SPEECH RESULT:", user_message)

    if not user_message:
        add_gather(
            response,
            "Sorry, I did not catch that. Please describe your painting project again."
        )
        return twiml_response(response)

    result = handle_message(
        session_id=CallSid,
        message=user_message,
    )

    bot_reply = result["reply"]

    # If lead is complete, finish call politely.
    if result["ready_to_send_to_painter"]:
        response.say(
            bot_reply,
            voice="alice",
            language="en-US",
        )
        response.say(
            "Thank you. The painter will follow up with you. Goodbye.",
            voice="alice",
            language="en-US",
        )
        response.hangup()
        return twiml_response(response)

    # Otherwise, answer and listen again.
    add_gather(response, bot_reply)

    return twiml_response(response)
=======
"""Twilio phone-call webhooks for the AI receptionist.

Architecture:
Twilio phone number -> /twilio/voice -> <Gather speech>
Caller speech -> /twilio/handle -> app.receptionist.handle_message
AI reply -> ElevenLabs MP3 if available -> Twilio <Play>
Fallback -> Twilio <Say>

For local testing, expose FastAPI with ngrok and set your Twilio number's
Voice webhook to: https://YOUR_NGROK_URL/twilio/voice using HTTP POST.
"""

from __future__ import annotations

import os
import re
import time
import uuid
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from twilio.twiml.voice_response import Gather, VoiceResponse

from app.receptionist import handle_message, get_lead
from app.tts import elevenlabs_configured, generate_elevenlabs_audio_bytes

router = APIRouter(tags=["twilio"])

@dataclass
class TwilioJob:
    job_id: str
    session_id: str
    user_message: str
    started_at: float = field(default_factory=time.perf_counter)
    ready: bool = False
    result: Optional[dict[str, Any]] = None
    bot_reply: str = ""
    audio_url: Optional[str] = None
    voice_provider: str = "pending"
    llm_latency_ms: Optional[int] = None
    tts_latency_ms: Optional[int] = None
    error: Optional[str] = None


_TWILIO_JOBS: dict[str, TwilioJob] = {}
_TWILIO_JOBS_LOCK = threading.Lock()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip().strip('"').strip("'")


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}






def _normalize_twilio_phone(value: str | None) -> str | None:
    """Return a caller phone number from Twilio's From field, or None.

    Twilio usually sends E.164 like +16505551234. Store it as-is so the
    business can call/text the same number back. Ignore anonymous/private
    callers and non-phone client IDs.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in {"anonymous", "unknown", "restricted", "private"}:
        return None
    # Twilio client/SIP identities can appear here; do not treat them as phones.
    if lowered.startswith(("client:", "sip:")):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def _attach_caller_phone(session_id: str, from_number: str | None) -> str | None:
    """Use the real caller ID as the callback phone number for phone calls.

    This prevents the receptionist from asking callers for a phone number we
    already have from Twilio. The caller can still correct it verbally later.
    """
    phone = _normalize_twilio_phone(from_number)
    if not phone:
        return None
    try:
        lead = get_lead(session_id)
        if not lead.phone:
            lead.phone = phone
            if "Caller phone captured automatically from Twilio caller ID." not in lead.notes:
                lead.notes.append("Caller phone captured automatically from Twilio caller ID.")
        return phone
    except Exception as exc:
        print("TWILIO caller phone capture failed:", repr(exc))
        return phone


def _reply_asks_for_phone(text: str) -> bool:
    t = (text or "").lower()
    return any(phrase in t for phrase in [
        "phone number", "callback number", "call back number", "number for a callback",
        "best number", "best phone", "reach you", "contact number",
    ])


def _phone_call_next_prompt(lead) -> str:
    """Choose a phone-call friendly next prompt when caller ID already gives phone."""
    if not getattr(lead, "name", None):
        return "May I get your name?"
    if not getattr(lead, "city", None):
        return "What city is the project in?"
    service = (getattr(lead, "service", None) or "").lower()
    if not service or service in {"painting", "paint", "painting project", "paint help"}:
        return "Is this for interior, exterior, cabinets, or touch-up painting?"
    if not getattr(lead, "timeline", None):
        return "When are you hoping to get this completed?"
    if getattr(lead, "photos_available", None) is None:
        return "Do you have any photos you can share?"
    return "Is there anything else you want me to note for the painter?"


def _sanitize_phone_call_reply(text: str, lead, caller_phone: str | None) -> str:
    """Do final phone-specific cleanup after the LLM/rule reply.

    If Twilio supplied caller ID, never ask for the caller's phone number.
    Replace that question with the next useful missing field.
    """
    text = _safe_twilio_text(text, max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260))
    if caller_phone and _reply_asks_for_phone(text):
        # Keep a short acknowledgement if present, then swap only the question.
        prefix = "Got it."
        if "—" in text:
            prefix = text.split("?", 1)[0].strip()
            # Avoid keeping the phone-question clause itself.
            if _reply_asks_for_phone(prefix):
                prefix = "Got it."
        return _safe_twilio_text(f"{prefix} {_phone_call_next_prompt(lead)}", max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260))
    return text


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip().strip('"').strip("'"))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip().strip('"').strip("'"))
    except ValueError:
        return default


def _phone_filler_text() -> str:
    return _env("TWILIO_FAST_ACK_TEXT", "Got it, one moment.")


def _twilio_say(parent, text: str):
    parent.say(
        _safe_twilio_text(text, max_chars=350),
        voice=_env("TWILIO_SAY_VOICE", "Polly.Joanna"),
        language=_env("TWILIO_SAY_LANGUAGE", "en-US"),
    )

def _twiml(response: VoiceResponse) -> Response:
    return Response(content=str(response), media_type="application/xml")


def _public_base_url(request: Request) -> str:
    """Return the public base URL Twilio can reach.

    With ngrok, either set TWILIO_PUBLIC_BASE_URL=https://...ngrok-free.app
    or let FastAPI infer from the Host header Twilio used.
    """
    configured = _env("TWILIO_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _audio_dir() -> Path:
    raw = _env("TWILIO_AUDIO_CACHE_DIR", ".cache/twilio_audio")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_old_audio(max_age_seconds: int = 60 * 60) -> None:
    now = time.time()
    try:
        for file in _audio_dir().glob("*.mp3"):
            if now - file.stat().st_mtime > max_age_seconds:
                file.unlink(missing_ok=True)
    except Exception:
        # Cleanup should never break calls.
        pass


def _safe_twilio_text(text: str, max_chars: int = 900) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    # Twilio <Say> handles normal punctuation well, but keep it short for phone.
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "."
    return text or "Sorry, I had trouble with that. Could you say that again?"


def _create_audio_url_from_base(public_base_url: str, text: str) -> tuple[Optional[str], Optional[int]]:
    """Generate ElevenLabs MP3, save it, and return a public URL and TTS latency.

    Returns (None, None) if ElevenLabs is disabled/not configured/fails.
    """
    if not _truthy(os.getenv("TWILIO_USE_ELEVENLABS"), True):
        return None, None
    if not elevenlabs_configured():
        return None, None

    try:
        _cleanup_old_audio()
        started = time.perf_counter()
        audio_bytes, headers = generate_elevenlabs_audio_bytes(text)
        latency_ms = int(headers.get("X-TTS-Latency-Ms", "0") or 0)
        if latency_ms <= 0:
            latency_ms = int((time.perf_counter() - started) * 1000)
        audio_id = uuid.uuid4().hex
        file_path = _audio_dir() / f"{audio_id}.mp3"
        file_path.write_bytes(audio_bytes)
        return f"{public_base_url.rstrip('/')}/twilio/audio/{audio_id}.mp3", latency_ms
    except Exception as exc:
        print("TWILIO ElevenLabs audio fallback:", repr(exc))
        return None, None


def _create_audio_url(request: Request, text: str) -> Optional[str]:
    audio_url, _latency = _create_audio_url_from_base(_public_base_url(request), text)
    return audio_url


def _say_or_play(request: Request, parent, text: str):
    """Append ElevenLabs <Play> when available, else Twilio <Say>."""
    text = _safe_twilio_text(text)
    audio_url = _create_audio_url(request, text)
    if audio_url:
        parent.play(audio_url)
        return {"voice_provider": "elevenlabs", "audio_url": audio_url}

    _twilio_say(parent, text)
    return {"voice_provider": "twilio_say", "audio_url": None}


def _add_gather(request: Request, response: VoiceResponse, prompt: str, call_sid: Optional[str] = None) -> VoiceResponse:
    """Speak prompt, gather caller speech, and post transcript to /twilio/handle."""
    action = f"{_public_base_url(request)}/twilio/handle"
    if call_sid:
        action += f"?session_id={call_sid}"

    gather_kwargs = {
        "input": "speech",
        "action": action,
        "method": "POST",
        "speech_timeout": _env("TWILIO_SPEECH_TIMEOUT", "auto"),
        "timeout": _int_env("TWILIO_GATHER_TIMEOUT", 5),
        "enhanced": True,
        "speech_model": _env("TWILIO_SPEECH_MODEL", "phone_call"),
        "language": _env("TWILIO_SPEECH_LANGUAGE", "en-US"),
        # actionOnEmptyResult keeps control in your app even if Twilio hears silence.
        "action_on_empty_result": True,
    }
    hints = _env("TWILIO_SPEECH_HINTS", "painting, painter, interior, exterior, cabinets, touch up, estimate, San Mateo, Daly City, Foster City, San Bruno, Redwood City")
    if hints:
        gather_kwargs["hints"] = hints
    # Partial-result callbacks are useful for future barge-in/streaming debugging.
    if _truthy(os.getenv("TWILIO_PARTIAL_RESULTS"), False):
        gather_kwargs["partial_result_callback"] = f"{_public_base_url(request)}/twilio/partial"
        gather_kwargs["partial_result_callback_method"] = "POST"
    gather = Gather(**gather_kwargs)
    _say_or_play(request, gather, prompt)
    response.append(gather)

    # If the caller is silent, repeat the loop politely.
    _say_or_play(request, response, "Sorry, I did not catch that. Please tell me how I can help with your painting project.")
    response.redirect(f"{_public_base_url(request)}/twilio/voice", method="POST")
    return response


@router.get("/twilio/audio/{audio_id}.mp3")
def get_twilio_audio(audio_id: str):
    """Public MP3 endpoint for Twilio <Play>. Exposed through ngrok in dev."""
    if not re.fullmatch(r"[a-f0-9]{32}", audio_id):
        raise HTTPException(status_code=404, detail="Audio not found")
    file_path = _audio_dir() / f"{audio_id}.mp3"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio expired or not found")
    return FileResponse(file_path, media_type="audio/mpeg")


@router.api_route("/twilio/voice", methods=["GET", "POST"])
async def twilio_voice(request: Request):
    """Incoming-call entry point for your Twilio phone number."""
    response = VoiceResponse()
    prompt = _env("TWILIO_GREETING", "Hi, my name is Mark. I’m the AI receptionist for the painting company. How can I help with your painting project today?")
    return _twiml(_add_gather(request, response, prompt))




def _job_ttl_cleanup(max_age_seconds: int = 10 * 60) -> None:
    now = time.perf_counter()
    with _TWILIO_JOBS_LOCK:
        stale = [job_id for job_id, job in _TWILIO_JOBS.items() if now - job.started_at > max_age_seconds]
        for job_id in stale:
            _TWILIO_JOBS.pop(job_id, None)


def _compute_twilio_job(job_id: str, session_id: str, user_message: str, public_base_url: str, caller_phone: str | None = None) -> None:
    """Run LLM + optional ElevenLabs audio off the Twilio request thread."""
    with _TWILIO_JOBS_LOCK:
        job = _TWILIO_JOBS.get(job_id)
    if job is None:
        return

    try:
        llm_started = time.perf_counter()
        result = handle_message(session_id=session_id, message=user_message, live_mode=True)
        llm_latency_ms = int((time.perf_counter() - llm_started) * 1000)
        bot_reply = result.get("reply") or "Sorry, I had trouble with that. Could you say it one more time?"
        bot_reply = _sanitize_phone_call_reply(bot_reply, result.get("lead"), caller_phone)

        audio_url = None
        tts_latency_ms = None
        voice_provider = "twilio_say"
        if _truthy(os.getenv("TWILIO_PREFETCH_ELEVENLABS"), True):
            audio_url, tts_latency_ms = _create_audio_url_from_base(public_base_url, bot_reply)
            if audio_url:
                voice_provider = "elevenlabs"

        with _TWILIO_JOBS_LOCK:
            job = _TWILIO_JOBS.get(job_id)
            if job:
                job.result = result
                job.bot_reply = bot_reply
                job.audio_url = audio_url
                job.voice_provider = voice_provider
                job.llm_latency_ms = llm_latency_ms
                job.tts_latency_ms = tts_latency_ms
                job.ready = True
    except Exception as exc:
        with _TWILIO_JOBS_LOCK:
            job = _TWILIO_JOBS.get(job_id)
            if job:
                job.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                job.bot_reply = "Sorry, I had trouble with that. Could you say it one more time?"
                job.voice_provider = "twilio_say"
                job.ready = True


def _start_twilio_job(session_id: str, user_message: str, public_base_url: str, caller_phone: str | None = None) -> str:
    _job_ttl_cleanup()
    job_id = uuid.uuid4().hex
    job = TwilioJob(job_id=job_id, session_id=session_id, user_message=user_message)
    with _TWILIO_JOBS_LOCK:
        _TWILIO_JOBS[job_id] = job
    thread = threading.Thread(
        target=_compute_twilio_job,
        args=(job_id, session_id, user_message, public_base_url, caller_phone),
        daemon=True,
    )
    thread.start()
    return job_id


def _append_job_reply(request: Request, response: VoiceResponse, job: TwilioJob) -> VoiceResponse:
    """Append ready job reply and either hang up or gather next turn."""
    result = job.result or {}
    bot_reply = job.bot_reply or "Sorry, I had trouble with that. Could you say it one more time?"

    if job.audio_url:
        response.play(job.audio_url)
    else:
        _twilio_say(response, bot_reply)

    lead = result.get("lead") if isinstance(result, dict) else None
    # Phone calls should not hang up just because the lead looks complete. A real
    # caller may want to add details or correct something. Keep listening unless
    # TWILIO_AUTO_HANGUP=true, or the caller explicitly says goodbye/end the call.
    done = False
    if isinstance(result, dict) and _truthy(os.getenv("TWILIO_AUTO_HANGUP"), False):
        done = bool(result.get("ready_to_send_to_painter"))

    print(
        f"TWILIO latency job={job.job_id} llm_ms={job.llm_latency_ms} "
        f"tts_ms={job.tts_latency_ms} total_ms={int((time.perf_counter() - job.started_at)*1000)} "
        f"voice={job.voice_provider} error={job.error}"
    )

    if done:
        _say_or_play(request, response, _env("TWILIO_GOODBYE", "Thank you. The team will follow up with you. Goodbye."))
        response.hangup()
        return response

    # Gather next caller turn. Empty prompt means the prior reply already asked the question.
    action = f"{_public_base_url(request)}/twilio/handle?session_id={job.session_id}"
    gather = Gather(
        input="speech",
        action=action,
        method="POST",
        speech_timeout=_env("TWILIO_SPEECH_TIMEOUT", "auto"),
        timeout=_int_env("TWILIO_GATHER_TIMEOUT", 5),
        enhanced=True,
        speech_model=_env("TWILIO_SPEECH_MODEL", "phone_call"),
        language=_env("TWILIO_SPEECH_LANGUAGE", "en-US"),
        action_on_empty_result=True,
        hints=_env("TWILIO_SPEECH_HINTS", "painting, painter, interior, exterior, cabinets, touch up, estimate, San Mateo, Daly City, Foster City, San Bruno, Redwood City"),
    )
    # A tiny pause helps Twilio start listening cleanly after playback.
    gather.pause(length=_float_env("TWILIO_POST_REPLY_PAUSE", 0.1))
    response.append(gather)
    response.redirect(f"{_public_base_url(request)}/twilio/voice", method="POST")
    return response

@router.post("/twilio/handle")
async def twilio_handle(
    request: Request,
    CallSid: str = Form(default=""),
    SpeechResult: str = Form(default=""),
    Confidence: str = Form(default=""),
    From: str = Form(default=""),
):
    """Receive speech transcript from Twilio Gather and continue the call."""
    session_id = request.query_params.get("session_id") or CallSid or "twilio-default"
    caller_phone = _attach_caller_phone(session_id, From)
    user_message = (SpeechResult or "").strip()
    print(f"TWILIO speech session={session_id} from={caller_phone or From or 'unknown'} confidence={Confidence}: {user_message}")

    response = VoiceResponse()

    if not user_message:
        return _twiml(_add_gather(request, response, "Sorry, I did not catch that. Could you say that again?", session_id))

    # Fast-ack mode improves perceived latency: Twilio hears a quick filler immediately
    # while LLM + ElevenLabs run in a background thread.
    if _truthy(os.getenv("TWILIO_FAST_ACK"), False):
        job_id = _start_twilio_job(session_id, user_message, _public_base_url(request), caller_phone)
        _twilio_say(response, _phone_filler_text())
        response.pause(length=_float_env("TWILIO_FAST_ACK_PAUSE", 0.15))
        response.redirect(f"{_public_base_url(request)}/twilio/wait/{job_id}", method="POST")
        return _twiml(response)

    # Simple mode: no filler. Wait for the real reply once, then speak it.
    # This feels less awkward than saying a filler phrase, and is usually fast enough
    # if OPENAI_FAST_MODEL and ELEVENLABS_MODEL are set to low-latency models.
    total_started = time.perf_counter()
    llm_started = time.perf_counter()
    result = handle_message(session_id=session_id, message=user_message, live_mode=True)
    bot_reply = result.get("reply") or "Sorry, I had trouble with that. Could you say it one more time?"
    lead = result.get("lead")
    bot_reply = _sanitize_phone_call_reply(bot_reply, lead, caller_phone)
    llm_ms = int((time.perf_counter() - llm_started) * 1000)
    print(f"TWILIO simple_mode llm_ms={llm_ms}")
    # Do not hang up automatically in phone mode unless explicitly enabled.
    done = bool(result.get("ready_to_send_to_painter")) and _truthy(os.getenv("TWILIO_AUTO_HANGUP"), False)

    if done:
        _say_or_play(request, response, bot_reply)
        _say_or_play(request, response, _env("TWILIO_GOODBYE", "Thank you. The team will follow up with you. Goodbye."))
        response.hangup()
        return _twiml(response)

    return _twiml(_add_gather(request, response, bot_reply, session_id))


@router.api_route("/twilio/wait/{job_id}", methods=["GET", "POST"])
async def twilio_wait(request: Request, job_id: str, attempt: int = 0):
    """Poll a background job and play the answer when ready."""
    response = VoiceResponse()
    with _TWILIO_JOBS_LOCK:
        job = _TWILIO_JOBS.get(job_id)

    if job is None:
        return _twiml(_add_gather(request, response, "Sorry, I lost my place for a moment. Could you say that again?"))

    if not job.ready:
        max_attempts = _int_env("TWILIO_WAIT_MAX_ATTEMPTS", 6)
        if attempt >= max_attempts:
            _twilio_say(response, "Sorry, this is taking longer than expected. Let me collect your phone number so the team can follow up.")
            return _twiml(_add_gather(request, response, "What is the best phone number for a callback?", job.session_id))
        response.pause(length=_float_env("TWILIO_WAIT_POLL_SECONDS", 0.6))
        response.redirect(f"{_public_base_url(request)}/twilio/wait/{job_id}?attempt={attempt + 1}", method="POST")
        return _twiml(response)

    # Job is ready. Remove it before replying so duplicate Twilio retries don't replay forever.
    with _TWILIO_JOBS_LOCK:
        _TWILIO_JOBS.pop(job_id, None)
    return _twiml(_append_job_reply(request, response, job))


@router.post("/twilio/partial")
async def twilio_partial(request: Request):
    """Debug endpoint for Twilio partial speech results.

    This does not interrupt playback yet, but it is the hook needed for true
    barge-in work with Media Streams / ConversationRelay later.
    """
    form = await request.form()
    print("TWILIO partial:", dict(form))
    return PlainTextResponse("ok")


# Backwards-compatible aliases for the older demo endpoints.
@router.api_route("/voice", methods=["GET", "POST"])
async def legacy_voice(request: Request):
    return await twilio_voice(request)


@router.post("/voice/handle")
async def legacy_voice_handle(
    request: Request,
    CallSid: str = Form(default=""),
    SpeechResult: str = Form(default=""),
    Confidence: str = Form(default=""),
):
    return await twilio_handle(request, CallSid=CallSid, SpeechResult=SpeechResult, Confidence=Confidence)


@router.get("/twilio/status")
def twilio_status(request: Request):
    """Safe debug endpoint for setup checks."""
    return {
        "ok": True,
        "public_base_url": _public_base_url(request),
        "twilio_use_elevenlabs": _truthy(os.getenv("TWILIO_USE_ELEVENLABS"), True),
        "elevenlabs_configured": elevenlabs_configured(),
        "voice_webhook_url": f"{_public_base_url(request)}/twilio/voice",
        "audio_cache_dir": str(_audio_dir()),
        "fallback_voice": _env("TWILIO_SAY_VOICE", "Polly.Joanna"),
        "fast_ack_enabled": _truthy(os.getenv("TWILIO_FAST_ACK"), False),
        "fast_ack_text": _phone_filler_text(),
        "prefetch_elevenlabs": _truthy(os.getenv("TWILIO_PREFETCH_ELEVENLABS"), True),
        "speech_timeout": _env("TWILIO_SPEECH_TIMEOUT", "auto"),
        "gather_timeout": _int_env("TWILIO_GATHER_TIMEOUT", 5),
        "wait_poll_seconds": _float_env("TWILIO_WAIT_POLL_SECONDS", 0.6),
        "wait_max_attempts": _int_env("TWILIO_WAIT_MAX_ATTEMPTS", 6),
        "recommended_phone_settings": {
            "TWILIO_FAST_ACK": "false",
            "OPENAI_FAST_MODEL": os.getenv("OPENAI_FAST_MODEL", "gpt-4.1-mini"),
            "ELEVENLABS_MODEL": os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            "TWILIO_MAX_REPLY_CHARS": _int_env("TWILIO_MAX_REPLY_CHARS", 260),
        },
        "auto_hangup_enabled": _truthy(os.getenv("TWILIO_AUTO_HANGUP"), False),
        "caller_id_phone_capture": True,
        "barge_in_note": "This simple webhook flow uses turn-taking. For real interruption while audio is playing, use Twilio Media Streams / ConversationRelay later.",
    }
>>>>>>> 3895666 (Deploy AI receptionist)
