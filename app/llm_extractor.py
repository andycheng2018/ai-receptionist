<<<<<<< HEAD
import json
import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from app.models import LeadInfo

import time

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=api_key) if api_key else None


def extract_info_with_llm(message: str, current_lead: LeadInfo) -> dict[str, Any]:
    """
    Uses an LLM to extract structured lead information from messy customer text.

    If anything goes wrong, return {} so the rule-based regex extractor can still work.
    """

    if client is None:
        print("LLM extraction skipped: OPENAI_API_KEY is missing.")
        return {}

    prompt = f"""
You are an information extraction engine for an AI receptionist for a painting business.

Extract only information explicitly stated or strongly implied by the customer's latest message.
Do not invent missing information.

Current lead information:
{current_lead.model_dump()}

Customer message:
{message}

Return ONLY valid JSON with these fields:
{{
  "intent": "painting_estimate | price_question | duration_question | service_area_question | repair_question | booking_request | unknown",
  "name": string or null,
  "phone": string or null,
  "city": string or null,
  "service": string or null,
  "room_size_sqft": number or null,
  "rooms": number or null,
  "walls_only": true/false/null,
  "ceiling": true/false/null,
  "trim": true/false/null,
  "timeline": string or null,
  "notes": string or null
}}

Rules:
- If customer says "10 by 12", convert to 120 square feet.
- If customer says "small bedroom" but gives no dimensions, do not guess square footage.
- If customer says "walls only", set walls_only true, ceiling false, trim false.
- If customer mentions ceiling, set ceiling true.
- If customer mentions trim, baseboards, or molding, set trim true.
- If customer mentions cracks, holes, peeling paint, damage, or repair, put that in notes.
- If customer asks "how long", intent should be duration_question.
- If customer asks "how much", "cost", "price", or "quote", intent should be price_question.
- If customer asks whether the business works in a city, intent should be service_area_question.
"""

    try:
        start_time = time.perf_counter()

        response = client.responses.create(
            model="gpt-5.4-mini",
            input=prompt,
        )

        end_time = time.perf_counter()
        llm_latency = end_time - start_time

        print(f"LLM extraction latency: {llm_latency:.3f} seconds")

        raw_text = response.output_text.strip()

        if raw_text.startswith("```"):
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()

        print("LLM raw output:", raw_text)

        return json.loads(raw_text)

    except Exception as e:
        print("LLM extraction failed:", repr(e))
        return {}
