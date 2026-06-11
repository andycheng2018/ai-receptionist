"""Twilio phone-call webhooks for the AI receptionist.

Architecture:
Twilio phone number -> /twilio/voice -> <Gather speech>
Caller speech -> /twilio/handle -> app.llm_receptionist.handle_message
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

from app.llm_receptionist import (
    handle_message,
    get_lead,
    last_ai_reply,
    missing_fields,
    score_lead,
    build_final_call_json,
    add_transcript_message,
)
from app.database import save_lead_to_db, save_final_call_json
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


def _phone_goodbye_signal(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    exact = {
        "no", "nope", "nah", "that's all", "thats all", "that is all",
        "nothing else", "no that's all", "no thats all", "all good",
        "i'm good", "im good", "we're good", "were good", "thank you", "thanks",
        "bye", "goodbye", "that'll be all", "that will be all",
    }
    if t in exact:
        return True
    return any(p in t for p in [
        "nothing else", "that's all", "thats all", "all set", "i'm all set",
        "im all set", "we are good", "we're good", "you can hang up", "end the call",
        "bye", "goodbye",
    ])


def _is_final_confirmation_context(previous_ai: str | None) -> bool:
    t = (previous_ai or "").lower()
    return any(p in t for p in [
        "anything else", "anything you'd like", "anything you would like",
        "else you'd like", "else you would like", "note for the painter",
        "add for the painter", "anything else you want me to note",
    ])


def _phone_lead_ready_for_wrapup(lead) -> bool:
    """Phone-specific completeness check.

    Twilio caller ID usually provides phone, so the bot should not keep asking
    for estimator details forever. Once we have the basic callback lead, do a
    final satisfaction check and then end politely when the caller is done.
    """
    service = (getattr(lead, "service", None) or "").lower().strip()
    specific_service = bool(service and service not in {"painting", "paint", "painting project", "paint help"})
    return bool(
        specific_service
        and getattr(lead, "city", None)
        and getattr(lead, "name", None)
        and getattr(lead, "phone", None)
        and getattr(lead, "timeline", None)
    )


def _final_check_prompt(lead) -> str:
    city = getattr(lead, "city", None)
    service = getattr(lead, "service", None)
    project = ""
    if service and city:
        project = f" for the {service} in {city}"
    elif service:
        project = f" for the {service}"
    return _safe_twilio_text(
        f"Great, I have the main details{project}. Is there anything else you'd like me to note for the painter?",
        max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260),
    )


def _save_phone_lead_if_needed(session_id: str, lead) -> None:
    try:
        lead = score_lead(lead)
        missing = missing_fields(lead)
        ready = len(missing) == 0 or _phone_lead_ready_for_wrapup(lead)
        if not getattr(lead, "saved", False):
            final_call_json = build_final_call_json(session_id, lead, missing, ready, None)
            save_lead_to_db(session_id, lead, final_call_json)
            save_final_call_json(session_id, final_call_json)
            lead.saved = True
    except Exception as exc:
        print("TWILIO final lead save failed:", repr(exc))


def _phone_call_next_prompt(lead) -> str:
    """Choose a phone-call friendly next prompt when caller ID already gives phone."""
    service = (getattr(lead, "service", None) or "").lower()
    if not service or service in {"painting", "paint", "painting project", "paint help"}:
        return "Is this for interior, exterior, cabinets, or touch-up painting?"
    if not getattr(lead, "city", None):
        return "What city is the project in?"
    if not getattr(lead, "timeline", None):
        return "When are you hoping to get this completed?"
    if not getattr(lead, "name", None):
        return "May I get your name?"
    return "Is there anything else you'd like me to note for the painter?"


def _sanitize_phone_call_reply(text: str, lead, caller_phone: str | None) -> str:
    """Do final phone-specific cleanup after the LLM/rule reply.

    If Twilio supplied caller ID, never ask for the caller's phone number.
    Once the phone lead has the basics, ask one final confirmation question
    instead of continuing to collect optional estimator details forever.
    """
    text = _safe_twilio_text(text, max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260))
    if caller_phone and _reply_asks_for_phone(text):
        prefix = "Got it."
        if "—" in text:
            prefix = text.split("?", 1)[0].strip()
            if _reply_asks_for_phone(prefix):
                prefix = "Got it."
        text = f"{prefix} {_phone_call_next_prompt(lead)}"

    if lead and _phone_lead_ready_for_wrapup(lead):
        lower = text.lower()
        if not _is_final_confirmation_context(text):
            # Preserve a short useful acknowledgement if it is not just another question.
            if "?" in text:
                prefix = text.split("?", 1)[0].strip()
                if not prefix or _reply_asks_for_phone(prefix):
                    prefix = "Great, I have the main details."
                return _safe_twilio_text(f"{prefix}. {_final_check_prompt(lead)}", max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260))
            return _safe_twilio_text(f"{text.rstrip(' .')}. {_final_check_prompt(lead)}", max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260))

    return _safe_twilio_text(text, max_chars=_int_env("TWILIO_MAX_REPLY_CHARS", 260))


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
        result = handle_message(session_id=session_id, message=user_message)
        llm_latency_ms = int((time.perf_counter() - llm_started) * 1000)
        bot_reply = result.get("reply") or "Sorry, I had trouble with that. Could you say it one more time?"
        if not result.get("should_end"):
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
    # Hang up only when the receptionist explicitly marks the conversation as
    # finished. ready_to_send_to_painter only means the lead is complete; the bot
    # may still need to ask the one final notes question.
    done = bool(isinstance(result, dict) and result.get("should_end"))

    print(
        f"TWILIO latency job={job.job_id} llm_ms={job.llm_latency_ms} "
        f"tts_ms={job.tts_latency_ms} total_ms={int((time.perf_counter() - job.started_at)*1000)} "
        f"voice={job.voice_provider} error={job.error}"
    )

    if done:
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

    previous_ai = last_ai_reply(session_id)
    current_lead = get_lead(session_id)
    if _phone_goodbye_signal(user_message) and (_is_final_confirmation_context(previous_ai) or _phone_lead_ready_for_wrapup(current_lead)):
        add_transcript_message(session_id, "customer", user_message)
        goodbye = _env("TWILIO_GOODBYE", "Perfect — I have the details. The painter will follow up with you. Thanks for calling, goodbye.")
        add_transcript_message(session_id, "ai", goodbye)
        _save_phone_lead_if_needed(session_id, current_lead)
        _say_or_play(request, response, goodbye)
        response.hangup()
        return _twiml(response)

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
    llm_started = time.perf_counter()
    result = handle_message(session_id=session_id, message=user_message)
    bot_reply = result.get("reply") or "Sorry, I had trouble with that. Could you say it one more time?"
    lead = result.get("lead")
    if not result.get("should_end"):
        bot_reply = _sanitize_phone_call_reply(bot_reply, lead, caller_phone)
    llm_ms = int((time.perf_counter() - llm_started) * 1000)
    print(f"TWILIO simple_mode llm_ms={llm_ms}")
    done = bool(result.get("should_end"))

    if done:
        _say_or_play(request, response, bot_reply)
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
