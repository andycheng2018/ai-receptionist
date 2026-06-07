"""LLM-first receptionist layer.

A fast LLM produces:
1. A short customer-facing reply.
2. A structured lead patch.


Design:
- Let the LLM understand natural customer language.
- Keep deterministic code for safety, schema validation, pricing guardrails,
  availability guardrails, and one-question-at-a-time behavior.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
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
from app.company_cache import get_fast_company_answer, find_city_in_message, find_service_in_message
from app.company_config import COMPANY_CONFIG, service_area_display
from app.database import save_final_call_json, save_lead_to_db
from app.faq_sheet import FAQ_SHEET_TEXT, deterministic_faq_answer, is_standalone_faq
from app.ai_response_cache import make_cache_key, get as cache_get, set as cache_set, stats as ai_cache_stats

load_dotenv()

logger = logging.getLogger(__name__)


def _client():
    """Create an OpenAI-compatible client.

    OPENAI_BASE_URL is optional. You only need it for local/OpenAI-compatible
    models. For normal OpenAI usage, OPENAI_API_KEY is enough.
    """
    if OpenAI is None:
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")

    if not api_key:
        return None

    kwargs: dict[str, Any] = {"api_key": api_key}

    if base_url:
        kwargs["base_url"] = base_url

    return OpenAI(**kwargs)


def llm_first_available() -> bool:
    """Whether the LLM-first receptionist can run."""
    return _client() is not None


def llm_available() -> bool:
    return llm_first_available()

# Whitelist of fields the LLM is allowed to update
PATCHABLE_FIELDS = {
    "intent",
    "name",
    "phone",
    "email",
    "address",
    "city",
    "service",
    "property_type",
    "stories",
    "room_size_sqft",
    "rooms",
    "walls_only",
    "ceiling",
    "trim",
    "occupied",
    "repairs_needed",
    "cabinet_count",
    "project_scope",
    "timeline",
    "urgency",
    "preferred_callback_time",
    "handoff_required",
    "notes",
}

# Tells the sanitizer how to treat certain fields
BOOL_FIELDS = {
    "walls_only",
    "ceiling",
    "trim",
    "occupied",
    "repairs_needed",
    "handoff_required",
}

INT_FIELDS = {
    "stories",
    "room_size_sqft",
    "rooms",
    "cabinet_count",
}

CONTACT_FIELDS = {
    "name",
    "phone",
    "email",
}


PROMPT_VERSION = "V24_CITY_PHONE_BUSINESS_GUARDRAILS"
# Tells the LLM exactly how to behave from prompt instructions
SYSTEM_PROMPT = """
You are the live AI receptionist for a painting contractor.

Your job on every turn:
1. Understand the customer's message using context.
2. Return a short, warm, spoken reply with personality.
3. Return a structured patch to update the lead.

Critical rules:
- Reply in 1-2 short sentences, under 30 words when possible.
- Ask exactly ONE next question, unless the customer is only asking a FAQ or the customer is clearly done.
- Never give a dollar estimate or price range. Say pricing depends on details and collect info.
- Never promise exact availability or appointments. You can note preferred times and say someone will confirm.
- Do not infer a name from emotions or descriptions. "I'm stressed" is not a name.
- Project city beats caller city. Example: "I live in San Jose but property is in San Mateo" means city = "San Mateo".
- Only accept project cities in the company service area. If the project city is outside the service area, do not set city; politely say the company only serves the listed areas and ask for the project city again.
- If the customer corrects themselves, the latest correction wins.
- If the customer says "actually", "forget that", "ignore that", "I meant", "not outside", "not bedrooms", or similar, set is_correction to true.
- For corrections, clear stale conflicting fields.
- For vague requests like "help with paint", ask whether it is interior, exterior, cabinets, or touch-up.
- Keep the tone warm and practical, not overly chatty.

Clean phone-call flow:
- Collect enough to make a useful lead: service/scope, project city, timeline/urgency, name, and a valid phone/contact.
- If the phone call already has caller ID, do not ask for a phone number again.
- Once the main details are collected, do not keep asking estimator questions. Ask: "Is there anything else you’d like me to note for the painter?"
- Do not ask for photos. Photos are optional and should not block completion.
- If the customer says no/nope/that's all/thanks after that final check, end politely with no additional questions.
- Do not end the call before the customer has had a chance to add or correct details.

Lead patch format:
{
  "reply": "short customer-facing reply",
  "set": {"field": "value"},
  "clear": ["field_to_clear"],
  "is_correction": false,
  "confidence": 0.0,
  "reason": "brief internal reason"
}

Allowed set/clear fields:
name, phone, email, address, city, service, property_type, stories, room_size_sqft, rooms,
walls_only, ceiling, trim, occupied, repairs_needed, cabinet_count,
project_scope, timeline, urgency, preferred_callback_time, notes, intent, handoff_required.

Common service labels:
- interior painting
- exterior painting
- cabinet painting
- interior touch-up
- exterior touch-up
- paint repair / repainting
- drywall repair + painting
- drywall repair + painting + cabinet painting
- exterior door painting + interior touch-up
- interior touch-up + paint repair
- interior touch-up + paint repair + door painting
- painting project

Patch rules:
- "set" contains fields to create or overwrite.
- "clear" contains stale fields to erase.
- Never clear name, phone, or email unless the customer directly corrects that field.
- repairs_needed must be true or false only, never descriptive text.
- Put repair descriptions in project_scope or notes.
- walls_only, ceiling, trim, occupied, and handoff_required must be true or false only.
- stories, rooms, room_size_sqft, and cabinet_count must be integers.
- project_scope should contain concrete areas/items, not vague summaries.
- If customer says "I live in X but the project/property/rental is in Y", set city to Y only if Y is in the company service area.
- If customer asks for a human, manager, representative, or real person, set handoff_required to true.
- If customer gives a phone number, only set phone if it has at least 10 digits or is a valid international-style number.
- If a phone number is invalid, ask: "Sorry, could you repeat your phone number? I need a valid callback number."
- If customer asks for a time outside business hours, do not accept it as the timeline or callback time. Apologize and state the business hours.
- Business hours: Monday-Friday 8 AM - 6 PM, Saturday 9 AM - 2 PM, Sunday closed.
- If customer gives callback timing, set preferred_callback_time.
- If customer gives project timing, set timeline and urgency.
- urgency should be "urgent", "soon", or "flexible" when clear. Do not label a future date as urgent unless the customer explicitly says urgent/ASAP/today/tomorrow.
- If the message is only a FAQ with no lead details, return an empty set/clear patch with low confidence.
- Return ONLY valid JSON. No markdown.

Examples:

