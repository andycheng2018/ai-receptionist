"""LLM-first live receptionist layer.

This v19 layer intentionally stops trying to solve nuanced conversation with
regex. A fast LLM produces a short customer reply plus a structured lead patch.
Deterministic code still validates/sanitizes safety-critical behavior.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

from app.models import LeadInfo
from app.faq_sheet import FAQ_SHEET_TEXT
from app.ai_response_cache import make_cache_key, get as cache_get, set as cache_set

load_dotenv()


def _client():
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


def llm_first_available() -> bool:
    return _client() is not None


PATCHABLE_FIELDS = {
    "intent", "name", "phone", "email", "address", "city", "service", "property_type",
    "stories", "room_size_sqft", "rooms", "walls_only", "ceiling", "trim", "occupied",
    "repairs_needed", "photos_available", "cabinet_count", "project_scope", "timeline",
    "urgency", "preferred_callback_time", "handoff_required", "notes",
}

BOOL_FIELDS = {"walls_only", "ceiling", "trim", "occupied", "repairs_needed", "photos_available", "handoff_required"}
INT_FIELDS = {"stories", "room_size_sqft", "rooms", "cabinet_count"}
CONTACT_FIELDS = {"name", "phone", "email"}


PROMPT_VERSION = "v20_faq_cache_safety"

SYSTEM_PROMPT = """
You are the live AI receptionist for a painting contractor.

Your job on every turn:
1. Understand the customer's message using context.
2. Return a short, warm, spoken reply with personality.
3. Return a structured patch to update the lead.

Critical rules:
- Reply in 1-2 short sentences, under 35 words when possible.
- Ask exactly ONE next question, unless the customer is only asking a FAQ.
- Never give a dollar estimate or price range. Say pricing depends on details and collect info.
- Never promise exact availability or appointments. You can note preferred times and say someone will confirm.
- Do not infer a name from emotions or descriptions. "I'm stressed" is not a name.
- Project city beats caller city. Example: "I live in San Jose but property is in San Mateo" => city San Mateo.
- If the customer corrects themselves (actually, forget that, I meant), the latest correction wins and stale conflicting fields should be cleared.
- For vague requests like "help with paint", ask whether it is interior, exterior, cabinets, or touch-up.
- Keep the tone warm and practical, not overly chatty.

Lead patch format:
{
  "reply": "short customer-facing reply",
  "set": {"field": "value"},
  "clear": ["field_to_clear"],
  "confidence": 0.0-1.0,
  "reason": "brief internal reason"
}

Allowed set/clear fields:
name, phone, email, address, city, service, property_type, stories, room_size_sqft, rooms,
walls_only, ceiling, trim, occupied, repairs_needed, photos_available, cabinet_count,
project_scope, timeline, urgency, preferred_callback_time, notes, intent, handoff_required.

Common service labels:
- interior painting
- exterior painting
- cabinet painting
- interior touch-up
- exterior touch-up
- paint repair / repainting
- drywall repair + painting
- exterior door painting + interior touch-up
- interior touch-up + paint repair
- painting project