=======
"""Hybrid AI extraction fallback for messy receptionist turns.

This module returns a *patch* instead of directly mutating the lead:
{
  "set": {"field": value},
  "clear": ["stale_field"],
  "is_correction": true,
  "confidence": 0.0-1.0,
  "source": "llm|heuristic"
}

Why a patch?
- Rules are fast, but stale fields can survive corrections.
- An extractor should say both what changed and what should be cleared.
- The deterministic receptionist flow still decides the next question.
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

load_dotenv()


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


client = _make_client()


def llm_available() -> bool:
    """Whether a real OpenAI-compatible LLM client is configured."""
    return client is not None


PATCHABLE_FIELDS = {
    "intent", "name", "phone", "email", "address", "city", "service", "property_type",
    "stories", "room_size_sqft", "rooms", "walls_only", "ceiling", "trim", "occupied",
    "repairs_needed", "photos_available", "cabinet_count", "project_scope", "timeline",
    "urgency", "preferred_callback_time", "handoff_required", "notes",
}

PROJECT_FIELDS = [
    "city", "address", "service", "property_type", "stories", "room_size_sqft", "rooms",
    "walls_only", "ceiling", "trim", "occupied", "repairs_needed", "photos_available",
    "cabinet_count", "project_scope", "timeline", "urgency", "preferred_callback_time",
]


def _title_city(candidate: str) -> str:
    candidate = re.sub(r"[^a-zA-Z .'-]", " ", candidate.strip(), flags=re.IGNORECASE).strip().lower()
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = re.sub(
        r"\b(?:and|but|needs?|that|with|before|after|paint|painted|painting|project|property|rental|house|home|unit|job|place)\b.*$",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()
    return " ".join(part.capitalize() for part in candidate.split())


def _extract_project_city(text: str) -> str | None:
    """Prefer the project/property city over the caller's home/calling city."""
    patterns = [
        # City before property noun: "the Daly City rental", "San Mateo property".
        r"\b(?:in|at|for)\s+(?:the\s+)?((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+mateo|[a-z]+\s+bruno|[a-z]+\s+alto)\s+(?:rental|property|project|place|house|home|unit|job|condo|apartment|office)\b",
        r"\b((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+mateo|[a-z]+\s+bruno|[a-z]+\s+alto)\s+(?:rental|property|project|place|house|home|unit|job|condo|apartment|office)\b",
        r"\b(?:property|project|place|rental|house|home|unit|job)\s+(?:that\s+needs\s+painting\s+)?(?:is\s+)?(?:in|at)\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+mateo|[a-z]+\s+bruno|[a-z]+\s+alto)\b",
        r"\b(?:property|project|place|rental|house|home|unit|job)[^,.;?!]{0,60}?\b(?:in|at)\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+mateo|[a-z]+\s+bruno|[a-z]+\s+alto)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _title_city(match.group(1))
    # Common contrast: "I live/call from X, but ... in Y". Prefer city after but
    # only when the after-but clause refers to the property/project.
    contrast = re.search(
        r"\b(?:i\s+live\s+in|i'?m\s+calling\s+from|calling\s+from)\s+[^,.;?!]+,?\s+but\s+(?:the\s+)?(?:property|project|place|rental|house|home|unit|job)[^,.;?!]{0,60}?\bin\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+mateo|[a-z]+\s+bruno|[a-z]+\s+alto)\b",
        text,
        re.IGNORECASE,
    )
    if contrast:
        return _title_city(contrast.group(1))
    return None


NON_NAME_WORDS = {
    "paint", "painting", "painter", "project", "job", "rental", "property",
    "house", "home", "place", "wall", "walls", "cabinet", "cabinets",
    "repair", "repairs", "interior", "exterior", "bedroom", "bedrooms",
    "kitchen", "hallway", "window", "bubbling", "drywall", "service", "quote",
}


def _looks_like_person_name(candidate: str) -> bool:
    candidate = re.sub(r"\s+", " ", candidate.strip())
    if not candidate or len(candidate.split()) > 3:
        return False
    lowered = candidate.lower().strip()
    invalid_starts = (
        "a ", "an ", "the ", "this ", "it ", "is ", "not ", "not sure",
        "looking", "trying", "calling", "interested", "wondering", "hoping",
        "thinking", "need", "want", "from", "at", "painting", "paint",
    )
    if lowered.startswith(invalid_starts):
        return False
    words = set(re.findall(r"[a-z]+", lowered))
    if words & NON_NAME_WORDS:
        return False
    return True


def _extract_basic_contact(text: str, set_values: dict[str, Any]) -> None:
    phone_match = re.search(r"\b(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b", text)
    if phone_match:
        digits = re.sub(r"\D", "", phone_match.group(1))
        if len(digits) == 10:
            set_values["phone"] = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    name_match = re.search(r"\b(?:my name is|i'm|i am|this is)\s+([a-z][a-z .'-]{0,40})", text, re.IGNORECASE)
    if name_match:
        name = re.split(r"\band\b|\bmy phone\b|\bphone\b|\bnumber\b|,|[.?!]", name_match.group(1).strip(), maxsplit=1)[0].strip()
        if _looks_like_person_name(name):
            set_values["name"] = " ".join(part.capitalize() for part in name.split())
    emailish = text.replace(" at ", "@").replace(" dot ", ".")
    email_match = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.sub(r"\s+", "", emailish))
    if email_match:
        set_values["email"] = email_match.group(0).lower()


def _clean_json(raw_text: str) -> dict[str, Any]:
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        return {}
    return data


def _parse_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "y", "needed", "needs repair", "repair needed"}:
            return True
        if low in {"false", "no", "n", "none", "not needed", "no repair"}:
            return False
        if any(w in low for w in ["repair", "damage", "stain", "bubbling", "leak", "water", "patch", "crack"]):
            return True
    return None


def _coerce_set_values(set_values: dict[str, Any]) -> dict[str, Any]:
    """Coerce model outputs into the strict LeadInfo schema.

    LLMs sometimes put a repair description into repairs_needed, but the app
    schema expects repairs_needed to be boolean and project_scope/notes to carry
    the description. Keep the lead JSON type-safe so downstream scoring/UI does
    not get polluted.
    """
    coerced: dict[str, Any] = {}
    notes_to_add: list[str] = []
    for key, value in set_values.items():
        if key not in PATCHABLE_FIELDS or value in (None, "", []):
            continue
        if key in {"repairs_needed", "photos_available", "walls_only", "ceiling", "trim", "occupied", "handoff_required"}:
            bool_value = _parse_boolish(value)
            if bool_value is None:
                continue
            coerced[key] = bool_value
            if key == "repairs_needed" and isinstance(value, str) and len(value.strip()) > 8:
                notes_to_add.append(f"Repair details: {value.strip()}")
            continue
        if key in {"stories", "room_size_sqft", "rooms", "cabinet_count"}:
            if isinstance(value, int):
                coerced[key] = value
            elif isinstance(value, str):
                digits = re.search(r"\d+", value)
                if digits:
                    coerced[key] = int(digits.group(0))
            continue
        if key == "notes":
            if isinstance(value, list):
                notes_to_add.extend(str(v)[:250] for v in value if v)
            elif isinstance(value, str):
                notes_to_add.append(value[:250])
            continue
        coerced[key] = value
    if notes_to_add:
        existing = coerced.get("notes")
        if isinstance(existing, list):
            existing.extend(notes_to_add)
        else:
            coerced["notes"] = notes_to_add
    return coerced