Customer:
"I live in San Jose, but the rental that needs painting is in San Mateo."
JSON:
{
  "reply": "Got it — the project is for a rental in San Mateo. What kind of painting do you need: interior, exterior, cabinets, or touch-up?",
  "set": {
    "city": "San Mateo",
    "property_type": "rental"
  },
  "clear": [],
  "is_correction": false,
  "confidence": 0.95,
  "reason": "Customer gave project city and rental property type."
}

Customer:
"Actually, not outside. It is inside, two bedrooms in Daly City."
JSON:
{
  "reply": "Got it — interior painting for two bedrooms in Daly City. When are you hoping to get this completed?",
  "set": {
    "service": "interior painting",
    "city": "Daly City",
    "rooms": 2,
    "project_scope": "two bedrooms"
  },
  "clear": ["stories", "room_size_sqft", "walls_only", "ceiling", "trim"],
  "is_correction": true,
  "confidence": 0.95,
  "reason": "Customer corrected an exterior project to interior bedrooms."
}

Customer:
"Not sure if this is painting or repair, but there is bubbling under one window and the kitchen cabinets look dated."
JSON:
{
  "reply": "Got it — I’ll note possible repair, painting, and cabinet work. What city is the project in?",
  "set": {
    "service": "drywall repair + painting + cabinet painting",
    "repairs_needed": true,
    "project_scope": "bubbling under one window and kitchen cabinets"
  },
  "clear": [],
  "is_correction": false,
  "confidence": 0.9,
  "reason": "Customer described repair-related paint issue and cabinet painting."
}

Customer:
"Front door outside, plus hallway touch-ups inside."
JSON:
{
  "reply": "Got it — I’ll capture both the front door and hallway touch-ups. What city is the project in?",
  "set": {
    "service": "exterior door painting + interior touch-up",
    "project_scope": "front door and hallway touch-ups"
  },
  "clear": [],
  "is_correction": false,
  "confidence": 0.9,
  "reason": "Customer described mixed exterior door and interior touch-up work."
}
""" + "\n\nCompany FAQ and policy facts:\n" + FAQ_SHEET_TEXT


def _lead_json(lead: LeadInfo) -> dict[str, Any]:
    return lead.model_dump()


# Tries to parse the LLM response into JSON
def _parse_json(text: str) -> dict[str, Any]:
    """Parse JSON from the model.

    The model is instructed to return JSON only, but this tolerates accidental
    markdown fences or extra text.
    """
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}

        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

# Converts messy values into boolean
def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        low = value.strip().lower()

        if low in {"true", "yes", "y", "1", "needed", "repair needed", "needs repair"}:
            return True

        if low in {"false", "no", "n", "0", "none", "not needed", "no repair"}:
            return False

        # Safety net: if the LLM puts repair text into repairs_needed, treat it
        # as true and preserve the details in notes.
        if any(word in low for word in ["repair", "damage", "stain", "bubbling", "leak", "water", "patch", "crack", "peeling"]):
            return True

    return None

# Converts values into integers
def _intish(value: Any) -> int | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    if isinstance(value, str):
        stripped = value.strip()

        if stripped.isdigit():
            return int(stripped)

        match = re.search(r"\d+", stripped)
        if match:
            return int(match.group(0))

    return None

# Cleans up JSON and makes it safe
def _normalize_patch(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw model JSON into the patch shape expected by the engine."""
    set_values = data.get("set") or {}
    clear_values = data.get("clear") or []

    if not isinstance(set_values, dict):
        set_values = {}

    if not isinstance(clear_values, list):
        clear_values = []

    clean_set: dict[str, Any] = {}
    notes: list[str] = []

    # Backward-compatible: if model returns flat fields, move them into set.
    for key, value in data.items():
        if key in PATCHABLE_FIELDS and value not in (None, "", []):
            set_values.setdefault(key, value)

    for key, value in set_values.items():
        if key not in PATCHABLE_FIELDS or value in (None, "", []):
            continue

        if key in BOOL_FIELDS:
            parsed = _boolish(value)

            if parsed is not None:
                clean_set[key] = parsed

                if key == "repairs_needed" and isinstance(value, str) and len(value.strip()) > 8:
                    notes.append(f"Repair details: {value.strip()[:220]}")

            continue

        if key in INT_FIELDS:
            parsed = _intish(value)

            if parsed is not None:
                clean_set[key] = parsed

            continue

        if key == "notes":
            if isinstance(value, list):
                notes.extend(str(item).strip()[:250] for item in value if str(item).strip())
            elif isinstance(value, str):
                notes.append(value.strip()[:250])
            continue

        if isinstance(value, str):
            clean_set[key] = value.strip()[:500]
        elif isinstance(value, list):
            clean_set[key] = ", ".join(str(item).strip() for item in value if item)[:500]
        elif isinstance(value, dict):
            clean_set[key] = json.dumps(value, ensure_ascii=False)[:500]
        else:
            clean_set[key] = str(value).strip()[:500]

    if notes:
        clean_set["notes"] = notes

    clean_clear: list[str] = []

    for field in clear_values:
        if not isinstance(field, str):
            continue

        field = field.strip()

        if field in PATCHABLE_FIELDS and field not in CONTACT_FIELDS:
            clean_clear.append(field)

    # Remove duplicates while preserving order.
    clean_clear = list(dict.fromkeys(clean_clear))

    # Do not clear a field that the same patch is setting.
    clean_clear = [field for field in clean_clear if field not in clean_set]

    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    confidence = max(0.0, min(confidence, 1.0))

    return {
        "set": clean_set,
        "clear": clean_clear,
        "is_correction": bool(data.get("is_correction", False)),
        "confidence": confidence,
        "reason": str(data.get("reason", ""))[:500],
        "source": "llm_first",
    }

# Checks whether the receptionist has collected enough info
def _lead_has_enough_for_final_check(lead: LeadInfo) -> bool:
    """Enough information to stop collecting and do a final check.

    Photos are intentionally not required. They can be useful for a painter,
    but they should not block a clean receptionist flow.
    """
    service = (lead.service or "").lower().strip()
    specific_service = bool(
        service
        and service not in {"painting project", "paint help", "painting", "paint"}
    )

    return bool(
        specific_service
        and lead.city
        and lead.name
        and lead.phone
        and lead.timeline
    )


# Chooses the next question if LLM gives no reply or something goes wrong
def _fallback_question(lead: LeadInfo) -> str:
    if lead.handoff_required and not lead.phone:
        return "Of course — I can have someone follow up directly. What’s the best phone number for a callback?"

    if not lead.service or lead.service in {"painting project", "paint help", "painting"}:
        return "Sure — I can help with that. Is this for interior, exterior, cabinets, or touch-up painting?"

    if not lead.city:
        return "What city is the project in?"

    if not lead.timeline:
        return "When are you hoping to get this completed?"

    if not lead.name:
        return "May I get your name?"

    if not lead.phone:
        return "What’s the best phone number for a callback?"

    return "Thanks — I have the main details. Is there anything else you’d like me to note for the painter?"

