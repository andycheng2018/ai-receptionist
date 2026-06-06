"""Personality Talker layer for the AI receptionist.

The deterministic receptionist engine still controls safety, extraction, and the
next question. This layer only makes the final spoken reply feel warmer and less
copy/paste. It is intentionally conservative:
- one short acknowledgement + one question
- no prices
- no appointment promises
- preserve the code-selected next question
- template mode is instant and default for live calls
- optional LLM mode is opt-in for smart/blocking demos
"""

from __future__ import annotations

import os
import re
import time
from hashlib import md5
from typing import Any

from app.models import LeadInfo
from app.llm_extractor import llm_available

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


MAX_REPLY_WORDS = 58


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _make_client():
    if OpenAI is None:
        return None
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("QWEN_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("QWEN_BASE_URL")
    if not api_key:
        return None
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _split_final_question(reply: str) -> tuple[str, str]:
    """Return (prefix, final_question). If there is no final question, question is ''."""
    text = re.sub(r"\s+", " ", (reply or "").strip())
    # Capture only the final question sentence, not the entire reply. The old
    # pattern could swallow the whole prefix when there was only one question,
    # causing availability replies to duplicate the safety sentence.
    match = re.search(r"([^.!?]*\?)\s*$", text)
    if not match:
        return text, ""
    question = match.group(1).strip()
    prefix = text[: match.start()].strip()
    return prefix, question


def _one_question_only(text: str) -> str:
    """Keep at most the final question mark in a reply."""
    text = re.sub(r"\s+", " ", text.strip())
    if text.count("?") <= 1:
        return text
    # Keep everything before the last question as statements by replacing earlier ?.
    last = text.rfind("?")
    before = text[:last].replace("?", ".")
    return before + text[last:]


def _trim_words(text: str, max_words: int = MAX_REPLY_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    # Prefer not to cut off the final question. If too long, keep the question
    # and a shorter acknowledgement.
    prefix, question = _split_final_question(text)
    if question:
        q_words = question.split()
        room = max(6, max_words - len(q_words))
        p_words = prefix.split()[:room]
        return " ".join(p_words + q_words)
    return " ".join(words[:max_words])


def _choose(options: list[str], key: str) -> str:
    if not options:
        return ""
    idx = int(md5(key.encode("utf-8")).hexdigest(), 16) % len(options)
    return options[idx]


def _lead_context_key(lead: LeadInfo) -> str:
    return "|".join(str(x or "") for x in [lead.service, lead.city, lead.project_scope, lead.intent, lead.urgency])


def _template_personality_reply(message: str, lead: LeadInfo, draft_reply: str) -> tuple[str, dict[str, Any]]:
    """Instant personality pass using controlled templates.

    This avoids slow model latency on phone calls while still making the reply
    feel less robotic than repeating the same canned sentence every turn.
    """
    prefix, question = _split_final_question(draft_reply)
    service = (lead.service or "").lower()
    scope = (lead.project_scope or "").lower()
    text = message.lower()
    key = message + _lead_context_key(lead)

    # Preserve very specific contact acknowledgements that tests and callers
    # expect, e.g. "Thanks, Andy. What is...".
    if draft_reply.startswith("Thanks,") or draft_reply.startswith("You’re welcome") or draft_reply.startswith("You're welcome"):
        return draft_reply, {"talker_source": "template_preserved", "talker_reason": "Preserved contact acknowledgement."}

    # Safety-critical price replies: warm them up, but preserve the safe pricing
    # text so it never sounds like a quote.
    if lead.intent == "price_question":
        base = "I don’t want to guess on price from just that."
        if "Pricing depends" in draft_reply:
            # Keep the original safe sentence verbatim after a short human opener.
            new_reply = f"{base} {draft_reply}"
        else:
            new_reply = draft_reply
        return _trim_words(_one_question_only(new_reply)), {"talker_source": "template", "talker_reason": "Warm safe-pricing opener."}

    if lead.intent == "unsafe_price_schedule_request":
        new_reply = draft_reply
        if not draft_reply.lower().startswith("i can’t promise") and not draft_reply.lower().startswith("i can't promise"):
            new_reply = "I can’t promise same-day availability or pricing from here. " + draft_reply
        return _trim_words(_one_question_only(new_reply)), {"talker_source": "template", "talker_reason": "Preserved safety wording."}

    if lead.intent == "availability_question":
        q = question or "What city is the project in?"
        return f"I can note that timing, but the team will need to confirm availability. {q}", {"talker_source": "template", "talker_reason": "Availability safety with warmer wording."}

    if lead.intent == "handoff_request" or lead.handoff_required:
        return draft_reply.replace("No problem.", "Of course — I’ll keep it simple."), {"talker_source": "template", "talker_reason": "Human handoff empathy."}

    if lead.intent == "incomplete_phone":
        q = question or "What’s the best full phone number for a callback?"
        ack = prefix if prefix and "interior painting" in prefix.lower() else "No worries — I just need the full number so the painter can follow up."
        return _trim_words(f"{ack} {q}"), {"talker_source": "template", "talker_reason": "Incomplete phone recovery."}

    # Vague paint-help openers should feel like a helpful receptionist, not a
    # form. This runs after safety/availability/handoff recovery branches so
    # it does not hide critical policy wording.
    if (lead.service or "").lower().strip() in {"", "painting project", "painting", "paint"} and "interior, exterior, cabinets, or touch-up" in draft_reply.lower():
        return "Sure — I can help with that. Is this for interior, exterior, cabinets, or touch-up painting?", {"talker_source": "template", "talker_reason": "Warm vague-service clarification."}

    # Rich project/personality acknowledgements. Keep the code-selected question.
    q = question or "May I get your name?"
    variants: list[str] = []

    if "repair" in service and "cabinet" in service:
        variants = [
            "Got it — possible repair, painting, and cabinet work.",
            "Understood — possible repair, painting, and cabinet work.",
            "Got it — I’ll note this as possible repair, painting, and cabinet work.",
        ]
    elif lead.repairs_needed and ("moisture" in text or "bubbling" in text or "stain" in text):
        city_part = f" in {lead.city}" if lead.city else ""
        if "touch-up" in service or "door painting" in service:
            variants = [
                f"Got it — touch-up and paint repair work{city_part}.",
                f"Understood — I’ll note the touch-up and paint repair details{city_part}.",
                f"Got it — I’ll mark this as touch-up and paint repair work{city_part}.",
            ]
        else:
            variants = [
                f"Got it — that may need prep or repair before repainting{city_part}.",
                f"Understood — bubbling or staining is worth flagging for the painter{city_part}.",
                f"Got it — I’ll mark this as possible paint repair and repainting{city_part}.",
            ]
    elif "+" in service or ("outside" in text and "inside" in text):
        variants = [
            "Got it — I’ll note both the outside and inside touch-up work.",
            "Understood — this sounds like a mixed interior and exterior touch-up.",
            "Got it — I’ll capture both parts of the project.",
        ]
    elif lead.property_type == "rental" and (lead.urgency in {"urgent", "soon"} or "tenant" in text):
        if lead.project_scope:
            variants = [
                f"Got it — {lead.project_scope} on a rental with a tight timeline.",
                f"Understood — I’ll note {lead.project_scope} on the rental with a tight timeline.",
                f"Got it — I’ll mark this as time-sensitive rental work for {lead.project_scope}.",
            ]
        else:
            variants = [
                "Got it — rental turnovers can be time-sensitive, so I’ll mark that clearly.",
                "Understood — I’ll note the rental timeline so the painter sees it.",
                "Got it — I’ll flag this as a rental project with timing in mind.",
            ]
    elif lead.property_type == "commercial office" or "commercial" in service:
        variants = [
            "Got it — I’ll note the business-hours constraint with the project.",
            "Understood — commercial scheduling matters, so I’ll capture that.",
            "Got it — I’ll mark this as a commercial painting request.",
        ]
    elif "exterior" in service:
        # Keep important expected words for tests and for clarity.
        if lead.urgency == "urgent":
            variants = [
                "Got it — exterior painting with a tight timeline.",
                "Understood — I’ll mark this as exterior painting with a tight timeline.",
            ]
        else:
            variants = [
                "Got it — exterior painting.",
                "Understood — I’ll note this as an exterior painting project.",
                "Got it — I can help collect the exterior painting details.",
            ]
    elif "interior" in service:
        if lead.project_scope:
            variants = [
                f"Got it — interior painting for {lead.project_scope}.",
                f"Understood — I’ll note the interior scope as {lead.project_scope}.",
                f"Got it — I can help with the {lead.project_scope} painting details.",
            ]
        else:
            variants = [
                "Got it — interior painting.",
                "Understood — I’ll note this as an interior painting project.",
                "Got it — I can help collect the interior painting details.",
            ]
    elif "cabinet" in service:
        variants = [
            "Got it — cabinet painting.",
            "Understood — I’ll note this as a cabinet painting request.",
            "Got it — I can help collect the cabinet painting details.",
        ]

    if variants and (prefix.startswith("Got it") or prefix.startswith("No problem") or prefix.startswith("Understood") or not prefix):
        new_prefix = _choose(variants, key)
        return _trim_words(_one_question_only(f"{new_prefix} {q}")), {"talker_source": "template", "talker_reason": "Context-specific personality acknowledgement."}

    # Generic fallback: preserve deterministic reply.
    return draft_reply, {"talker_source": "template_preserved", "talker_reason": "Reply already specific or safety-sensitive."}


def _llm_personality_reply(message: str, lead: LeadInfo, draft_reply: str, timeout_s: float) -> tuple[str | None, str | None]:
    client = _make_client()
    if client is None:
        return None, "LLM talker not configured."
    model = os.getenv("AI_TALKER_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("QWEN_MODEL") or "gpt-4.1-mini"
    lead_snapshot = {
        "service": lead.service,
        "city": lead.city,
        "property_type": lead.property_type,
        "project_scope": lead.project_scope,
        "timeline": lead.timeline,
        "urgency": lead.urgency,
        "intent": lead.intent,
        "repairs_needed": lead.repairs_needed,
    }
    system = (
        "You rewrite a painting-company receptionist reply to sound warm and natural. "
        "Keep it phone-friendly: max 28 words, one short acknowledgement plus the same final question. "
        "Do not add prices, guarantees, appointment promises, or extra questions. "
        "Do not change facts. If the draft is already safety/compliance wording, preserve the safety meaning. "
        "Return only the rewritten reply text."
    )
    user = (
        f"Customer message: {message}\n"
        f"Lead state: {lead_snapshot}\n"
        f"Draft reply: {draft_reply}\n"
        "Rewrite with personality but preserve the next question and safety constraints."
    )
    try:
        start = time.perf_counter()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.4,
            max_tokens=80,
            timeout=timeout_s,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return None, "LLM talker returned empty text."
        text = _one_question_only(text)
        text = _trim_words(text, 34)
        # Guardrails: reject if it invents dollar amounts or has too many questions.
        if re.search(r"\$\s*\d", text):
            return None, "Rejected LLM talker reply with dollar amount."
        if text.count("?") > 1:
            return None, "Rejected LLM talker reply with multiple questions."
        # Must preserve a final question if the draft had one.
        _, draft_q = _split_final_question(draft_reply)
        if draft_q and "?" not in text:
            return None, "Rejected LLM talker reply without required question."
        elapsed = int((time.perf_counter() - start) * 1000)
        return text, f"LLM personality rewrite in {elapsed} ms."
    except Exception as exc:  # pragma: no cover - network/API dependent
        return None, f"LLM talker failed: {exc!r}"


def personalize_reply(message: str, lead: LeadInfo, before: LeadInfo | None, draft_reply: str, *, live_mode: bool) -> tuple[str, dict[str, Any]]:
    """Return a warmer final reply plus debug metadata.

    Default behavior is instant template personality. Optional LLM rewriting can
    be enabled for smart demos with AI_TALKER_MODE=blocking. For live calls, the
    LLM talker is disabled unless AI_TALKER_LIVE=true because phone latency is
    more important than perfect wording.
    """
    enabled = _env_bool("AI_TALKER_ENABLED", True)
    if not enabled:
        return draft_reply, {"talker_source": "disabled", "talker_used": False, "talker_reason": "AI_TALKER_ENABLED=false"}

    # First produce a safe instant template version. This is the live default.
    template_reply, meta = _template_personality_reply(message, lead, draft_reply)
    meta.update({"talker_used": template_reply != draft_reply, "talker_latency_ms": 0})

    mode = os.getenv("AI_TALKER_MODE", "template").strip().lower()
    allow_live_llm = _env_bool("AI_TALKER_LIVE", False)
    if mode != "blocking" or (live_mode and not allow_live_llm) or not llm_available():
        return template_reply, meta

    timeout_s = float(os.getenv("AI_TALKER_TIMEOUT_SECONDS", "1.2"))
    start = time.perf_counter()
    llm_text, reason = _llm_personality_reply(message, lead, template_reply, timeout_s)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if llm_text:
        return llm_text, {
            "talker_source": "llm",
            "talker_used": True,
            "talker_latency_ms": elapsed_ms,
            "talker_reason": reason or "LLM personality rewrite applied.",
        }
    meta["talker_reason"] = f"{meta.get('talker_reason', '')} LLM talker not used: {reason}"
    meta["talker_latency_ms"] = elapsed_ms
    return template_reply, meta