def _normalize_patch(data: dict[str, Any]) -> dict[str, Any]:
    set_values = data.get("set") or {}
    clear_values = data.get("clear") or []

    # Backward-compatible: if a model returned flat fields, turn them into set values.
    for key, value in list(data.items()):
        if key in PATCHABLE_FIELDS and value not in (None, "", []):
            set_values.setdefault(key, value)

    if not isinstance(set_values, dict):
        set_values = {}
    if not isinstance(clear_values, list):
        clear_values = []

    set_values = {k: v for k, v in set_values.items() if k in PATCHABLE_FIELDS and v not in (None, "", [])}
    set_values = _coerce_set_values(set_values)
    clear_values = [k for k in clear_values if k in PATCHABLE_FIELDS and k not in {"name", "phone", "email"}]

    return {
        "set": set_values,
        "clear": clear_values,
        "is_correction": bool(data.get("is_correction", False)),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "reason": str(data.get("reason", ""))[:500],
        "source": data.get("source", "llm"),
    }


def _latest_customer_text(message: str) -> str:
    """For post-call cleanup, reason from the latest customer correction first."""
    matches = re.findall(r"(?ims)^customer:\s*(.*?)(?=^ai receptionist:|^ai:|^customer:|\Z)", message)
    if matches:
        return matches[-1].strip().lower()
    return message.lower()