# Detects risky requests and avoids promising pricing or availability
def _unsafe_price_schedule_signal(text: str) -> bool:
    t = text.lower()

    whole_home = any(
        phrase in t
        for phrase in [
            "whole house",
            "entire house",
            "whole home",
            "entire home",
            "all of my house",
            "whole exterior",
            "entire exterior",
        ]
    )

    fast = any(
        phrase in t
        for phrase in [
            "tonight",
            "today",
            "same day",
            "same-day",
            "tomorrow",
        ]
    )

    cheap = (
        bool(re.search(r"\b(?:under|less than|below|for)\s*\$?\s*(?:\d{2,4})\b", t))
        or "$200" in t
        or "$500" in t
    )

    return (whole_home and (fast or cheap)) or (fast and cheap)

# Decides next best single question based on what is missing
def _next_priority_question(lead_after: LeadInfo, *, prefer_city: bool = True) -> str:
    if lead_after.handoff_required and not lead_after.phone:
        return "What’s the best phone number for a callback?"

    if not lead_after.service or lead_after.service in {"painting project", "paint help", "painting"}:
        return "Is this for interior, exterior, cabinets, or touch-up painting?"

    if prefer_city and not lead_after.city:
        return "What city is the project in?"

    if not lead_after.timeline:
        return "When are you hoping to get this completed?"

    if not lead_after.name:
        return "May I get your name?"

    if not lead_after.phone:
        return "What’s the best phone number for a callback?"

    return "Is there anything else you’d like me to note for the painter?"


# Fixes even if the LLM returns something unsafe
def _sanitize_reply(reply: str, customer_message: str, lead_after: LeadInfo) -> str:
    """Safety and style guardrail around the LLM's customer-facing reply."""
    reply = re.sub(r"\s+", " ", (reply or "").strip())
    text = customer_message.lower()

    price_signal = (
        any(
            phrase in text
            for phrase in [
                "how much",
                "price",
                "cost",
                "quote",
                "estimate",
                "under $",
                "less than",
                "budget",
                "cheaper",
                "expensive",
            ]
        )
        or bool(re.search(r"\bunder\s*\$?\s*\d", text))
        or bool(re.search(r"\$\s*\d", text))
    )

    schedule_signal = any(
        phrase in text
        for phrase in [
            "can someone come",
            "can you come",
            "appointment",
            "tomorrow at",
            "saturday at",
            "schedule",
            "tonight",
            "today",
            "same day",
            "same-day",
        ]
    )

    # Highest priority: impossible/unsafe price + availability combos.
    if _unsafe_price_schedule_signal(customer_message):
        lead_after.intent = "unsafe_price_schedule_request"

        if not lead_after.timeline and any(phrase in text for phrase in ["tonight", "today", "tomorrow"]):
            if "tonight" in text:
                lead_after.timeline = "tonight"
            elif "today" in text:
                lead_after.timeline = "today"
            else:
                lead_after.timeline = "tomorrow"

        lead_after.urgency = "urgent"

        return (
            "I can’t promise same-day availability or pricing from here, especially for a larger project. "
            "I can collect the details and have someone follow up with a reliable estimate. "
            + _next_priority_question(lead_after)
        )

    if not reply:
        reply = _fallback_question(lead_after)

    # No exact price promises or price ranges.
    if price_signal:
        if lead_after.intent not in {"unsafe_price_schedule_request", "incomplete_phone"}:
            lead_after.intent = "price_question"

        next_question = _next_priority_question(lead_after)

        safe_pricing_phrases = [
            "pricing depends",
            "don’t want to guess",
            "don't want to guess",
            "reliable estimate",
            "depends on the project",
            "depends on details",
        ]

        if re.search(r"\$\s?\d", reply) or not any(phrase in reply.lower() for phrase in safe_pricing_phrases):
            reply = (
                "I don’t want to guess on price from just that. Pricing depends on the project details, "
                "but I can collect the basics for a reliable follow-up. "
                + next_question
            )

    # No guaranteed availability.
    if schedule_signal and re.search(
        r"\b(yes|sure|we can|we'll|we will)\b.*\b(come|be there|send someone|schedule)\b",
        reply,
        flags=re.I,
    ):
        reply = (
            "I can note that as your preferred time, but someone will need to confirm availability. "
            + _next_priority_question(lead_after)
        )

    # Avoid multi-question bundled replies.
    if reply.count("?") > 1:
        if price_signal:
            reply = (
                "I don’t want to guess on price from just that. Pricing depends on the project details, "
                "but I can collect the basics for a reliable follow-up. "
                + _next_priority_question(lead_after)
            )
        elif schedule_signal:
            reply = (
                "I can note that as your preferred time, but someone will need to confirm availability. "
                + _next_priority_question(lead_after)
            )
        else:
            reply = re.sub(r"\?+.*$", "?", reply, count=1)

    # Catch one question containing multiple asks, e.g. "city and square footage?"
    question_part = reply.split("?")[0].lower() if "?" in reply else ""

    if (
        " and " in question_part
        and any(word in question_part for word in ["city", "where"])
        and any(word in question_part for word in ["rooms", "square", "size", "stories"])
    ):
        reply = _next_priority_question(lead_after)

    # When lead is basically complete, final check instead of more estimator questions.
    if _lead_has_enough_for_final_check(lead_after):
        lower_reply = reply.lower()
        final_check_present = any(
            phrase in lower_reply
            for phrase in [
                "anything else",
                "anything you",
                "else you",
                "note for the painter",
                "add for the painter",
            ]
        )

        if not final_check_present:
            if "?" in reply:
                reply = re.sub(
                    r"[^.?!]*\?$",
                    "Is there anything else you’d like me to note for the painter?",
                    reply,
                ).strip()
            else:
                reply = (
                    reply.rstrip(" .")
                    + ". Is there anything else you’d like me to note for the painter?"
                )

    # Keep spoken reply short.
    words = reply.split()

    if len(words) > 55:
        reply = " ".join(words[:55]).rstrip(" ,;:") + "."

    # Clean common punctuation artifacts from combining LLM reply + guardrails.
    reply = re.sub(r"\s+", " ", reply).strip()
    reply = reply.replace("!.", "!").replace("?.", "?").replace("..", ".")
    reply = re.sub(r"\.\s*\.", ".", reply)

    return reply


