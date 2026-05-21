"""
Receptionist conversation engine for a painting business.

What this module does:
1. Maintains per-session lead state.
2. Extracts structured lead information from each caller message.
3. Uses fast deterministic rules first.
4. Falls back to the LLM only when useful.
5. Generates short phone-friendly replies.
6. Saves complete leads once.

Drop-in replacement for the original receptionist module.
It assumes the existing app.models.LeadInfo, app.estimate_rules, app.llm_extractor,
and app.database modules still exist.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from word2number import w2n

from app.database import save_lead_to_db
from app.estimate_rules import estimate_painting_duration, estimate_painting_price
from app.llm_extractor import extract_info_with_llm
from app.models import LeadInfo


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICE_CITIES = {
    "san jose",
    "berkeley",
    "fremont",
    "milpitas",
    "sunnyvale",
    "santa clara",
    "oakland",
    "palo alto",
    "mountain view",
    "cupertino",
    "redwood city",
    "san mateo",
    "foster city",
    "menlo park",
}

CITY_ALIASES = {
    "sf": "san francisco",
    "san fran": "san francisco",
    "mt view": "mountain view",
    "mtn view": "mountain view",
}

SUPPORTED_CITY_DISPLAY = (
    "San Jose, Berkeley, Fremont, Milpitas, Sunnyvale, Santa Clara, Oakland, "
    "Palo Alto, Mountain View, Cupertino, Redwood City, San Mateo, Foster City, "
    "and Menlo Park"
)

# Common speech-to-text mistakes from phone calls.
ASR_CORRECTIONS = {
    "wars only": "walls only",
    "war is only": "walls only",
    "wall's only": "walls only",
    "wall only": "walls only",
    "saleing": "ceiling",
    "sealing": "ceiling",
    "base bored": "baseboard",
    "base boards": "baseboards",
    "read wood city": "redwood city",
    "foster cities": "foster city",
    "san matteo": "san mateo",
    "pollow alto": "palo alto",
}

INTENT_PATTERNS = {
    "service_area_question": [
        "serve",
        "service area",
        "work in",
        "available in",
        "cover",
        "come to",
        "do you guys work",
        "do you work",
        "do you go to",
    ],
    "price_question": [
        "cost",
        "price",
        "how much",
        "quote",
        "estimate",
        "what would that run",
        "what would this run",
        "what would it run",
        "what would that cost",
        "what would this cost",
    ],
    "duration_question": [
        "how long",
        "duration",
        "how many days",
        "how many hours",
        "take",
        "complete",
        "finished",
        "finish",
    ],
    "repair_question": [
        "repair",
        "damage",
        "holes",
        "cracks",
        "peeling",
        "wall prep",
        "nail holes",
        "water damage",
        "patch",
        "patching",
    ],
    "booking_request": [
        "book",
        "schedule",
        "appointment",
        "call me",
        "call back",
        "have someone call",
        "talk to someone",
        "send someone",
    ],
}

ROOM_SIZE_HINTS = {
    "small bedroom": 100,
    "small room": 100,
    "normal bedroom": 140,
    "average bedroom": 140,
    "medium bedroom": 150,
    "medium room": 150,
    "large bedroom": 220,
    "large room": 220,
    "living room": 250,
}

NUMBER_WORDS = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
}

SESSION_MEMORY: dict[str, LeadInfo] = {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def get_lead(session_id: str) -> LeadInfo:
    """Return the lead state for this phone/web session."""
    if session_id not in SESSION_MEMORY:
        SESSION_MEMORY[session_id] = LeadInfo()
    return SESSION_MEMORY[session_id]


def has_word(text: str, word: str) -> bool:
    """Whole-word match so 'time' does not match 'sometime'."""
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def normalize_text(message: str) -> str:
    """Normalize punctuation, spacing, and common ASR mistakes."""
    text = (
        message.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    text = re.sub(r"\s+", " ", text).strip()

    lowered = text.lower()
    for wrong, right in ASR_CORRECTIONS.items():
        lowered = re.sub(rf"\b{re.escape(wrong)}\b", right, lowered)

    return lowered


def title_city(city: str | None) -> str | None:
    if not city:
        return None
    return " ".join(part.capitalize() for part in city.strip().split())


def get_attr(lead: LeadInfo, name: str, default: Any = None) -> Any:
    return getattr(lead, name, default)


def set_if_present(lead: LeadInfo, field: str, value: Any, *, overwrite: bool = True) -> None:
    """Assign to Pydantic/model fields without crashing on unknown optional fields."""
    if value is None:
        return
    if not hasattr(lead, field):
        return
    current = getattr(lead, field)
    if overwrite or current in (None, "", [], 0):
        setattr(lead, field, value)


def append_note(lead: LeadInfo, note: str) -> None:
    if not note:
        return
    if not hasattr(lead, "notes") or lead.notes is None:
        return
    if note not in lead.notes:
        lead.notes.append(note)


def looks_like_supported_city(city: str | None) -> bool:
    return bool(city and city.strip().lower() in SERVICE_CITIES)


def needs_area_measurement(lead: LeadInfo) -> bool:
    """Only ask for square footage when square footage makes sense."""
    service = (lead.service or "").lower()
    if not service:
        return True

    if "cabinet" in service or "exterior" in service:
        return False

    return True


def safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def quick_detect_intent(message: str, lead: LeadInfo) -> LeadInfo:
    """Fast, conservative intent detection."""
    text = normalize_text(message)

    ordered_intents = [
        "booking_request",
        "service_area_question",
        "price_question",
        "duration_question",
        "repair_question",
    ]

    for intent in ordered_intents:
        patterns = INTENT_PATTERNS[intent]
        if any(has_word(text, p) if " " not in p else p in text for p in patterns):
            lead.intent = intent
            return lead

    return lead


def extract_city(text: str, lead: LeadInfo) -> LeadInfo:
    aliased = CITY_ALIASES.get(text.strip())
    if aliased:
        lead.city = title_city(aliased)
        return lead

    for city in sorted(SERVICE_CITIES | set(CITY_ALIASES.values()), key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", text):
            lead.city = title_city(city)
            return lead

    return lead


def extract_service(text: str, lead: LeadInfo) -> LeadInfo:
    """Detect service type. Specific services must come before generic painting."""
    if any(p in text for p in ["exterior", "outside", "outside of my house", "outside my house"]):
        lead.service = "exterior painting"
    elif "cabinet" in text or "cabinets" in text:
        lead.service = "cabinet painting"
    elif any(p in text for p in ["touch-up", "touch up", "touchup"]):
        lead.service = "touch-up painting"
    elif any(p in text for p in ["interior", "inside", "bedroom", "guest room", "living room"]):
        lead.service = "interior painting"
    elif any(p in text for p in ["paint", "painting", "painter", "painted", "walls", "room"]):
        lead.service = lead.service or "interior painting"

    return lead


def extract_size(text: str, lead: LeadInfo) -> LeadInfo:
    """
    Extract square footage or dimensions.

    Handles:
    - 100 sq ft / 100 square feet
    - 10 by 12 / 10x12 / 10 x 12
    - one hundred square feet
    - ten by twelve
    - small/medium/large bedroom hints
    """

    size_match = re.search(
        r"\b(\d{2,5})\s*(square feet|square foot|square ft|sq ft|sq\. ft\.|sqft|sf)\b",
        text,
    )
    if size_match:
        lead.room_size_sqft = int(size_match.group(1))
        return lead

    if re.search(r"\b\d+\s*(feet|foot|ft)\s*(ceiling|tall|high)\b", text):
        return lead

    dimension_match = re.search(r"\b(\d{1,3})\s*(by|x)\s*(\d{1,3})\b", text)
    if dimension_match:
        width = int(dimension_match.group(1))
        length = int(dimension_match.group(3))
        area = width * length
        if 25 <= area <= 10000:
            lead.room_size_sqft = area
        return lead

    spoken_size_match = re.search(
        r"\b([a-zA-Z\s-]+?)\s*(square feet|square foot|square ft|sq ft|sqft|sf)\b",
        text,
    )
    if spoken_size_match:
        number_words = spoken_size_match.group(1).strip()
        number_words = re.sub(
            r"^(i need a|i need|i needed a|i needed|it is|it's|about|around|roughly|maybe|a|an)\s+",
            "",
            number_words,
        )
        try:
            value = int(w2n.word_to_num(number_words))
            if 25 <= value <= 10000:
                lead.room_size_sqft = value
                return lead
        except ValueError:
            pass

    spoken_dimension_match = re.search(r"\b([a-zA-Z-]+)\s+by\s+([a-zA-Z-]+)\b", text)
    if spoken_dimension_match:
        try:
            width = int(w2n.word_to_num(spoken_dimension_match.group(1)))
            length = int(w2n.word_to_num(spoken_dimension_match.group(2)))
            area = width * length
            if 25 <= area <= 10000:
                lead.room_size_sqft = area
                return lead
        except ValueError:
            pass

    for phrase, sqft in ROOM_SIZE_HINTS.items():
        if phrase in text and not lead.room_size_sqft:
            lead.room_size_sqft = sqft
            append_note(lead, f"Caller described room size as '{phrase}'; estimated as {sqft} sq ft.")
            return lead

    return lead


def extract_rooms(text: str, lead: LeadInfo) -> LeadInfo:
    room_match = re.search(r"\b(\d{1,2})\s*(room|rooms|bedroom|bedrooms)\b", text)
    if room_match:
        lead.rooms = int(room_match.group(1))
        return lead

    spoken_match = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(room|rooms|bedroom|bedrooms)\b",
        text,
    )
    if spoken_match:
        try:
            lead.rooms = int(w2n.word_to_num(spoken_match.group(1)))
            return lead
        except ValueError:
            pass

    if any(p in text for p in ["one room", "a room", "one bedroom", "a bedroom", "bedroom", "guest room"]):
        lead.rooms = lead.rooms or 1

    return lead


def extract_surfaces(text: str, lead: LeadInfo) -> LeadInfo:
    """Extract walls / ceiling / trim."""
    walls_only_patterns = [
        "walls only",
        "only the walls",
        "just the walls",
        "just walls",
    ]

    if any(pattern in text for pattern in walls_only_patterns):
        lead.walls_only = True
        lead.ceiling = False
        lead.trim = False

    if any(pattern in text for pattern in ["walls and ceiling", "walls, ceiling", "ceiling and walls"]):
        lead.walls_only = False
        lead.ceiling = True

    if any(pattern in text for pattern in ["everything", "whole room", "entire room"]):
        lead.walls_only = False
        lead.ceiling = True
        lead.trim = True

    if "no ceiling" in text:
        lead.ceiling = False
    elif "ceiling" in text:
        lead.ceiling = True
        if lead.walls_only is True:
            lead.walls_only = False

    if any(p in text for p in ["no trim", "no baseboard", "no baseboards", "without trim"]):
        lead.trim = False
    elif any(p in text for p in ["trim", "baseboard", "baseboards", "molding", "moulding"]):
        lead.trim = True
        if lead.walls_only is True:
            lead.walls_only = False

    if lead.walls_only is True:
        if lead.ceiling is None:
            lead.ceiling = False
        if lead.trim is None:
            lead.trim = False

    if lead.ceiling is True and lead.trim is None and not any(p in text for p in ["trim", "baseboard", "molding", "moulding"]):
        lead.trim = False

    return lead


def extract_timeline(text: str, lead: LeadInfo) -> LeadInfo:
    timeline_phrases = [
        "sometime next year",
        "sometime next month",
        "maybe next year",
        "maybe next month",
        "before my parents visit",
        "before parents visit",
        "before they visit",
        "before they arrive",
        "before family arrives",
        "before my guests come",
        "before guests come",
        "before move-in",
        "before move in",
        "before lease inspection",
        "before my lease inspection",
        "before the end of the month",
        "end of the month",
        "after this weekend",
        "before this weekend",
        "before next weekend",
        "after next weekend",
        "before saturday morning",
        "before saturday",
        "before friday",
        "after saturday",
        "after friday",
        "as soon as possible",
        "this weekend",
        "next weekend",
        "next month",
        "this month",
        "next year",
        "this year",
        "this week",
        "next week",
        "today",
        "tomorrow",
        "weekend",
        "asap",
        "not urgent",
        "flexible",
        "whenever",
    ]

    for phrase in timeline_phrases:
        if phrase in text:
            lead.timeline = phrase
            return lead

    return lead


def extract_phone(message: str, lead: LeadInfo) -> LeadInfo:
    phone_match = re.search(r"\b(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b", message)
    if phone_match:
        digits = re.sub(r"\D", "", phone_match.group(1))
        lead.phone = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return lead

    tokens = re.findall(r"\b[a-zA-Z0-9]+\b", normalize_text(message))
    digits: list[str] = []

    for token in tokens:
        if token.isdigit() and len(token) == 1:
            digits.append(token)
        elif token in NUMBER_WORDS:
            digits.append(NUMBER_WORDS[token])
        else:
            if len(digits) >= 10:
                break

    if len(digits) >= 10:
        phone = "".join(digits[-10:])
        lead.phone = f"{phone[:3]}-{phone[3:6]}-{phone[6:]}"
        return lead

    return lead


def extract_name(message: str, lead: LeadInfo) -> LeadInfo:
    """Extract simple caller names while avoiding false positives."""
    normalized = message.strip()

    name_patterns = [
        r"\bmy name is ([a-zA-Z][a-zA-Z .'-]{0,40})",
        r"\bi['’]?m ([a-zA-Z][a-zA-Z .'-]{0,40})",
        r"\bi am ([a-zA-Z][a-zA-Z .'-]{0,40})",
        r"\bthis is ([a-zA-Z][a-zA-Z .'-]{0,40})",
    ]

    invalid_starts = [
        "not sure",
        "looking",
        "in ",
        "trying",
        "calling",
        "interested",
        "wondering",
        "hoping",
        "thinking",
        "need",
        "want",
        "checking",
        "asking",
        "from",
        "at",
    ]

    for pattern in name_patterns:
        for name_match in re.finditer(pattern, normalized, re.IGNORECASE):
            name = name_match.group(1).strip()
            name = re.split(r"\band\b|\bmy phone\b|\bphone\b|\bnumber\b|,|[.?!]", name, maxsplit=1)[0].strip()
            name = re.sub(r"\s+", " ", name)
            lowered_name = name.lower()

            if not name:
                continue
            if any(lowered_name.startswith(phrase) for phrase in invalid_starts):
                continue
            if len(name.split()) > 3:
                continue

            lead.name = name.title()
            return lead

    return lead


def extract_notes(text: str, raw_message: str, lead: LeadInfo) -> LeadInfo:
    note_keywords = [
        "damage",
        "repair",
        "holes",
        "cracks",
        "peeling",
        "water damage",
        "nail holes",
        "wall prep",
        "patch",
        "patching",
        "mold",
        "stain",
    ]

    if any(keyword in text for keyword in note_keywords):
        append_note(lead, raw_message)

    return lead


def should_use_llm(message: str, lead: LeadInfo) -> bool:
    """
    Use LLM only when rules are unlikely to be enough.

    This keeps latency down for phone calls while preserving accuracy for messy input.
    """
    text = normalize_text(message)

    if lead.intent == "service_area_question":
        return False

    if any([lead.city, lead.service, lead.room_size_sqft, lead.phone, lead.name]) and not any(
        phrase in text for phrase in ["before my trip", "before guests arrive", "before inspection", "before move"]
    ):
        return False

    if re.search(r"\b(in|at|near)\s+[a-zA-Z ]{3,30}\b", text) and not lead.city:
        return True

    if not lead.service and any(p in text for p in ["room", "bedroom", "walls", "house", "kitchen", "bathroom"]):
        return True

    if ("before" in text or "after" in text) and not lead.timeline:
        return True

    if re.search(r"\d+\s*(by|x)\s*\d+", text) and not lead.room_size_sqft:
        return True

    vague_timeline_phrases = [
        "soon-ish",
        "before my trip",
        "before guests arrive",
        "before inspection",
        "before move",
    ]

    if any(phrase in text for phrase in vague_timeline_phrases) and not lead.timeline:
        return True

    return False


def apply_llm_extraction(lead: LeadInfo, extracted: dict[str, Any] | None) -> LeadInfo:
    if not extracted:
        return lead

    string_fields = ["name", "phone", "city", "service", "timeline", "intent"]
    for field in string_fields:
        value = extracted.get(field)
        if value not in (None, ""):
            if field == "city":
                value = title_city(str(value))
            set_if_present(lead, field, value)

    for field in ["room_size_sqft", "rooms"]:
        value = safe_int(extracted.get(field))
        if value is not None:
            set_if_present(lead, field, value)

    for field in ["walls_only", "ceiling", "trim"]:
        value = extracted.get(field)
        if isinstance(value, bool):
            set_if_present(lead, field, value)

    notes = extracted.get("notes")
    if notes:
        append_note(lead, str(notes))

    return lead


def extract_info(message: str, lead: LeadInfo) -> LeadInfo:
    raw_message = normalize_text(message)
    text = raw_message.lower()

    lead.intent = None

    lead = quick_detect_intent(raw_message, lead)
    lead = extract_city(text, lead)
    lead = extract_service(text, lead)
    lead = extract_size(text, lead)
    lead = extract_rooms(text, lead)
    lead = extract_surfaces(text, lead)
    lead = extract_timeline(text, lead)
    lead = extract_phone(message, lead)
    lead = extract_name(message, lead)
    lead = extract_notes(text, message, lead)

    if should_use_llm(message, lead):
        try:
            llm_start = time.perf_counter()
            extracted = extract_info_with_llm(message, lead)
            logger.info("LLM extraction latency: %.3fs", time.perf_counter() - llm_start)
            lead = apply_llm_extraction(lead, extracted)
        except Exception:
            logger.exception("LLM extraction failed; continuing with rule-based extraction.")

    return lead


# ---------------------------------------------------------------------------
# Conversation flow
# ---------------------------------------------------------------------------

def missing_fields(lead: LeadInfo) -> list[str]:
    missing: list[str] = []

    if not lead.city:
        missing.append("city")
    if not lead.service:
        missing.append("service")

    if needs_area_measurement(lead) and not lead.room_size_sqft:
        missing.append("room_size_sqft")

    service = (lead.service or "").lower()
    if "cabinet" in service and not lead.notes:
        missing.append("cabinet_details")
    elif "exterior" in service and not lead.notes:
        missing.append("exterior_details")

    if lead.walls_only is None and lead.ceiling is None and lead.trim is None and "interior" in service:
        missing.append("walls_ceiling_trim")

    if not lead.timeline:
        missing.append("timeline")
    if not lead.name:
        missing.append("name")
    if not lead.phone:
        missing.append("phone")

    return missing


def next_missing_question(missing: list[str]) -> str:
    """Ask one concise question at a time. Better for phone calls."""
    if "city" in missing:
        return "What city is the project in?"

    if "service" in missing:
        return "Is this for interior, exterior, cabinets, or touch-up painting?"

    if "room_size_sqft" in missing:
        return "About how large is the room in square feet?"

    if "cabinet_details" in missing:
        return "About how many cabinet doors and drawers need painting?"

    if "exterior_details" in missing:
        return "Is this the full exterior, or only part of the house like trim, siding, or doors?"

    if "walls_ceiling_trim" in missing:
        return "Is this walls only, or also the ceiling or trim?"

    if "timeline" in missing:
        return "When are you hoping to get this completed?"

    if "name" in missing:
        return "May I get your name?"

    if "phone" in missing:
        return "What is the best phone number for a call back?"

    return ""


def service_area_reply(lead: LeadInfo, missing: list[str]) -> str:
    if lead.city:
        if looks_like_supported_city(lead.city):
            follow_up = next_missing_question(missing)
            if follow_up:
                return f"Yes, we can help with painting projects in {lead.city}. {follow_up}"
            return f"Yes, we can help with painting projects in {lead.city}. I have the details and can pass this to the painter."

        return (
            f"I’m not fully sure whether {lead.city} is in the regular service area. "
            f"The usual service area is {SUPPORTED_CITY_DISPLAY}. "
            "I can still take your details and have the painter confirm."
        )

    return f"The regular service area includes {SUPPORTED_CITY_DISPLAY}. What city is your project in?"


def generate_reply(message: str, lead: LeadInfo) -> str:
    text = normalize_text(message)
    missing = missing_fields(lead)

    if lead.intent == "service_area_question":
        return service_area_reply(lead, missing)

    if lead.intent == "repair_question":
        repair_reply = (
            "Yes, minor wall prep like small cracks, nail holes, or peeling paint can usually "
            "be handled before painting. Larger damage may need an inspection first."
        )
        follow_up = next_missing_question(missing)
        return f"{repair_reply} {follow_up}" if follow_up else f"{repair_reply} I have the details and can pass this to the painter."

    if lead.intent == "price_question":
        if "room_size_sqft" in missing:
            return "I can give a rough price range. About how large is the room in square feet?"

        try:
            estimate = estimate_painting_price(lead)
        except Exception:
            logger.exception("Price estimate failed.")
            estimate = "I can collect the details and have the painter provide a final quote."

        follow_up = next_missing_question(missing)
        return f"{estimate} {follow_up}" if follow_up else estimate

    if lead.intent == "duration_question":
        if "room_size_sqft" in missing:
            return "I can give a rough time estimate. About how large is the room in square feet?"

        try:
            estimate = estimate_painting_duration(lead)
        except Exception:
            logger.exception("Duration estimate failed.")
            estimate = "Timing depends on the project size and prep work."

        follow_up = next_missing_question(missing)
        return f"{estimate} {follow_up}" if follow_up else estimate

    if lead.intent == "booking_request":
        follow_up = next_missing_question(missing)
        if follow_up:
            return f"Sure, I can help pass your request to the painter. {follow_up}"
        return "Thanks. I have your contact information and project details. I’ll send this to the painter so they can follow up."

    follow_up = next_missing_question(missing)
    if follow_up:
        if any(p in text for p in ["thank", "thanks"]):
            return f"You’re welcome. {follow_up}"
        return follow_up

    return "Thanks. I have the project details and will send them to the painter for follow-up."


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_lead_to_file(lead: LeadInfo, file_path: Path = Path("leads.json")) -> None:
    """Optional local JSON save for development/testing."""
    if not file_path.exists() or file_path.read_text().strip() == "":
        existing_data = []
    else:
        try:
            existing_data = json.loads(file_path.read_text())
        except json.JSONDecodeError:
            existing_data = []

    existing_data.append(lead.model_dump())
    file_path.write_text(json.dumps(existing_data, indent=2))


def is_ready_to_save(lead: LeadInfo) -> bool:
    return len(missing_fields(lead)) == 0


def handle_message(session_id: str, message: str) -> dict[str, Any]:
    """
    Main entry point called by your web/Twilio route.

    Returns:
    {
        "reply": str,
        "lead": LeadInfo,
        "missing_fields": list[str],
        "ready_to_send_to_painter": bool,
    }
    """
    start_time = time.perf_counter()

    lead = get_lead(session_id)

    extract_start = time.perf_counter()
    lead = extract_info(message, lead)
    extraction_latency = time.perf_counter() - extract_start

    reply_start = time.perf_counter()
    reply = generate_reply(message, lead)
    reply_latency = time.perf_counter() - reply_start

    missing = missing_fields(lead)
    ready = len(missing) == 0

    save_start = time.perf_counter()
    if ready and not get_attr(lead, "saved", False):
        try:
            lead.saved = True
            save_lead_to_db(session_id, lead)
        except Exception:
            lead.saved = False
            logger.exception("Failed to save lead.")
    save_latency = time.perf_counter() - save_start

    total_latency = time.perf_counter() - start_time

    logger.info(
        "Receptionist latency session=%s extraction=%.3fs reply=%.3fs save=%.3fs total=%.3fs ready=%s missing=%s",
        session_id,
        extraction_latency,
        reply_latency,
        save_latency,
        total_latency,
        ready,
        missing,
    )

    return {
        "reply": reply,
        "lead": lead,
        "missing_fields": missing,
        "ready_to_send_to_painter": ready,
    }


def reset_session(session_id: str) -> None:
    """Useful for tests or a 'New Customer' button."""
    SESSION_MEMORY.pop(session_id, None)