def _join_parts(parts: list[str]) -> str:
    parts = [p for p in dict.fromkeys(parts) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _sanitize_patch_with_message(patch: dict[str, Any], message: str, current_lead: LeadInfo, reasoner_mode: str | None = None) -> dict[str, Any]:
    """Guardrail around both LLM and heuristic patches.

    The model may understand the transcript but still copy stale current-lead
    fields into the output. This sanitizer applies high-confidence transcript
    rules after the LLM, especially for post-call cleanup where the latest
    correction should win.
    """
    text = message.lower().replace("’", "'")
    latest = _latest_customer_text(message)
    set_values = dict(patch.get("set") or {})
    clear = list(patch.get("clear") or [])

    correction = any(p in latest for p in ["actually", "forget", "ignore that", "explained this wrong", "explained that badly", "not really", "what i really need"])

    # Project city beats caller location.
    project_city = _extract_project_city(latest)
    if project_city:
        set_values["city"] = project_city

    # Mixed interior + exterior prompt, e.g. front door outside plus hallway touch-ups inside.
    mixed_inside_outside = (
        not any(neg in latest for neg in ["not outside", "not the outside", "not exterior", "not the exterior"])
        and not any(neg in latest for neg in ["not inside", "not interior", "not bedrooms", "not bedroom"])
        and any(p in latest for p in ["outside", "exterior", "front door", "siding", "garage door"])
        and any(p in latest for p in ["inside", "interior", "hallway", "living room", "bedroom", "kitchen"])
        and any(p in latest for p in ["also", "but also", "both", "plus", "and"])
    )
    if mixed_inside_outside:
        set_values["service"] = "exterior door painting + interior touch-up" if "front door" in latest else "exterior painting + interior touch-up"
        scope_parts: list[str] = []
        if "front door" in latest:
            scope_parts.append("front door")
        elif "garage door" in latest:
            scope_parts.append("garage door")
        elif "siding" in latest:
            scope_parts.append("siding")
        if "hallway" in latest:
            scope_parts.append("hallway touch-ups" if "touch" in latest else "hallway")
        if "living room" in latest:
            scope_parts.append("living room touch-ups" if "touch" in latest else "living room")
        if scope_parts:
            set_values["project_scope"] = _join_parts(scope_parts)
        if "rental" in latest:
            set_values["property_type"] = "rental"
        if "new tenants" in latest or "tenants arrive" in latest or "tenant arrive" in latest:
            set_values["timeline"] = "before new tenants arrive next weekend" if "next weekend" in latest else "before new tenants arrive"
            set_values["urgency"] = "urgent"

    # Rich repair + cabinet prompt.
    if any(p in latest for p in ["bubbling", "water staining", "water stain", "drywall", "repair"]) and "cabinet" in latest:
        set_values["service"] = "drywall repair + painting + cabinet painting"
        set_values["repairs_needed"] = True
        scope_parts: list[str] = []
        if "bay window" in latest:
            scope_parts.append("paint bubbling under bay window" if "under" in latest else "bubbling area around bay window")
        elif "window" in latest and "bubbling" in latest:
            scope_parts.append("bubbling paint near window")
        if "kitchen ceiling" in latest and ("water" in latest or "stain" in latest):
            scope_parts.append("water staining near kitchen ceiling")
        elif "ceiling" in latest and ("water" in latest or "stain" in latest):
            scope_parts.append("ceiling water staining")
        if "cabinet doors" in latest:
            scope_parts.append("worn cabinet doors")
        elif "kitchen" in latest and "cabinet" in latest:
            scope_parts.append("kitchen cabinets")
        if scope_parts:
            set_values["project_scope"] = _join_parts(scope_parts)

    if any(p in latest for p in ["dental office", "office"]) and any(p in latest for p in ["waiting room", "lobby", "conference room"]):
        set_values["service"] = "interior painting"
        set_values["property_type"] = "commercial office"
        if "waiting room" in latest:
            set_values["project_scope"] = "waiting room"
        elif "lobby" in latest and "conference room" in latest:
            set_values["project_scope"] = "lobby and conference room"
        if "after 6" in latest and "weekend" in latest:
            set_values["preferred_callback_time"] = "after 6pm or weekend"
        elif "after 6" in latest:
            set_values["preferred_callback_time"] = "after 6pm"
        elif "weekend" in latest:
            set_values["preferred_callback_time"] = "weekend"

    if (any(p in latest for p in ["ceiling", "bathroom", "stain", "stains"]) or ("peeling" in latest and not any(p in latest for p in ["exterior", "outside", "front exterior", "outside front", "siding"]))) and any(p in latest for p in ["landlord", "unit", "rental"]):
        set_values["property_type"] = "rental"
        set_values["service"] = "paint repair / repainting"
        set_values["repairs_needed"] = True
        scope_parts: list[str] = []
        if "bubbling" in latest and "window" in latest:
            if "under" in latest:
                scope_parts.append("bubbling under window")
            else:
                scope_parts.append("bubbling near window")
        if "ceiling" in latest and any(p in latest for p in ["stain", "stains", "water"]):
            scope_parts.append("ceiling stains")
        if "bathroom" in latest and "peeling" in latest:
            scope_parts.append("bathroom peeling paint")
        if scope_parts:
            set_values["project_scope"] = _join_parts(scope_parts)

    # Tenant moved out / showings correction.
    if any(p in latest for p in ["tenant moved out", "moved out"]) and any(p in latest for p in ["scuffs", "bubbling", "front door", "showings"]):
        set_values["property_type"] = "rental"
        set_values["service"] = "interior touch-up + paint repair + door painting"
        set_values["repairs_needed"] = True
        scope_parts = []
        if "hallway" in latest and "scuff" in latest:
            scope_parts.append("hallway wall scuffs")
        elif "hallway" in latest:
            scope_parts.append("hallway")
        if "bubbling" in latest and "window" in latest:
            scope_parts.append("bubbling paint near window")
        if "front door" in latest:
            scope_parts.append("front door")
        if scope_parts:
            set_values["project_scope"] = _join_parts(scope_parts)
        if "before showings next week" in latest:
            set_values["timeline"] = "before showings next week"
            set_values["urgency"] = "urgent"

    if any(p in latest for p in ["before she moves in", "before my mom moves in", "before mom moves in"]):
        set_values["timeline"] = "before mom moves in"
        set_values.setdefault("urgency", "soon")
    elif "before my sister moves in" in latest or "before sister moves in" in latest:
        set_values["timeline"] = "before sister moves in"
        set_values.setdefault("urgency", "soon")

    if correction:
        # Latest correction should be allowed to replace stale project facts.
        for field in ["service", "project_scope", "rooms", "room_size_sqft", "stories", "cabinet_count", "property_type", "timeline", "urgency", "repairs_needed"]:
            if field not in clear and field not in {"name", "phone", "email"}:
                clear.append(field)
        patch["is_correction"] = True
        patch["reason"] = patch.get("reason") or "Latest customer correction replaced earlier project details."

    patch["set"] = _coerce_set_values(set_values)
    patch["clear"] = [c for c in dict.fromkeys(clear) if c in PATCHABLE_FIELDS and c not in {"name", "phone", "email"}]
    return patch


def heuristic_patch(message: str, current_lead: LeadInfo) -> dict[str, Any]:
    """Small deterministic fallback for common correction patterns.

    This is not meant to replace the LLM. It makes the demo reliable when no API
    key is configured and covers the exact cases that rules tend to corrupt.
    """
    text = message.lower().replace("’", "'").strip()
    set_values: dict[str, Any] = {}
    clear: list[str] = []
    is_correction = any(p in text for p in ["actually", "ignore that", "correction", "sorry", "i meant", "instead", "not outside", "not bedrooms"])

    # Customer city vs project city is not necessarily a correction, but rules
    # often grab the first city. Prefer the city attached to project/property.
    project_city = _extract_project_city(text)
    if project_city and ("i live in" in text or "calling from" in text):
        set_values["city"] = project_city

    # Non-correction messy/all-in-one enrichment. Rules handle the basics, but
    # long natural messages often contain scope + timeline that regex misses.
    if not is_correction:
        _extract_basic_contact(text, set_values)

        rich_ambiguous_repair = (
            ("not sure" in text or "or both" in text or "repair job" in text)
            and any(p in text for p in ["bubbling", "water", "stain", "drywall", "repair"])
            and any(p in text for p in ["cabinet", "cabinets"])
        )
        mixed_inside_outside = (
            not any(neg in text for neg in ["not outside", "not the outside", "not exterior", "not the exterior"])
            and not any(neg in text for neg in ["not inside", "not interior", "not bedrooms", "not bedroom"])
            and any(p in text for p in ["outside", "exterior", "front door", "siding", "garage door"])
            and any(p in text for p in ["inside", "interior", "hallway", "living room", "bedroom", "kitchen"])
            and any(p in text for p in ["also", "but also", "both", "plus", "and"])
        )
        if mixed_inside_outside:
            set_values["service"] = "exterior door painting + interior touch-up" if "front door" in text else "exterior painting + interior touch-up"
            scope_parts = []
            if "front door" in text:
                scope_parts.append("front door")
            elif "garage door" in text:
                scope_parts.append("garage door")
            elif "siding" in text:
                scope_parts.append("siding")
            if "hallway" in text:
                scope_parts.append("hallway touch-ups" if "touch" in text else "hallway")
            if "living room" in text:
                scope_parts.append("living room touch-ups" if "touch" in text else "living room")
            if scope_parts:
                set_values["project_scope"] = _join_parts(scope_parts)
            if "rental" in text:
                set_values["property_type"] = "rental"
            if "new tenants" in text or "tenants arrive" in text or "tenant arrive" in text:
                set_values["timeline"] = "before new tenants arrive next weekend" if "next weekend" in text else "before new tenants arrive"
                set_values["urgency"] = "urgent"
        elif rich_ambiguous_repair:
            set_values["service"] = "drywall repair + painting + cabinet painting"
            set_values["repairs_needed"] = True
            scope_parts = []
            if "bay window" in text:
                scope_parts.append("bubbling area around bay window")
            elif "window" in text:
                scope_parts.append("bubbling area near window")
            elif "bubbling" in text:
                scope_parts.append("bubbling paint area")
            if "kitchen" in text and "cabinet" in text:
                scope_parts.append("kitchen cabinets")
            if scope_parts:
                set_values["project_scope"] = " and ".join(scope_parts)
        elif any(p in text for p in ["cabinet" , "cabinets"]) and any(p in text for p in ["touch up", "touch-up", "walls", "living room", "also", "too"]):
            set_values["service"] = "cabinet painting + interior touch-up"
            scope_parts = []
            if "kitchen" in text and "cabinet" in text:
                scope_parts.append("kitchen cabinets")
            if "living room" in text:
                scope_parts.append("living room walls")
            if scope_parts:
                set_values["project_scope"] = " and ".join(scope_parts)
        elif any(p in text for p in ["drywall", "leak", "water damage", "water staining", "water stain", "stains", "stain", "peeling", "bubbling", "repaired and painted", "repair and repaint"]) and not any(p in text for p in ["exterior", "outside", "front exterior", "outside front", "siding"]):
            set_values["service"] = "paint repair / repainting"
            set_values["repairs_needed"] = True
            scope_parts = []
            if "ceiling" in text and any(p in text for p in ["stain", "stains", "water"]):
                scope_parts.append("ceiling stains")
            elif "ceiling" in text:
                scope_parts.append("ceiling")
            if "bathroom" in text and "peeling" in text:
                scope_parts.append("bathroom peeling paint")
            if "bubbling" in text and "one wall" in text:
                scope_parts.append("bubbling paint and one wall")
            elif "bubbling" in text and "window" in text:
                if "under" in text:
                    scope_parts.append("bubbling under window")
                else:
                    scope_parts.append("bubbling near window")
            elif "bubbling" in text:
                scope_parts.append("bubbling paint")
            if scope_parts:
                set_values["project_scope"] = _join_parts(scope_parts)
        elif any(p in text for p in ["dental office", "office"]) and any(p in text for p in ["waiting room", "lobby", "conference room"]):
            set_values["service"] = "interior painting"
            set_values["property_type"] = "commercial office"
            if "waiting room" in text:
                set_values["project_scope"] = "waiting room"
            elif "lobby" in text and "conference room" in text:
                set_values["project_scope"] = "lobby and conference room"
            if "after 6" in text and "weekend" in text:
                set_values["preferred_callback_time"] = "after 6pm or weekend"
            elif "after 6" in text:
                set_values["preferred_callback_time"] = "after 6pm"
            elif "weekend" in text:
                set_values["preferred_callback_time"] = "weekend"
        elif any(p in text for p in ["exterior", "outside", "siding", "garage door", "front of"]):
            set_values.setdefault("service", "exterior painting")
        elif any(p in text for p in ["interior", "inside", "hallway", "kitchen", "bedroom", "bedrooms", "living room"]):
            set_values.setdefault("service", "interior painting")

        # Scope: preserve multiple mentioned areas, not just the first one.
        has_front_scope = any(p in text for p in ["front only", "front of my", "front of the", "front exterior", "outside front", "front looks"])
        if has_front_scope:
            set_values.setdefault("project_scope", "front exterior only")
        exterior_parts: list[str] = []
        if not has_front_scope:
            if "siding" in text:
                exterior_parts.append("siding")
            if "trim" in text:
                exterior_parts.append("exterior trim" if "exterior" in text or "outside" in text else "trim")
            if "garage door" in text or "garage doors" in text:
                exterior_parts.append("garage door")
            if exterior_parts and "+" not in (set_values.get("service") or ""):
                set_values["project_scope"] = " and ".join(dict.fromkeys(exterior_parts))
        interior_parts: list[str] = []
        for area in ["hallway", "kitchen", "living room", "dining room", "bathroom", "office"]:
            if area in text:
                interior_parts.append(area)
        if "bedrooms" in text or "bedroom" in text:
            interior_parts.append("bedrooms" if "bedrooms" in text else "bedroom")
        if interior_parts and "cabinet" not in (set_values.get("service") or "") and "+" not in (set_values.get("service") or ""):
            set_values["project_scope"] = " and ".join(dict.fromkeys(interior_parts))

        if "rental" in text or "landlord" in text or "tenant" in text or "unit" in text:
            set_values["property_type"] = "rental"
        if any(p in text for p in ["dental office", "office", "commercial", "storefront", "business"]):
            set_values["property_type"] = "commercial office" if "office" in text else "business"

        # Prefer project/property city if caller separately gives home city.
        project_city = _extract_project_city(text)
        if project_city:
            set_values["city"] = project_city
        else:
            city_match = re.search(r"\bin\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+alto|[a-z]+\s+mateo|[a-z]+\s+bruno)\b", text)
            if city_match:
                set_values["city"] = _title_city(city_match.group(1))
        story_match = re.search(r"\b(\d+|one|two|three|four)\s*(?:story|stories|storey|storeys)\b", text)
        word_to_num = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
        if story_match:
            value = story_match.group(1)
            set_values["stories"] = int(value) if value.isdigit() else word_to_num.get(value)
        rooms_match = re.search(r"\b(\d+|one|two|three|four|five|six)\s+(?:bedrooms?|rooms?)\b", text)
        if rooms_match:
            value = rooms_match.group(1)
            set_values["rooms"] = int(value) if value.isdigit() else word_to_num.get(value)
        if "before my sister moves in" in text or "before sister moves in" in text:
            set_values["timeline"] = "before sister moves in"
            set_values["urgency"] = "soon"
        elif any(p in text for p in ["before next friday", "next friday", "before friday", "tenants move", "move in", "open house", "listing photos", "before photos"]):
            if "before next friday" in text:
                set_values["timeline"] = "before next friday"
            elif "next friday" in text:
                set_values["timeline"] = "next friday"
            elif "open house" in text:
                set_values["timeline"] = "before open house"
            elif "listing photos" in text or "before photos" in text:
                set_values["timeline"] = "before listing photos"
            else:
                set_values["timeline"] = "tenants move in"
            set_values["urgency"] = "urgent"
        appt_match = re.search(r"\b(?:tomorrow|today|this weekend|next weekend)(?:\s+(?:morning|afternoon|evening))?(?:\s+at\s+[0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?)?\b", text)
        if appt_match and any(p in text for p in ["come", "available", "appointment", "schedule", "take a look"]):
            set_values["preferred_callback_time"] = appt_match.group(0)
        confidence = 0.82 if set_values else 0.0
        return {
            "set": set_values,
            "clear": [],
            "is_correction": False,
            "confidence": confidence,
            "reason": "Heuristic Reasoner enrichment for complex customer message." if set_values else "",
            "source": "heuristic",
        }

    # Service switch.
    if any(p in text for p in ["inside", "interior", "hallway", "kitchen", "living room", "bedroom", "bedrooms"]):
        if not any(p in text for p in ["not inside", "not interior"]):
            set_values["service"] = "interior painting"
            clear.extend(["stories", "rooms", "room_size_sqft", "walls_only", "ceiling", "trim", "project_scope"])
    if any(p in text for p in ["outside", "exterior", "siding"]):
        if not any(p in text for p in ["not outside", "not exterior"]):
            set_values["service"] = "exterior painting"
            clear.extend(["rooms", "room_size_sqft", "walls_only", "ceiling", "trim", "project_scope"])

    # Negative phrases can override the obvious words.
    if any(p in text for p in ["not outside", "not the outside", "not exterior", "not the exterior"]):
        if any(p in text for p in ["inside", "interior", "hallway", "kitchen", "bedroom", "bedrooms"]):
            set_values["service"] = "interior painting"
            clear.extend(["stories", "project_scope", "trim", "timeline", "urgency", "repairs_needed"])
    if any(p in text for p in ["not bedrooms", "not bedroom", "not inside", "not interior"]):
        if any(p in text for p in ["outside", "exterior", "siding"]):
            set_values["service"] = "exterior painting"
            clear.extend(["rooms", "room_size_sqft", "walls_only", "ceiling", "trim", "project_scope"])

    # City from latest correction. Preserve old city if no new city is stated.
    city_match = re.search(r"\bin\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+alto|[a-z]+\s+mateo|[a-z]+\s+bruno)\b", text)
    if city_match:
        set_values["city"] = _title_city(city_match.group(1))

    # Scope from the corrected latest message. Avoid capturing negated areas
    # such as "not bedrooms" as the new scope.
    exterior_areas: list[str] = []
    if "siding" in text:
        exterior_areas.append("exterior siding")
    if "garage door" in text or "garage doors" in text:
        exterior_areas.append("garage door")
    if "trim" in text and not any(p in text for p in ["not trim", "no trim"]):
        exterior_areas.append("exterior trim" if set_values.get("service") == "exterior painting" else "trim")
    if exterior_areas and set_values.get("service") == "exterior painting":
        set_values["project_scope"] = " and ".join(dict.fromkeys(exterior_areas))

    # Multi-area interior scope.
    areas: list[str] = []
    negated_bedroom = any(p in text for p in ["not bedroom", "not bedrooms", "not the bedrooms"])
    for area in ["hallway", "kitchen", "living room", "dining room", "bathroom", "office"]:
        if area in text:
            if area not in areas:
                areas.append(area)
    if not negated_bedroom:
        if "bedrooms" in text:
            areas.append("bedrooms")
        elif "bedroom" in text:
            areas.append("bedroom")
    if areas and set_values.get("service") != "exterior painting":
        set_values["project_scope"] = " and ".join(dict.fromkeys(areas))

    rooms_match = re.search(r"\b(\d+|one|two|three|four|five|six)\s+(?:bedrooms?|rooms?)\b", text)
    word_to_num = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    if rooms_match and "not bedroom" not in text and "not bedrooms" not in text:
        value = rooms_match.group(1)
        set_values["rooms"] = int(value) if value.isdigit() else word_to_num.get(value)

    # "ignore that" usually means old context should not survive unless restated.
    if "ignore that" in text:
        clear.extend(["property_type", "timeline", "urgency", "repairs_needed", "photos_available", "stories"])
    elif set_values.get("service") and set_values["service"] != (current_lead.service or ""):
        clear.extend(["timeline", "urgency", "repairs_needed"])

    if "rental" in text:
        set_values["property_type"] = "rental"
    if any(p in text for p in ["next friday", "before friday", "tenants move", "move in"]):
        set_values["timeline"] = "next Friday" if "next friday" in text else "tenants move in"
        set_values["urgency"] = "urgent"

    # De-duplicate clear and do not clear fields that this patch sets.
    clear = [field for field in dict.fromkeys(clear) if field not in set_values]
    confidence = 0.86 if set_values else 0.0
    return {
        "set": set_values,
        "clear": clear,
        "is_correction": is_correction,
        "confidence": confidence,
        "reason": "Heuristic correction patch for latest customer message.",
        "source": "heuristic",
    }


def extract_lead_patch(message: str, current_lead: LeadInfo, *, use_llm: bool = True, reasoner_mode: str | None = None) -> dict[str, Any]:
    """Return a set/clear patch for messy or corrective messages.

    Works with OpenAI or OpenAI-compatible providers. For Qwen, set:
      OPENAI_BASE_URL=http://localhost:8000/v1
      OPENAI_API_KEY=anything
      OPENAI_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
    """
    fallback = heuristic_patch(message, current_lead)

    if not use_llm:
        fallback["llm_configured"] = llm_available()
        fallback["llm_deferred"] = True
        fallback["reasoner_mode"] = reasoner_mode or "live"
        fallback["reason"] = "Live-call mode used fast heuristic patch; real LLM cleanup can run after the call."
        return _sanitize_patch_with_message(fallback, message, current_lead, reasoner_mode)

    if client is None:
        fallback["llm_configured"] = False
        if fallback.get("source") == "heuristic":
            fallback["reason"] = "Real LLM is not configured, so heuristic fallback was used. " + (fallback.get("reason") or "")
        return _sanitize_patch_with_message(fallback, message, current_lead, reasoner_mode)

    model = os.getenv("OPENAI_MODEL") or os.getenv("QWEN_MODEL") or "gpt-4.1-mini"
    latest_customer = _latest_customer_text(message) if reasoner_mode == "post_call_cleanup" else message
    transcript_context = message if reasoner_mode == "post_call_cleanup" else ""
    prompt = f"""
You are the slower Reasoner in a Talker-Reasoner AI receptionist architecture.
The fast Talker/rule layer already handles obvious short replies. Your job is only to understand complex or ambiguous customer language and return a structured state patch for a painting-company lead.

Return ONLY valid JSON. Do not chat. Do not include markdown.

Task:
Given the existing lead state and the customer's latest message, return a PATCH:
- "set": fields that should be created or overwritten
- "clear": stale fields that should be erased because the customer corrected/replaced earlier info
- "is_correction": whether the latest message corrects previous details
- "confidence": 0 to 1
- "reason": short explanation

Existing lead state:
{json.dumps(current_lead.model_dump(), ensure_ascii=False)}

Latest customer message to prioritize:
{latest_customer}

Full transcript/context, if provided:
{transcript_context}

Allowed fields:
{sorted(PATCHABLE_FIELDS)}

Important rules:
- Extract only from the latest message plus obvious correction context.
- If customer says "actually", "ignore that", "forget", "not outside", "not bedrooms", "what I really need", or similar, use clear for conflicting stale fields.
- In post-call cleanup, the latest customer correction wins over older transcript details and over existing lead state.
- Preserve contact fields unless directly corrected.
- repairs_needed must be boolean true/false, never descriptive text. Put descriptive repair details in project_scope or notes.
- service must be specific for mixed jobs, for example "drywall repair + painting + cabinet painting", "interior touch-up + paint repair + door painting", or "exterior door painting + interior touch-up".
- project_scope should contain concrete areas/items, not stale values from the existing lead.
- Preserve city if no new city is stated; overwrite city if the latest message gives a new project city.
- If customer says "I live in X but the project/property is in Y", set city to Y. If multiple cities are mentioned, pick the one attached to property/project/house/unit/rental.
- If customer says "ignore that" and does not restate rental/tenant/open-house info, clear old property_type/timeline/urgency/repairs if they came from the ignored project.
- If latest message says "inside hallway and kitchen", project_scope should be "hallway and kitchen".
- If latest message says "exterior siding, not bedrooms", set service to "exterior painting" and clear rooms.
- Do not ask questions. JSON only.
- Complex questions may include both an FAQ/price/scheduling request and lead details. Extract the lead details anyway.
- If rules may have treated a useful message as invalid/no-op, infer the most likely field from the existing lead state and latest message.
- For scheduling constraints like "only after 6pm or over the weekend", set preferred_callback_time. For "come tomorrow morning at 8", set preferred_callback_time but do not promise availability.
- For spoken email like "andy dot chen at gmail dot com", set email to "andy.chen@gmail.com".

JSON shape:
{{
  "set": {{"city": "Daly City", "service": "interior painting", "project_scope": "hallway and kitchen"}},
  "clear": ["property_type", "timeline", "urgency"],
  "is_correction": true,
  "confidence": 0.95,
  "reason": "Customer replaced earlier exterior rental request with an interior hallway/kitchen project."
}}
"""
    try:
        start = time.perf_counter()
        response = client.responses.create(model=model, input=prompt)
        raw_text = response.output_text.strip()
        patch = _normalize_patch(_clean_json(raw_text))
        patch["source"] = "llm"
        patch["llm_configured"] = True
        patch["latency_ms"] = int((time.perf_counter() - start) * 1000)
        return _sanitize_patch_with_message(patch, message, current_lead, reasoner_mode)
    except Exception as exc:  # pragma: no cover - network/API dependent
        fallback["llm_configured"] = True
        fallback["llm_error"] = repr(exc)
        fallback["reason"] = f"LLM patch failed, used heuristic fallback: {exc!r}"
        return _sanitize_patch_with_message(fallback, message, current_lead, reasoner_mode)


# Backward-compatible function used by older code/tests.
def extract_info_with_llm(message: str, current_lead: LeadInfo) -> dict[str, Any]:
    patch = extract_lead_patch(message, current_lead)
    data = dict(patch.get("set", {}))
    if patch.get("is_correction"):
        data["is_correction"] = True
    return data
>>>>>>> 3895666 (Deploy AI receptionist)