""" + "\n\n" + FAQ_SHEET_TEXT


def _lead_json(lead: LeadInfo) -> dict[str, Any]:
    return lead.model_dump()


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    # Try direct JSON first, then object substring.
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}


def _boolish(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in {"true", "yes", "y", "needed", "repair needed"}:
            return True
        if low in {"false", "no", "n", "none", "not needed"}:
            return False
    return None


def _normalize_patch(data: dict[str, Any]) -> dict[str, Any]:
    set_values = data.get("set") or {}
    clear_values = data.get("clear") or []
    if not isinstance(set_values, dict):
        set_values = {}
    if not isinstance(clear_values, list):
        clear_values = []

    clean_set: dict[str, Any] = {}
    notes: list[str] = []
    for k, v in set_values.items():
        if k not in PATCHABLE_FIELDS or v in (None, "", []):
            continue
        if k in BOOL_FIELDS:
            bv = _boolish(v)
            if bv is not None:
                clean_set[k] = bv
            elif isinstance(v, str) and k == "repairs_needed":
                clean_set[k] = True
                notes.append(f"Repair details: {v[:200]}")
            continue
        if k in INT_FIELDS:
            if isinstance(v, int):
                clean_set[k] = v
            elif isinstance(v, str):
                m = re.search(r"\d+", v)
                if m:
                    clean_set[k] = int(m.group(0))
            continue
        if k == "notes":
            if isinstance(v, list):
                notes.extend(str(x)[:250] for x in v if x)
            elif isinstance(v, str):
                notes.append(v[:250])
            continue
        if isinstance(v, str):
            clean_set[k] = v.strip()
        elif isinstance(v, list):
            clean_set[k] = ", ".join(str(x).strip() for x in v if x)
        elif isinstance(v, dict):
            clean_set[k] = json.dumps(v, ensure_ascii=False)[:500]
        else:
            clean_set[k] = str(v).strip()
    if notes:
        clean_set["notes"] = notes

    clean_clear = [x for x in clear_values if isinstance(x, str) and x in PATCHABLE_FIELDS and x not in CONTACT_FIELDS]
    return {
        "set": clean_set,
        "clear": clean_clear,
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "reason": str(data.get("reason", ""))[:500],
        "source": "llm_first",
    }


def _fallback_question(lead: LeadInfo) -> str:
    if lead.handoff_required and not lead.phone:
        return "Of course — I can have someone follow up directly. What’s the best phone number for a callback?"
    if not lead.service or lead.service in {"painting project", "paint help", "painting"}:
        return "Sure — I can help with that. Is this for interior, exterior, cabinets, or touch-up painting?"
    if not lead.city:
        return "What city is the project in?"
    if not lead.name:
        return "May I get your name?"
    if not lead.phone:
        return "What’s the best phone number for a callback?"
    if lead.photos_available is None:
        return "Do you have any photos you can share?"
    return "Thanks — I have the main details and will pass them along for follow-up."



def _unsafe_price_schedule_signal(text: str) -> bool:
    t = text.lower()
    whole_home = any(p in t for p in ["whole house", "entire house", "whole home", "entire home", "all of my house"])
    fast = any(p in t for p in ["tonight", "today", "same day", "tomorrow"])
    cheap = bool(re.search(r"\b(?:under|less than|below|for)\s*\$?\s*(?:\d{2,4})\b", t)) or "$200" in t or "$500" in t
    return whole_home and (fast or cheap) or (fast and cheap)


def _next_priority_question(lead_after: LeadInfo, *, prefer_city: bool = True) -> str:
    if lead_after.handoff_required and not lead_after.phone:
        return "What’s the best phone number for a callback?"
    if not lead_after.service or lead_after.service in {"painting project", "paint help", "painting"}:
        return "Is this for interior, exterior, cabinets, or touch-up painting?"
    if prefer_city and not lead_after.city:
        return "What city is the project in?"
    if not lead_after.name:
        return "May I get your name?"
    if not lead_after.phone:
        return "What’s the best phone number for a callback?"
    if lead_after.photos_available is None:
        return "Do you have any photos you can share?"
    return "Thanks — I have the main details and will pass them along for follow-up."

def _sanitize_reply(reply: str, customer_message: str, lead_after: LeadInfo) -> str:
    reply = re.sub(r"\s+", " ", (reply or "").strip())
    text = customer_message.lower()

    price_signal = any(p in text for p in [
        "how much", "price", "cost", "quote", "estimate", "under $", "less than", "budget", "cheaper", "expensive"
    ]) or bool(re.search(r"\bunder\s*\$?\s*\d", text))
    schedule_signal = any(p in text for p in [
        "can someone come", "can you come", "appointment", "tomorrow at", "saturday at", "schedule", "tonight", "today"
    ])

    # Highest priority: impossible/unsafe price + availability combos.
    if _unsafe_price_schedule_signal(customer_message):
        lead_after.intent = "unsafe_price_schedule_request"
        if not lead_after.timeline and any(p in text for p in ["tonight", "today", "tomorrow"]):
            lead_after.timeline = "tonight" if "tonight" in text else ("today" if "today" in text else "tomorrow")
        lead_after.urgency = "urgent"
        return (
            "I can’t promise same-day availability or pricing from here, especially for a whole-house project. "
            "I can collect the details and have someone follow up with a reliable estimate. "
            + _next_priority_question(lead_after)
        )

    if not reply:
        reply = _fallback_question(lead_after)

    # No exact price promises or price ranges. Also override if the LLM ignores a price/budget question.
    if price_signal:
        if lead_after.intent not in {"unsafe_price_schedule_request", "incomplete_phone"}:
            lead_after.intent = "price_question"
        next_q = _next_priority_question(lead_after)
        if re.search(r"\$\s?\d", reply) or not any(p in reply.lower() for p in ["pricing depends", "don’t want to guess", "don't want to guess", "reliable estimate"]):
            reply = (
                "I don’t want to guess on price from just that. Pricing depends on the project details, "
                "but I can collect the basics for a reliable follow-up. " + next_q
            )

    # No guaranteed availability.
    if schedule_signal and re.search(r"\b(yes|sure|we can|we'll|we will)\b.*\b(come|be there|send someone|schedule)\b", reply, flags=re.I):
        reply = "I can note that as your preferred time, but someone will need to confirm availability. " + _next_priority_question(lead_after)

    # Avoid multi-question bundled replies; use deterministic next question.
    if reply.count("?") > 1:
        prefix = reply.split("?", 1)[0]
        # Preserve safety phrasing, but replace bundled details with one question.
        if price_signal and "pricing" in prefix.lower():
            reply = prefix + ". " + _next_priority_question(lead_after)
        elif schedule_signal and "confirm" in reply.lower():
            reply = "I can note that as your preferred time, but someone will need to confirm availability. " + _next_priority_question(lead_after)
        else:
            reply = re.sub(r"\?+.*$", "?", reply, count=1)

    # Catch a single question containing two asks, e.g. "city and square footage?".
    question_part = reply.split("?")[0].lower() if "?" in reply else ""
    if " and " in question_part and any(a in question_part for a in ["city", "where"]) and any(b in question_part for b in ["rooms", "square", "size", "stories"]):
        lead_after.intent = lead_after.intent or ("price_question" if price_signal else lead_after.intent)
        if price_signal:
            reply = "I don’t want to guess on price from just that. Pricing depends on the project details, but I can collect the basics for a reliable follow-up. " + _next_priority_question(lead_after)
        else:
            reply = _next_priority_question(lead_after)

    # Keep spoken reply short enough.
    words = reply.split()
    if len(words) > 55:
        reply = " ".join(words[:55]).rstrip(" ,;:") + "."
    return reply


def run_llm_receptionist(message: str, lead: LeadInfo, transcript: list[dict[str, str]], *, timeout_label: str = "fast") -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return (reply, patch, debug). Raises on model/config failure."""
    client = _client()
    if client is None:
        raise RuntimeError("LLM client not configured")
    model = os.getenv("OPENAI_FAST_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("QWEN_MODEL") or "gpt-4.1-mini"
    temperature = float(os.getenv("AI_RECEPTIONIST_TEMPERATURE", "0.2"))
    max_tokens = int(os.getenv("AI_RECEPTIONIST_MAX_TOKENS", "450"))

    recent = transcript[-10:]
    lead_snapshot = _lead_json(lead)
    user_payload = {
        "current_lead": lead_snapshot,
        "recent_transcript": recent,
        "customer_message": message,
        "instruction": "Return ONLY JSON with reply, set, clear, confidence, reason.",
    }
    cache_key = make_cache_key(
        model=model,
        message=message,
        lead_snapshot=lead_snapshot,
        recent_transcript=recent,
        prompt_version=PROMPT_VERSION,
    )
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    start = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    raw = response.choices[0].message.content or "{}"
    data = _parse_json(raw)
    patch = _normalize_patch(data)
    reply = str(data.get("reply") or "").strip()
    debug = {
        "source": "llm_first",
        "model": model,
        "latency_ms": latency_ms,
        "confidence": patch.get("confidence"),
        "reason": patch.get("reason"),
        "raw": raw[:2000],
        "mode": timeout_label,
        "cache_hit": False,
    }
    result = (reply, patch, debug)
    cache_set(cache_key, result)
    return result


def sanitize_llm_reply(reply: str, customer_message: str, lead_after: LeadInfo) -> str:
    return _sanitize_reply(reply, customer_message, lead_after)