def run_llm_receptionist(
    message: str,
    lead: LeadInfo,
    transcript: list[dict[str, str]],
    *,
    timeout_label: str = "fast",
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return (reply, patch, debug).

    Raises if model/config fails, so the caller can use a fallback path.
    """
    client = _client()

    if client is None:
        raise RuntimeError("LLM client not configured")

    model = (
        os.getenv("OPENAI_FAST_MODEL")
        or os.getenv("OPENAI_MODEL")
        or os.getenv("QWEN_MODEL")
        or "gpt-4.1-mini"
    )

    temperature = float(os.getenv("AI_RECEPTIONIST_TEMPERATURE", "0.2"))
    max_tokens = int(os.getenv("AI_RECEPTIONIST_MAX_TOKENS", "450"))

    recent = transcript[-10:]
    lead_snapshot = _lead_json(lead)

    user_payload = {
        "current_lead": lead_snapshot,
        "recent_transcript": recent,
        "customer_message": message,
        "instruction": "Return ONLY JSON with reply, set, clear, is_correction, confidence, reason.",
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
        reply, patch, debug = cached
        debug = dict(debug or {})
        debug["cache_hit"] = True
        return reply, patch, debug

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
    """Public reply sanitizer used by the main receptionist engine."""
    return _sanitize_reply(reply, customer_message, lead_after)


def _empty_patch(
    *,
    source: str,
    reason: str,
    llm_configured: bool,
    llm_deferred: bool = False,
    llm_error: str | None = None,
    reasoner_mode: str | None = None,
) -> dict[str, Any]:
    """Safe no-op patch for compatibility paths."""
    patch: dict[str, Any] = {
        "set": {},
        "clear": [],
        "is_correction": False,
        "confidence": 0.0,
        "reason": reason,
        "source": source,
        "llm_configured": llm_configured,
        "llm_deferred": llm_deferred,
    }

    if llm_error:
        patch["llm_error"] = llm_error

    if reasoner_mode:
        patch["reasoner_mode"] = reasoner_mode

    return patch


def extract_lead_patch(
    message: str,
    current_lead: LeadInfo,
    *,
    use_llm: bool = True,
    reasoner_mode: str | None = None,
) -> dict[str, Any]:
    """Backward-compatible patch-only function.

    Old code used app.llm_extractor.extract_lead_patch().
    Now this delegates to llm_receptionist and returns only the patch.

    For the normal AI-first path, prefer run_llm_receptionist(), because it
    returns both the customer reply and the lead patch together.
    """
    if not use_llm:
        return _empty_patch(
            source="llm_deferred",
            reason="LLM patch extraction skipped because use_llm=False.",
            llm_configured=llm_first_available(),
            llm_deferred=True,
            reasoner_mode=reasoner_mode or "patch_only",
        )

    if not llm_first_available():
        return _empty_patch(
            source="unavailable",
            reason="LLM patch extraction skipped because no LLM client is configured.",
            llm_configured=False,
            llm_deferred=False,
            reasoner_mode=reasoner_mode,
        )

    try:
        transcript = [
            {
                "speaker": "customer",
                "text": message.strip(),
                "timestamp": "",
            }
        ]

        _reply, patch, debug = run_llm_receptionist(
            message,
            current_lead,
            transcript,
            timeout_label=reasoner_mode or "patch_only",
        )

        patch["llm_configured"] = True
        patch["llm_deferred"] = False
        patch["reasoner_mode"] = reasoner_mode or "patch_only"
        patch["latency_ms"] = debug.get("latency_ms", 0)

        return patch

    except Exception as exc:
        return _empty_patch(
            source="llm_error",
            reason="LLM patch extraction failed; returned safe no-op patch.",
            llm_configured=True,
            llm_deferred=False,
            llm_error=repr(exc),
            reasoner_mode=reasoner_mode,
        )

# =============================================================================
# Conversation orchestration
# =============================================================================
# This section replaces the old app/receptionist.py rule-heavy engine.
# The LLM above owns natural language understanding and reply drafting.
# This section owns session state, cache/FAQ fast-paths, scoring, saving,
# transcript management, and the public handle_message() API.

SESSION_MEMORY: dict[str, LeadInfo] = {}
SESSION_TRANSCRIPTS: dict[str, list[dict[str, str]]] = {}

SERVICE_CITY_DISPLAY = {
    city.lower().strip(): city
    for city in COMPANY_CONFIG["service_areas"]
}
SERVICE_CITIES = set(SERVICE_CITY_DISPLAY.keys())
SUPPORTED_CITY_DISPLAY = service_area_display()


def _clean_city_text(city: str | None) -> str:
    """Normalize city text for service-area comparison."""
    return re.sub(r"\s+", " ", (city or "").strip().lower().strip(".,!?;:"))


def canonical_supported_city(city: str | None) -> str | None:
    """Return official city display name if supported, otherwise None."""
    cleaned = _clean_city_text(city)
    if not cleaned:
        return None
    return SERVICE_CITY_DISPLAY.get(cleaned)


def unsupported_city_reply(city: str | None = None) -> str:
    city_text = f" in {city.strip()}" if isinstance(city, str) and city.strip() else " there"
    return (
        f"Sorry, we don’t currently service projects{city_text}. "
        f"We serve {SUPPORTED_CITY_DISPLAY}. What city is the project in?"
    )


def get_lead(session_id: str) -> LeadInfo:
    if session_id not in SESSION_MEMORY:
        SESSION_MEMORY[session_id] = LeadInfo()
    return SESSION_MEMORY[session_id]


def reset_session(session_id: str) -> None:
    SESSION_MEMORY.pop(session_id, None)
    SESSION_TRANSCRIPTS.pop(session_id, None)


def add_transcript_message(session_id: str, speaker: str, text: str) -> None:
    SESSION_TRANSCRIPTS.setdefault(session_id, []).append(
        {
            "speaker": speaker,
            "text": text,
            "timestamp": datetime.utcnow().isoformat(),
        }
    )


def get_transcript(session_id: str) -> list[dict[str, str]]:
    return SESSION_TRANSCRIPTS.get(session_id, [])


def last_ai_reply(session_id: str) -> str | None:
    for item in reversed(SESSION_TRANSCRIPTS.get(session_id, [])):
        if item.get("speaker") == "ai":
            return item.get("text")
    return None


def normalize_text(message: str) -> str:
    return re.sub(r"\s+", " ", (message or "").strip().lower())


def normalize_phone(value: str | None) -> str | None:
    """Normalize a customer phone number.

    Accepts common US phone formats like 650-333-3333 or (650) 333-3333,
    plus international-style numbers that include a leading +.
    Returns E.164-ish text such as +16503333333, or None if invalid.
    """
    raw = (value or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    if lowered in {"unknown", "none", "n/a", "na", "no", "nope"}:
        return None

    digits = re.sub(r"\D", "", raw)

    if len(digits) == 10:
        return "+1" + digits

    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits

    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits

    return None




def phone_request_context(previous_ai_reply: str | None) -> bool:
    """Whether the receptionist most recently asked for a phone/callback number."""
    t = normalize_text(previous_ai_reply or "")
    return any(
        phrase in t
        for phrase in [
            "phone number", "callback number", "call back number", "best number",
            "best phone", "number for a callback", "reach you", "contact number",
        ]
    )


def invalid_phone_retry_needed(message: str, previous_ai_reply: str | None, phone_before: str | None, lead_after: LeadInfo) -> bool:
    """Return True when the customer appears to have given an invalid phone number.

    This is deterministic because the LLM may accidentally move on even when the
    callback number is too short or malformed.
    """
    if lead_after.phone:
        return False

    # If we already had a phone before this turn, do not treat unrelated numbers
    # like room size or dates as bad phone numbers.
    if phone_before:
        return False

    digits = re.sub(r"\D", "", message or "")
    if not digits:
        return False

    # Only treat short numbers as phone attempts when the previous AI message
    # specifically asked for a phone number. Otherwise, numbers like "2 rooms"
    # or "100 sq ft" should not trigger phone retry logic.
    if phone_request_context(previous_ai_reply):
        return normalize_phone(message) is None

    # If the customer gives 7+ digits unprompted, it is probably meant as a
    # phone number and should be validated.
    return len(digits) >= 7 and normalize_phone(message) is None


def business_hours_display() -> str:
    """Human-readable business hours from company config with safe defaults."""
    hours = COMPANY_CONFIG.get("business_hours", {}) if isinstance(COMPANY_CONFIG, dict) else {}
    weekday = hours.get("monday_friday", "8 AM - 6 PM")
    saturday = hours.get("saturday", "9 AM - 2 PM")
    sunday = hours.get("sunday", "Closed")
    return f"Monday-Friday {weekday}, Saturday {saturday}, and Sunday {sunday}"


def _extract_requested_time_hours(text: str) -> float | None:
    """Extract a requested hour from text, returning 24-hour decimal time.

    Handles phrases such as 7pm, 7:30 pm, 18:30, and 6 pm. Ambiguous bare
    numbers like "7" are ignored so dates/room counts do not get misread.
    """
    t = text.lower()
    match = re.search(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)\b", t)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        suffix = match.group(3)
        if suffix == "pm" and hour != 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        return hour + minute / 60

    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if match:
        return int(match.group(1)) + int(match.group(2)) / 60

    return None


def _mentioned_day_type(text: str) -> str | None:
    """Return weekday/saturday/sunday if the customer named a day."""
    t = normalize_text(text)
    if re.search(r"\b(sun|sunday)\b", t):
        return "sunday"
    if re.search(r"\b(sat|saturday)\b", t):
        return "saturday"
    if re.search(r"\b(mon|monday|tue|tues|tuesday|wed|wednesday|thu|thur|thurs|thursday|fri|friday)\b", t):
        return "weekday"
    return None


def business_hours_violation(message: str) -> str | None:
    """Return a reason if the requested timing is outside business hours.

    This intentionally stays simple. It catches common conversational cases like
    "Sunday", "next Tuesday at 7pm", "Saturday at 3pm", or "today at 7pm".
    """
    text = message or ""
    t = normalize_text(text)
    day_type = _mentioned_day_type(t)
    requested_hour = _extract_requested_time_hours(t)

    timing_signal = any(
        phrase in t
        for phrase in [
            "today", "tomorrow", "tonight", "this morning", "this afternoon", "this evening",
            "next", "appointment", "schedule", "come", "callback", "call back",
            "done", "completed", "finish", "available", "availability", "at ", "am", "pm",
        ]
    )

    if day_type == "sunday" and timing_signal:
        return "Sunday is outside business hours."

    if requested_hour is None:
        return None

    if day_type == "saturday":
        if not (9 <= requested_hour < 14):
            return "Saturday request is outside business hours."
        return None

    # If no day is mentioned, assume normal weekday hours for the time check.
    if not (8 <= requested_hour < 18):
        return "Requested time is outside business hours."

    return None


def business_hours_retry_reply() -> str:
    return f"Sorry, we're only available {business_hours_display()}. What time during business hours works best?"

def patch_unsupported_city(patch: dict[str, Any] | None) -> str | None:
    """Return unsupported city attempted by the LLM patch, if any."""
    if not isinstance(patch, dict):
        return None
    set_values = patch.get("set") or {}
    if not isinstance(set_values, dict):
        return None
    city = set_values.get("city")
    if not city:
        return None
    city_text = str(city).strip()
    if canonical_supported_city(city_text):
        return None
    return city_text


def customer_done_signal(message: str) -> bool:
    """True when the customer is clearly done after a final check."""
    t = normalize_text(message)
    if not t:
        return False

    exact = {
        "no", "nope", "nah", "no photos", "no photo", "that's all", "thats all", "that is all",
        "nothing else", "no that's all", "no thats all", "all good",
        "i'm good", "im good", "we're good", "were good", "thank you",
        "thanks", "bye", "goodbye", "done", "all set",
    }
    if t in exact:
        return True

    return any(
        phrase in t
        for phrase in [
            "nothing else", "that's all", "thats all", "all set",
            "i'm all set", "im all set", "you can send it",
            "you can pass it along", "that should be it",
            "no other details", "no more details", "no photos", "no photo",
        ]
    )


def final_check_context(previous_ai_reply: str | None) -> bool:
    """Whether the last AI reply was the final anything-else check."""
    t = normalize_text(previous_ai_reply or "")
    return any(
        phrase in t
        for phrase in [
            "anything else", "anything you'd like", "anything you would like",
            "else you'd like", "else you would like", "note for the painter",
            "add for the painter",
        ]
    )


def closing_reply(lead: LeadInfo) -> str:
    """A clean ending with no extra questions."""
    name = f", {lead.name}" if lead.name else ""
    service = lead.service or "painting project"
    city = f" in {lead.city}" if lead.city else ""
    timeline = f" for {lead.timeline}" if lead.timeline else ""
    return f"Thanks{name}. I have your {service}{city}{timeline}. The painter will follow up with you."


def is_probable_assistant_echo(message: str, previous_ai_reply: str | None) -> bool:
    """Ignore accidental mic/browser echo of the receptionist's own reply."""
    if not previous_ai_reply:
        return False

    msg = normalize_text(message)
    prev = normalize_text(previous_ai_reply)
    if not msg or not prev:
        return False

    if msg == prev:
        return True

    msg_words = set(re.findall(r"[a-z0-9']+", msg))
    prev_words = set(re.findall(r"[a-z0-9']+", prev))
    if not msg_words or not prev_words:
        return False

    overlap = len(msg_words & prev_words) / max(1, min(len(msg_words), len(prev_words)))
    return overlap >= 0.75 and len(msg_words) >= 4


def append_note(lead: LeadInfo, note: str) -> None:
    if note and note not in lead.notes:
        lead.notes.append(note)


def looks_like_supported_city(city: str | None) -> bool:
    return canonical_supported_city(city) is not None


def is_generic_or_unknown_service(lead: LeadInfo) -> bool:
    service = (lead.service or "").lower().strip()
    return service in {"", "painting project", "painting", "paint", "paint help"}


def missing_fields(lead: LeadInfo) -> list[str]:
    """Minimum useful lead fields for painter follow-up.

    Keep this intentionally small. The LLM can collect extra details, but the
    receptionist should not trap callers in a long estimator form.
    """
    missing: list[str] = []

    if is_generic_or_unknown_service(lead):
        missing.append("service")
    if not lead.city:
        missing.append("city")

    service = (lead.service or "").lower()
    if "cabinet" in service and not lead.cabinet_count:
        missing.append("cabinet_details")
    if "exterior" in service and not lead.project_scope:
        missing.append("exterior_scope")

    if not lead.timeline:
        missing.append("timeline")
    if not lead.name:
        missing.append("name")
    if not lead.phone:
        missing.append("phone")
    return missing


def next_missing_question(missing: list[str]) -> str:
    if "service" in missing:
        return "Is this for interior, exterior, cabinets, or touch-up painting?"
    if "city" in missing:
        return "What city is the project in?"
    if "cabinet_details" in missing:
        return "About how many cabinet doors and drawers need painting?"
    if "exterior_scope" in missing:
        return "Is this the full exterior or just one area?"
    if "timeline" in missing:
        return "When are you hoping to get this completed?"
    if "name" in missing:
        return "May I get your name?"
    if "phone" in missing:
        return "What is the best phone number for a callback?"
    return ""


def append_next_question_to_answer(answer: str, missing: list[str]) -> str:
    answer = (answer or "").strip()
    follow_up = next_missing_question(missing).strip()

    if not answer:
        return follow_up
    if not follow_up:
        return answer
    if answer.endswith("?"):
        return answer
    if follow_up.lower() in answer.lower():
        return answer
    return f"{answer} {follow_up}"


def apply_lead_patch(lead: LeadInfo, patch: dict[str, Any]) -> LeadInfo:
    """Apply the structured patch returned by the LLM.

    Clearing happens before setting, so corrections can erase stale fields and
    then set the corrected scope. Contact fields are protected.
    """
    if not isinstance(patch, dict):
        return lead

    protected = {"name", "phone", "email"}

    for field in patch.get("clear", []) or []:
        if field in protected or not hasattr(lead, field):
            continue
        current = getattr(lead, field)
        setattr(lead, field, [] if isinstance(current, list) else None)

    set_values = patch.get("set", {}) or {}
    if isinstance(set_values, dict):
        for field, value in set_values.items():
            if not hasattr(lead, field) or value in (None, "", []):
                continue

            if field in BOOL_FIELDS:
                parsed = _boolish(value)
                if parsed is not None:
                    setattr(lead, field, parsed)
                    if field == "repairs_needed" and isinstance(value, str) and len(value.strip()) > 8:
                        append_note(lead, f"Repair details: {value.strip()[:250]}")
                continue

            if field in INT_FIELDS or field == "lead_score":
                parsed = _intish(value)
                if parsed is not None:
                    setattr(lead, field, parsed)
                continue

            if field == "notes":
                if isinstance(value, list):
                    for note in value:
                        append_note(lead, str(note)[:250])
                else:
                    append_note(lead, str(value)[:250])
                continue

            if field == "city":
                canonical_city = canonical_supported_city(str(value))
                if canonical_city:
                    setattr(lead, field, canonical_city)
                else:
                    append_note(lead, f"Unsupported city ignored: {str(value).strip()[:80]}")
                continue

            if field == "phone":
                normalized = normalize_phone(str(value))
                if normalized:
                    setattr(lead, field, normalized)
                else:
                    append_note(lead, f"Invalid phone number ignored: {str(value).strip()[:80]}")
                continue

            setattr(lead, field, str(value).strip()[:500])

    reason = patch.get("reason")
    source = patch.get("source")
    if reason and patch.get("is_correction"):
        append_note(lead, f"Correction handled by {source or 'LLM'}: {reason}")

    return lead


def sanitize_lead_schema(lead: LeadInfo) -> LeadInfo:
    """Keep LeadInfo safe after LLM output or cache-derived updates."""
    string_fields = {
        "name", "phone", "email", "address", "city", "service", "property_type",
        "project_scope", "timeline", "urgency", "preferred_callback_time",
        "lead_priority", "intent",
    }
    int_fields = {"room_size_sqft", "rooms", "stories", "cabinet_count", "lead_score"}
    bool_fields = BOOL_FIELDS | {"saved"}

    for field in string_fields:
        if not hasattr(lead, field):
            continue
        value = getattr(lead, field, None)
        if value is None:
            continue
        if isinstance(value, list):
            setattr(lead, field, ", ".join(str(x).strip() for x in value if x) or None)
        elif isinstance(value, dict):
            setattr(lead, field, str(value)[:500])
        elif field == "phone":
            setattr(lead, field, normalize_phone(str(value)))
        elif field == "city":
            setattr(lead, field, canonical_supported_city(str(value)))
        else:
            setattr(lead, field, str(value).strip() or None)

    for field in int_fields:
        if not hasattr(lead, field):
            continue
        value = getattr(lead, field, None)
        if value is None or isinstance(value, int):
            continue
        parsed = _intish(value)
        setattr(lead, field, parsed)

    for field in bool_fields:
        if not hasattr(lead, field):
            continue
        value = getattr(lead, field, None)
        if value is None or isinstance(value, bool):
            continue
        setattr(lead, field, _boolish(value))

    if not isinstance(lead.notes, list):
        lead.notes = [str(lead.notes)[:250]] if lead.notes else []
    else:
        lead.notes = [str(n)[:250] for n in lead.notes if n]

    return lead


def score_lead(lead: LeadInfo) -> LeadInfo:
    score = 0

    if lead.phone:
        score += 25
    if lead.name:
        score += 10
    if lead.city and looks_like_supported_city(lead.city):
        score += 15
    if lead.service:
        score += 10
    if lead.timeline:
        score += 10
    if lead.urgency == "urgent":
        score += 15
    elif lead.urgency == "soon":
        score += 10
    if lead.repairs_needed:
        score += 5
    if lead.service and any(s in lead.service.lower() for s in ["exterior", "cabinet", "commercial"]):
        score += 10
    if lead.rooms and lead.rooms >= 3:
        score += 10

    lead.lead_score = min(score, 100)

    hot_context = (
        lead.urgency == "urgent"
        and (
            lead.property_type in {"rental", "business"}
            or bool(lead.project_scope)
            or bool(lead.service and any(s in lead.service.lower() for s in ["exterior", "commercial", "cabinet"]))
        )
    )

    if lead.lead_score >= 75 or hot_context:
        lead.lead_priority = "Hot"
    elif lead.lead_score >= 45 or lead.urgency == "soon":
        lead.lead_priority = "Warm"
    else:
        lead.lead_priority = "Normal"

    return lead


def build_conversation_summary(lead: LeadInfo) -> str:
    parts: list[str] = []
    for label, value in [
        ("Customer", lead.name),
        ("Service", lead.service),
        ("City", lead.city),
        ("Address", lead.address),
        ("Property", lead.property_type),
        ("Scope", lead.project_scope),
        ("Timeline", lead.timeline),
        ("Callback preference", lead.preferred_callback_time),
        ("Priority", lead.lead_priority),
    ]:
        if value:
            parts.append(f"{label}: {value}.")

    if lead.stories:
        parts.append(f"Stories: {lead.stories}.")
    if lead.room_size_sqft:
        parts.append(f"Approximate size: {lead.room_size_sqft} sq ft.")
    if lead.rooms:
        parts.append(f"Rooms: {lead.rooms}.")
    if lead.repairs_needed is True:
        parts.append("Repairs or prep may be needed.")
    if lead.notes:
        parts.append(f"Notes: {'; '.join(lead.notes)}.")

    return " ".join(parts) if parts else "No project details collected yet."


def build_final_call_json(
    session_id: str,
    lead: LeadInfo,
    missing: list[str],
    ready: bool,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "created_at": datetime.utcnow().isoformat(),
        "customer": {
            "name": lead.name,
            "phone": lead.phone,
            "email": lead.email,
            "preferred_callback_time": lead.preferred_callback_time,
        },
        "project": {
            "address": lead.address,
            "city": lead.city,
            "service": lead.service,
            "property_type": lead.property_type,
            "stories": lead.stories,
            "occupied": lead.occupied,
            "project_scope": lead.project_scope,
            "room_size_sqft": lead.room_size_sqft,
            "rooms": lead.rooms,
            "walls_only": lead.walls_only,
            "ceiling": lead.ceiling,
            "trim": lead.trim,
            "repairs_needed": lead.repairs_needed,
            "cabinet_count": lead.cabinet_count,
            "timeline": lead.timeline,
            "urgency": lead.urgency,
            "notes": lead.notes,
        },
        "lead_status": {
            "ready_to_send_to_painter": ready,
            "missing_fields": missing,
            "intent": lead.intent,
            "handoff_required": lead.handoff_required,
            "lead_score": lead.lead_score,
            "lead_priority": lead.lead_priority,
        },
        "conversation": {
            "summary": build_conversation_summary(lead),
            "transcript": get_transcript(session_id),
        },
        "metrics": {"latency_ms": latency_ms},
    }


def is_ready_to_save(lead: LeadInfo) -> bool:
    return len(missing_fields(lead)) == 0


def get_background_reasoner_status(session_id: str) -> dict[str, Any] | None:
    """Compatibility for the existing dashboard.

    The simplified version no longer runs a second background reasoner. One LLM
    call already returns both reply and lead patch.
    """
    return None


def _base_metrics(latency_ms: int, *, source: str) -> dict[str, Any]:
    return {
        "latency_ms": latency_ms,
        "architecture": "single_file_llm_receptionist",
        "reasoner_source": source,
        "reasoner_used": source == "llm_receptionist",
        "reasoner_trigger": source,
        "reasoner_confidence": None,
        "reasoner_latency_ms": 0,
        "reasoner_reason": None,
        "reasoner_patch": None,
        "llm_configured": llm_first_available(),
        "llm_deferred": False,
        "talker_source": source,
        "talker_used": source == "llm_receptionist",
        "talker_latency_ms": 0,
        "talker_reason": None,
        "cache_hit": False,
        "cache_kind": None,
        "cache_stats": ai_cache_stats(),
    }


def _cache_or_faq_reply(session_id: str, message: str, lead: LeadInfo, start: float) -> dict[str, Any] | None:
    """Answer stable company facts without spending an LLM call."""
    fast_answer = get_fast_company_answer(message)
    cache_kind = "company_cache"

    if not fast_answer and is_standalone_faq(message):
        faq = deterministic_faq_answer(message)
        if faq:
            fast_answer, faq_key = faq
            cache_kind = f"faq:{faq_key}"

    if not fast_answer:
        return None

    city = find_city_in_message(message)
    if city:
        canonical_city = canonical_supported_city(city)
        if canonical_city:
            lead.city = canonical_city

    service = find_service_in_message(message)
    if service:
        lead.service = service

    lead = score_lead(sanitize_lead_schema(lead))
    missing = missing_fields(lead)
    reply = append_next_question_to_answer(fast_answer, missing)

    add_transcript_message(session_id, "customer", message.strip())
    add_transcript_message(session_id, "ai", reply)

    latency_ms = int((time.perf_counter() - start) * 1000)
    metrics = _base_metrics(latency_ms, source=cache_kind)
    metrics.update(
        {
            "lead_score": lead.lead_score,
            "lead_priority": lead.lead_priority,
            "reasoner_used": False,
            "reasoner_confidence": 1.0,
            "reasoner_reason": "Answered from deterministic cache/FAQ and appended the next lead-capture question.",
            "talker_used": False,
            "talker_reason": "No LLM needed for this stable factual answer.",
            "cache_hit": True,
            "cache_kind": cache_kind,
        }
    )

    return {
        "reply": reply,
        "lead": lead,
        "missing_fields": missing,
        "ready_to_send_to_painter": len(missing) == 0,
        "handoff_required": lead.handoff_required,
        "final_call_json": None,
        "metrics": metrics,
    }


def handle_message(session_id: str, message: str) -> dict[str, Any]:
    """Main chat/call entry point used by FastAPI.

    Flow:
    1. Ignore assistant echo.
    2. Answer deterministic company/FAQ questions immediately.
    3. Use one LLM call for natural reply + structured lead patch.
    4. Sanitize, score, save, and return the same response shape as before.
    """
    start = time.perf_counter()
    lead = get_lead(session_id)

    previous_ai = last_ai_reply(session_id)
    phone_before = lead.phone

    if customer_done_signal(message) and (final_check_context(previous_ai) or _lead_has_enough_for_final_check(lead)):
        add_transcript_message(session_id, "customer", message.strip())
        lead = score_lead(sanitize_lead_schema(lead))
        missing = missing_fields(lead)
        ready = len(missing) == 0
        reply = closing_reply(lead)
        add_transcript_message(session_id, "ai", reply)

        final_call_json: dict[str, Any] | None = None
        latency_ms = int((time.perf_counter() - start) * 1000)

        if (ready or lead.handoff_required) and not lead.saved:
            try:
                final_call_json = build_final_call_json(session_id, lead, missing, ready, latency_ms)
                save_lead_to_db(session_id, lead, final_call_json)
                save_final_call_json(session_id, final_call_json)
                lead.saved = True
            except Exception:
                logger.exception("Failed to save lead/final call JSON.")

        metrics = _base_metrics(latency_ms, source="customer_done")
        metrics.update(
            {
                "lead_score": lead.lead_score,
                "lead_priority": lead.lead_priority,
                "reasoner_used": False,
                "reasoner_confidence": 1.0,
                "reasoner_reason": "Customer said they were done after the final check; closed without asking another question.",
                "talker_used": False,
                "talker_reason": "Deterministic close.",
            }
        )

        return {
            "reply": reply,
            "lead": lead,
            "missing_fields": missing,
            "ready_to_send_to_painter": ready,
            "handoff_required": lead.handoff_required,
            "final_call_json": final_call_json,
            "metrics": metrics,
        }

    if is_probable_assistant_echo(message, previous_ai):
        missing = missing_fields(lead)
        latency_ms = int((time.perf_counter() - start) * 1000)
        metrics = _base_metrics(latency_ms, source="echo_guard")
        metrics.update(
            {
                "reasoner_used": False,
                "reasoner_confidence": 1.0,
                "reasoner_reason": "Ignored probable echo of the receptionist's previous reply.",
                "talker_used": False,
                "talker_reason": "Returned previous assistant reply.",
            }
        )
        return {
            "reply": previous_ai or "Sorry, I may have picked up my own audio there. Could you repeat that?",
            "lead": lead,
            "missing_fields": missing,
            "ready_to_send_to_painter": len(missing) == 0,
            "handoff_required": lead.handoff_required,
            "final_call_json": None,
            "metrics": metrics,
        }

    cache_result = _cache_or_faq_reply(session_id, message, lead, start)
    if cache_result:
        return cache_result

    if not llm_first_available():
        add_transcript_message(session_id, "customer", message.strip())
        reply = (
            "I can help, but the AI receptionist is not configured right now. "
            "Please set OPENAI_API_KEY, then try again."
        )
        add_transcript_message(session_id, "ai", reply)
        missing = missing_fields(lead)
        latency_ms = int((time.perf_counter() - start) * 1000)
        metrics = _base_metrics(latency_ms, source="unavailable")
        metrics.update(
            {
                "reasoner_used": False,
                "reasoner_confidence": 0.0,
                "reasoner_trigger": "llm_not_configured",
                "reasoner_reason": "Natural lead capture now lives in llm_receptionist, but no LLM client is configured.",
                "talker_used": False,
                "talker_reason": "No LLM client configured.",
            }
        )
        return {
            "reply": reply,
            "lead": lead,
            "missing_fields": missing,
            "ready_to_send_to_painter": False,
            "handoff_required": lead.handoff_required,
            "final_call_json": None,
            "metrics": metrics,
        }

    transcript_for_llm = get_transcript(session_id) + [
        {
            "speaker": "customer",
            "text": message.strip(),
            "timestamp": datetime.utcnow().isoformat(),
        }
    ]

    try:
        llm_start = time.perf_counter()
        draft_reply, patch, debug = run_llm_receptionist(
            message,
            lead,
            transcript_for_llm,
            timeout_label="llm",
        )
        llm_latency_ms = int((time.perf_counter() - llm_start) * 1000)

        lead = apply_lead_patch(lead, patch)
        lead = sanitize_lead_schema(lead)
        lead = score_lead(lead)

        hours_issue = business_hours_violation(message)
        bad_phone = invalid_phone_retry_needed(message, previous_ai, phone_before, lead)
        unsupported_city = patch_unsupported_city(patch)

        if unsupported_city:
            lead.city = None
            reply = unsupported_city_reply(unsupported_city)
        elif bad_phone:
            lead.phone = None
            reply = "Sorry, could you repeat your phone number? I need a valid callback number."
        elif hours_issue:
            # Do not accept a requested time that the company cannot support.
            # Clear timing fields so the lead is not marked ready until the
            # customer gives a valid business-hours timeline.
            if lead.timeline:
                append_note(lead, f"Rejected outside-hours timeline: {lead.timeline}")
            if lead.preferred_callback_time:
                append_note(lead, f"Rejected outside-hours callback preference: {lead.preferred_callback_time}")
            lead.timeline = None
            lead.preferred_callback_time = None
            if lead.urgency not in {"urgent", "soon", "flexible"}:
                lead.urgency = None
            reply = business_hours_retry_reply()
        else:
            reply = sanitize_llm_reply(draft_reply, message, lead)

        missing = missing_fields(lead)
        if missing and "?" not in reply:
            reply = append_next_question_to_answer(reply, missing)

    except Exception as exc:
        logger.exception("LLM receptionist failed.")
        reply = "Sorry, I had trouble understanding that. Could you say that another way?"
        patch = None
        debug = {
            "model": None,
            "confidence": 0.0,
            "latency_ms": 0,
            "reason": f"LLM receptionist failed: {exc!r}",
            "cache_hit": False,
        }
        llm_latency_ms = 0
        missing = missing_fields(lead)

    add_transcript_message(session_id, "customer", message.strip())
    add_transcript_message(session_id, "ai", reply)

    ready = len(missing) == 0
    final_call_json: dict[str, Any] | None = None
    latency_ms = int((time.perf_counter() - start) * 1000)

    if (ready or lead.handoff_required) and not lead.saved:
        try:
            final_call_json = build_final_call_json(session_id, lead, missing, ready, latency_ms)
            save_lead_to_db(session_id, lead, final_call_json)
            save_final_call_json(session_id, final_call_json)
            lead.saved = True
        except Exception:
            logger.exception("Failed to save lead/final call JSON.")

    metrics = _base_metrics(latency_ms, source="llm_receptionist")
    metrics.update(
        {
            "lead_score": lead.lead_score,
            "lead_priority": lead.lead_priority,
            "reasoner_used": True,
            "reasoner_trigger": "cache_and_faq_miss",
            "reasoner_confidence": debug.get("confidence"),
            "reasoner_latency_ms": debug.get("latency_ms", llm_latency_ms),
            "reasoner_reason": debug.get("reason"),
            "reasoner_patch": patch,
            "llm_configured": True,
            "llm_model": debug.get("model"),
            "talker_used": True,
            "talker_latency_ms": debug.get("latency_ms", llm_latency_ms),
            "talker_reason": "One LLM call generated the reply and structured lead patch; deterministic guardrails sanitized the result.",
            "cache_hit": bool(debug.get("cache_hit")),
            "cache_kind": "ai_response" if debug.get("cache_hit") else None,
            "cache_stats": ai_cache_stats(),
        }
    )

    return {
        "reply": reply,
        "lead": lead,
        "missing_fields": missing,
        "ready_to_send_to_painter": ready,
        "handoff_required": lead.handoff_required,
        "final_call_json": final_call_json,
        "metrics": metrics,
    }
