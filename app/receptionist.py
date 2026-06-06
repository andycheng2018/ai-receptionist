"""Fast, phone-friendly AI receptionist engine for a painting business.

Design:
- Layer 1: deterministic company cache for stable facts.
- Layer 2: rule extraction for common lead details.
- Layer 3: optional LLM extraction only for messy language.
"""

from __future__ import annotations

import logging
import os
import re
import time
import threading
from difflib import SequenceMatcher
from datetime import datetime
from typing import Any

from app.company_cache import get_fast_company_answer, find_city_in_message, find_service_in_message
from app.company_config import COMPANY_CONFIG, service_area_display
from app.database import save_final_call_json, save_lead_to_db
from app.estimate_rules import estimate_painting_duration, estimate_painting_price
from app.faq import find_faq_answer
from app.llm_extractor import extract_lead_patch, llm_available
from app.personality_talker import personalize_reply
from app.models import LeadInfo

try:
    from word2number import w2n
except ImportError:
    class _SimpleWordToNumber:
        _values = {
            "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
            "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
            "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
            "ninety": 90, "hundred": 100,
        }
        def word_to_num(self, words: str) -> int:
            total = 0
            current = 0
            found = False
            for word in words.lower().replace("-", " ").split():
                if word not in self._values:
                    continue
                found = True
                value = self._values[word]
                if value == 100:
                    current = max(current, 1) * 100
                else:
                    current += value
            if not found:
                raise ValueError("No number words")
            return total + current
    w2n = _SimpleWordToNumber()

logger = logging.getLogger(__name__)


def live_call_mode_default() -> bool:
    """Default to fast phone-call mode.

    In live mode, real LLM calls are not allowed to block the response.
    The system uses rules/heuristics immediately and exposes that real AI was
    deferred for post-call cleanup. Set AI_RECEPTIONIST_LIVE_MODE=false for
    slower smart-web-chat behavior.
    """
    return os.getenv("AI_RECEPTIONIST_LIVE_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}

SERVICE_CITIES = {city.lower() for city in COMPANY_CONFIG["service_areas"]}
SUPPORTED_CITY_DISPLAY = service_area_display()

CITY_ALIASES = {
    "sf": "san francisco", "san fran": "san francisco", "mt view": "mountain view",
    "mtn view": "mountain view", "red wood city": "redwood city",
}

ASR_CORRECTIONS = {
    "wars only": "walls only", "war is only": "walls only", "wall's only": "walls only",
    "wall only": "walls only", "just wars": "just walls", "saleing": "ceiling",
    "sealing": "ceiling", "ceilings": "ceiling", "base bored": "baseboard",
    "base boards": "baseboards", "read wood city": "redwood city",
    "red wood city": "redwood city", "foster cities": "foster city",
    "san matteo": "san mateo", "pollow alto": "palo alto", "square fit": "square feet",
    "square food": "square foot", "square ft": "square feet", "sq feet": "square feet",
}

HUMAN_HANDOFF_PATTERNS = [
    "talk to a person", "talk to someone", "real person", "representative", "manager",
    "human", "complaint", "angry", "upset", "not happy", "cancel", "reschedule",
]

INTENT_PATTERNS = {
    "handoff_request": HUMAN_HANDOFF_PATTERNS,
    "availability_question": ["available", "availability", "can you come", "can someone come", "do you have time", "next available"],
    "price_question": ["cost", "price", "how much", "quote", "estimate", "less than", "budget", "cheaper", "expensive", "what would that run", "what would it run"],
    "duration_question": ["how long", "duration", "how many days", "how many hours", "how much time", "how many coats", "complete", "finish", "finished"],
    "service_area_question": ["serve", "service area", "available in", "cover", "come to", "do you work", "do you serve"],
    "booking_request": ["book", "schedule", "appointment", "call me", "call back", "have someone call", "send someone"],
    "repair_question": ["do you repair", "can you repair", "can you fix", "do you fix", "drywall repair", "wall repair", "handle repairs", "repair walls", "patch holes"],
}

NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "won": "1", "two": "2", "to": "2",
    "too": "2", "three": "3", "four": "4", "for": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "ate": "8", "nine": "9",
}

VAGUE_SIZE_PHRASES = ["normal bedroom", "average bedroom", "typical bedroom", "small bedroom", "medium bedroom", "large bedroom", "small room", "medium room", "large room"]

SESSION_MEMORY: dict[str, LeadInfo] = {}
SESSION_TRANSCRIPTS: dict[str, list[dict[str, str]]] = {}
BACKGROUND_REASONER_STATUS: dict[str, dict[str, Any]] = {}
BACKGROUND_REASONER_LOCK = threading.Lock()


def get_lead(session_id: str) -> LeadInfo:
    if session_id not in SESSION_MEMORY:
        SESSION_MEMORY[session_id] = LeadInfo()
    return SESSION_MEMORY[session_id]


def reset_session(session_id: str) -> None:
    SESSION_MEMORY.pop(session_id, None)
    SESSION_TRANSCRIPTS.pop(session_id, None)


def add_transcript_message(session_id: str, speaker: str, text: str) -> None:
    SESSION_TRANSCRIPTS.setdefault(session_id, []).append({"speaker": speaker, "text": text, "timestamp": datetime.utcnow().isoformat()})


def get_transcript(session_id: str) -> list[dict[str, str]]:
    return SESSION_TRANSCRIPTS.get(session_id, [])


def last_ai_reply(session_id: str) -> str | None:
    """Return the latest assistant/receptionist message for echo detection."""
    for item in reversed(SESSION_TRANSCRIPTS.get(session_id, [])):
        if item.get("speaker") == "ai":
            return item.get("text")
    return None


def is_probable_assistant_echo(message: str, previous_ai_reply: str | None) -> bool:
    """Detect accidental mic/browser echo of the receptionist's own reply.

    This can happen in voice demos when the browser speech recognizer hears the
    speaker output, or in manual testing when the user pastes the bot reply back
    into the customer field. Echoes should not mutate lead state.
    """
    if not previous_ai_reply:
        return False
    msg = normalize_text(message)
    prev = normalize_text(previous_ai_reply)
    if not msg or not prev:
        return False

    # Exact or near-exact repeat of the last assistant prompt.
    if msg == prev:
        return True
    if SequenceMatcher(None, msg, prev).ratio() >= 0.88:
        return True

    # Common receptionist-style phrase copied with minor punctuation/extra text.
    assistant_starts = (
        "got it", "thanks", "thank you", "no problem", "sure",
        "pricing depends", "i can note", "i can't promise",
        "what's the best", "what is the best", "may i get",
    )
    if msg.startswith(assistant_starts):
        # Require meaningful overlap so we don't block real callers who say
        # something like "thanks, my number is...".
        msg_words = set(re.findall(r"[a-z0-9']+", msg))
        prev_words = set(re.findall(r"[a-z0-9']+", prev))
        overlap = len(msg_words & prev_words) / max(1, min(len(msg_words), len(prev_words)))
        if overlap >= 0.60:
            return True
    return False


def normalize_text(message: str) -> str:
    text = message.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip().lower()
    for wrong, right in ASR_CORRECTIONS.items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text


def has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE) is not None


def clean_city_candidate(city: str | None) -> str | None:
    """Clean conservative regex city captures.

    Prevents bugs like "San Mateo And" when a regex captures the
    coordinating word after a city. Also maps to known service cities when a
    known city appears as a prefix of an over-captured phrase.
    """
    if not city:
        return None
    candidate = re.sub(r"[^a-zA-Z .'-]", " ", city).strip().lower()
    candidate = re.sub(r"\s+", " ", candidate)
    # Strip common words that may be swallowed after a city.
    candidate = re.sub(
        r"\b(?:and|but|with|before|after|because|where|that|needs?|need|paint(?:ed|ing)?|project|property|rental|house|home|unit|job|place)\b.*$",
        "",
        candidate,
    ).strip()
    # Prefer configured cities, longest first. This fixes over-captures such as
    # "san mateo and" while avoiding fake city names.
    for known in sorted(SERVICE_CITIES | set(CITY_ALIASES.values()), key=len, reverse=True):
        if candidate == known or candidate.startswith(known + " "):
            candidate = known
            break
    words = candidate.split()
    if not words or len(words) > 3:
        return None
    return candidate


def title_city(city: str | None) -> str | None:
    cleaned = clean_city_candidate(city)
    return " ".join(part.capitalize() for part in cleaned.split()) if cleaned else None


def append_note(lead: LeadInfo, note: str) -> None:
    if note and note not in lead.notes:
        lead.notes.append(note)


def looks_like_supported_city(city: str | None) -> bool:
    return bool(city and city.strip().lower() in SERVICE_CITIES)


def service_needs_square_feet(lead: LeadInfo) -> bool:
    service = (lead.service or "").lower()
    # If the caller already gave a room count/scope, do not block lead capture
    # on exact square footage. A receptionist can collect contact info first.
    return bool(service and "interior" in service and not lead.rooms and not lead.project_scope)


def detect_unsafe_price_or_schedule_request(text: str) -> str | None:
    """Detect requests where the caller asks for an unrealistic price or
    exact/same-day availability promise. These must be answered safely before
    normal lead-capture acknowledgements.

    Examples:
    - "paint my entire house tonight for under $200"
    - "can you do the whole exterior tomorrow for less than 500"
    """
    low_price = re.search(
        r"\b(?:under|less than|below|for)\s*\$?\s*(\d{2,4})\b|\$\s*(\d{2,4})",
        text,
    )
    low_price_amount = None
    if low_price:
        raw_amount = low_price.group(1) or low_price.group(2)
        try:
            low_price_amount = int(raw_amount)
        except (TypeError, ValueError):
            low_price_amount = None

    same_day = any(p in text for p in ["tonight", "today", "right now", "same day", "same-day", "this evening"])
    next_day = "tomorrow" in text or "next day" in text or "next-day" in text
    big_scope = any(p in text for p in ["entire house", "whole house", "full house", "entire exterior", "whole exterior", "whole home"])
    asks_promise = any(p in text for p in ["can you", "could you", "will you", "are you able", "for under", "less than"])

    if low_price_amount is not None and low_price_amount <= 500 and (big_scope or same_day or next_day):
        return "low_price_large_or_urgent_job"
    if same_day and big_scope and asks_promise:
        return "same_day_large_job"
    return None


def looks_like_price_question(text: str) -> bool:
    """Avoid false positives like 'bubbling under one window'.

    Words such as 'under' are location words in painting/repair calls, so we
    only classify pricing when there is an actual price/cost/comparison signal.
    """
    if any(p in text for p in ["how much", "cost", "price", "quote", "estimate", "ballpark", "budget", "cheaper", "expensive", "what would that run", "what would it run"]):
        return True
    if re.search(r"\b(?:under|less than|below)\s+\$?\d+\b", text):
        return True
    if re.search(r"\$\s*\d+", text):
        return True
    return False



def looks_like_incomplete_phone(message: str) -> bool:
    """Detect when a caller tried to give a phone number but it is incomplete.

    Example: "My number is 650-555" should not be ignored. The receptionist
    should ask for the full callback number before moving on to name/timeline.
    This is deterministic and fast, but prevents a bad lead from silently losing
    contact intent.
    """
    text = normalize_text(message)
    contact_signal = any(p in text for p in [
        "my number", "phone", "call me", "callback", "reach me", "text me", "contact me"
    ])
    if not contact_signal:
        return False
    # If a complete 10-digit phone is present, this is not incomplete.
    if re.search(r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", text):
        return False
    digits = re.findall(r"\d", text)
    spoken_digit_count = sum(1 for tok in re.findall(r"\b[a-zA-Z]+\b", text) if tok in NUMBER_WORDS)
    total_digits = len(digits) + spoken_digit_count
    return 3 <= total_digits < 10

def quick_detect_intent(message: str, lead: LeadInfo) -> LeadInfo:
    text = normalize_text(message)

    if looks_like_incomplete_phone(message):
        lead.intent = "incomplete_phone"
        append_note(lead, "Caller attempted to give a phone number, but it was incomplete.")
        return lead

    unsafe_reason = detect_unsafe_price_or_schedule_request(text)
    if unsafe_reason:
        lead.intent = "unsafe_price_schedule_request"
        append_note(lead, f"Caller asked for an unrealistic price/schedule promise: {unsafe_reason}.")
        return lead

    # Price detection should not depend only on fixed keywords in INTENT_PATTERNS.
    # Examples like "under $500" or "$500" must be caught, while phrases like
    # "bubbling under one window" must not.
    if looks_like_price_question(text):
        lead.intent = "price_question"
        return lead

    # Availability requests should be safe even when phrased as exact appointment
    # questions. The reply layer will note a preference, not promise a slot.
    if re.search(r"\bcan\s+(?:someone|a\s+painter|your\s+team)\s+come\b", text) or re.search(r"\b(?:come|visit|stop by)\s+(?:this|tomorrow|next)\b", text):
        lead.intent = "availability_question"
        return lead

    for intent in ["handoff_request", "availability_question", "duration_question", "service_area_question", "booking_request", "repair_question"]:
        for pattern in INTENT_PATTERNS[intent]:
            if (" " in pattern and pattern in text) or (" " not in pattern and has_word(text, pattern)):
                # Avoid false positives like "touch-up work in the Daly City rental".
                # Service-area intent should only fire when the user is actually asking
                # whether the company serves/comes to/covers an area.
                if intent == "price_question" and not looks_like_price_question(text):
                    continue
                if intent == "service_area_question":
                    service_area_question_like = (
                        "?" in message
                        or any(q in text for q in ["do you", "can you", "are you", "service area", "serve", "available in", "cover", "come to"])
                    )
                    if not service_area_question_like:
                        continue
                lead.intent = intent
                if intent == "handoff_request":
                    lead.handoff_required = True
                return lead
    return lead



def message_has_project_correction(text: str) -> bool:
    """Detect when the caller is correcting earlier project facts.

    This is important because lead fields are long-lived in a session. If a
    caller says "actually, not outside, inside...", stale exterior scope,
    city, trim, and urgency should not keep polluting the corrected lead.
    """
    correction_words = ["actually", "sorry", "correction", "not the", "not outside", "not exterior", "instead", "i mean"]
    return any(p in text for p in correction_words)


def correction_target_service(text: str) -> str | None:
    """Infer the corrected service from the latest message only.

    Negative phrases matter. For example, "exterior siding, not bedrooms"
    must become exterior, even though the word "bedrooms" appears. Likewise,
    "not outside, inside hallway" must become interior.
    """
    negative_exterior = any(p in text for p in ["not the outside", "not outside", "not exterior", "not the exterior"])
    negative_interior = any(p in text for p in ["not the inside", "not inside", "not interior", "not the interior", "not bedrooms", "not bedroom"])

    positive_exterior = any(p in text for p in ["outside", "exterior", "siding", "front exterior", "front of the house"])
    positive_interior = any(p in text for p in ["inside", "interior", "bedroom", "bedrooms", "hallway", "hall", "living room", "walls", "room"])

    if "cabinet" in text:
        return "cabinet painting"
    if negative_exterior and positive_interior:
        return "interior painting"
    if negative_interior and positive_exterior:
        return "exterior painting"
    if positive_exterior and not negative_exterior:
        return "exterior painting"
    if positive_interior and not negative_interior:
        return "interior painting"
    return None


def clear_project_fields_for_correction(text: str, lead: LeadInfo) -> LeadInfo:
    """Clear stale project facts when caller corrects the job type/location.

    Contact info is preserved. Project facts are cleared only when the latest
    message clearly corrects a prior project and points to a new service type.
    """
    if not message_has_project_correction(text):
        return lead

    new_service = correction_target_service(text)
    old_service = (lead.service or "").lower()
    service_switch = bool(new_service and new_service.lower() not in old_service)

    if not service_switch:
        return lead

    # Keep contact fields, but reset stale project-specific values.
    lead.service = None
    # Preserve city unless the correction message provides a new one.
    # Example: "Actually it’s exterior siding, not bedrooms" should keep San Bruno.
    # Later extract_city() will overwrite city when the correction says "in Daly City".
    lead.address = None
    lead.room_size_sqft = None
    lead.rooms = None
    lead.walls_only = None
    lead.ceiling = None
    lead.trim = None
    lead.stories = None
    lead.occupied = None
    lead.repairs_needed = None
    lead.photos_available = None
    lead.cabinet_count = None
    lead.project_scope = None
    lead.timeline = None
    lead.urgency = None
    lead.notes = []
    return lead

def extract_city(text: str, lead: LeadInfo) -> LeadInfo:
    # Prefer the project/property city over the caller's home city.
    # Example: "I live in San Jose, but the house that needs painting is in San Mateo."
    project_city_match = re.search(
        r"\b(?:project|property|house|home|rental|condo|apartment|office|job|unit)\b[^.?!,;]{0,80}\b(?:is|needs painting|that needs painting)?\s*(?:in|at)\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+alto|[a-z]+\s+mateo|[a-z]+\s+bruno)\b",
        text,
    )
    if project_city_match:
        candidate = re.sub(r"\b(needs?|that|with|before|after)\b.*$", "", project_city_match.group(1).strip()).strip()
        lead.city = title_city(candidate)
        return lead

    cached_city = find_city_in_message(text)
    if cached_city:
        lead.city = cached_city
        return lead
    for alias, canonical in CITY_ALIASES.items():
        if has_phrase(text, alias):
            lead.city = title_city(canonical)
            return lead
    for city in sorted(SERVICE_CITIES | set(CITY_ALIASES.values()), key=len, reverse=True):
        if has_phrase(text, city):
            lead.city = title_city(city)
            return lead

    # Fallback for cities not yet listed in company_config, e.g. "in Daly City".
    # This keeps extraction useful; service-area validation can still happen later.
    city_match = re.search(
        r"\b(?:project|property|unit|house|home|rental|condo|apartment|office|it|this|job)\s+(?:is\s+)?(?:in|at)\s+([a-z][a-z .'-]{2,40}?)(?:\.|,|$|\s+before|\s+and|\s+with)",
        text,
    )
    if city_match:
        candidate = city_match.group(1).strip()
        if 1 <= len(candidate.split()) <= 3 and not any(w in candidate for w in ["the ", "a ", "an "]):
            lead.city = title_city(candidate)
            return lead
    simple_in_city = re.search(r"\bin\s+([a-z]+(?:\s+[a-z]+)?\s+city)\b", text)
    if simple_in_city:
        lead.city = title_city(simple_in_city.group(1).strip())
        return lead

    # Generic Bay Area-style city extraction, including unsupported cities such as
    # "San Bruno". Keep this conservative so phrases like "in a rental" do not
    # become fake cities.
    generic_city = re.search(
        r"\bin\s+((?:san|santa|south|east|west|north)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city|[a-z]+\s+park|[a-z]+\s+alto|[a-z]+\s+mateo|[a-z]+\s+bruno)\b",
        text,
    )
    if generic_city:
        candidate = generic_city.group(1).strip()
        bad_candidates = {"a rental", "the house", "the home", "the room", "the hallway"}
        if candidate not in bad_candidates:
            lead.city = title_city(candidate)
            return lead
    return lead


def extract_service(text: str, lead: LeadInfo) -> LeadInfo:
    corrected_service = correction_target_service(text) if message_has_project_correction(text) else None
    if corrected_service:
        lead.service = corrected_service
        return lead

    # Mixed inside/outside projects are common in real calls. Do not collapse
    # them to only "exterior painting" just because "outside" appears first.
    has_outside = any(p in text for p in ["outside", "exterior", "front door", "siding", "garage door"])
    has_inside = any(p in text for p in ["inside", "interior", "hallway", "living room", "bedroom", "kitchen", "walls inside", "touch-ups inside", "touch up inside"])
    has_touchup = any(p in text for p in ["touch up", "touch-up", "touchup", "scuffs", "small area"])
    if has_outside and has_inside and has_touchup:
        if "front door" in text:
            lead.service = "exterior door painting + interior touch-up"
        else:
            lead.service = "exterior painting + interior touch-up"
        return lead

    if "cabinet" in text and any(p in text for p in ["touch up", "touch-up", "walls", "living room"]):
        lead.service = "cabinet painting + interior touch-up"
        return lead
    if any(p in text for p in ["exterior", "outside", "siding", "front of the house", "front looks", "front exterior", "outside front"]):
        lead.service = "exterior painting"
        return lead
    if any(p in text for p in ["drywall", "leak", "water damage", "water staining", "water stain", "stains", "stain", "peeling", "bubbling", "repair and paint", "repaired and painted"]):
        if "cabinet" in text:
            lead.service = "drywall repair + painting + cabinet painting"
        else:
            lead.service = "paint repair / repainting"
        return lead
    if any(p in text for p in ["exterior", "outside", "siding", "front of the house", "front looks"]):
        lead.service = "exterior painting"
        return lead

    cached_service = find_service_in_message(text)
    if cached_service:
        lead.service = cached_service
        return lead
    if any(p in text for p in ["exterior", "outside", "siding", "front of the house", "front looks"]):
        lead.service = "exterior painting"
    elif "cabinet" in text:
        lead.service = "cabinet painting"
    elif any(p in text for p in ["touch-up", "touch up", "touchup"]):
        lead.service = "touch-up painting"
    elif any(p in text for p in ["interior", "inside", "bedroom", "guest room", "living room", "walls", "room"]):
        lead.service = "interior painting"
    elif any(p in text for p in ["paint", "painting", "painter", "painted"]):
        lead.service = lead.service or "painting project"
    return lead


def extract_size(text: str, lead: LeadInfo) -> LeadInfo:
    size_match = re.search(r"\b(\d{2,5})\s*(square feet|square foot|square ft|sq ft|sq\. ft\.|sqft|sf)\b", text)
    if size_match:
        lead.room_size_sqft = int(size_match.group(1))
        return lead
    dimension_match = re.search(r"\b(\d{1,3})\s*(by|x)\s*(\d{1,3})\b", text)
    if dimension_match:
        area = int(dimension_match.group(1)) * int(dimension_match.group(3))
        if 25 <= area <= 10000:
            lead.room_size_sqft = area
        return lead
    spoken_size_match = re.search(r"\b([a-zA-Z\s-]+?)\s*(square feet|square foot|square ft|sq ft|sqft|sf)\b", text)
    if spoken_size_match:
        number_words = re.sub(r"^(about|around|roughly|maybe|a|an|it is|it's)\s+", "", spoken_size_match.group(1).strip())
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
            area = int(w2n.word_to_num(spoken_dimension_match.group(1))) * int(w2n.word_to_num(spoken_dimension_match.group(2)))
            if 25 <= area <= 10000:
                lead.room_size_sqft = area
                return lead
        except ValueError:
            pass
    for phrase in VAGUE_SIZE_PHRASES:
        if phrase in text:
            append_note(lead, f"Caller described size as '{phrase}'. Exact square footage still needed.")
            break
    return lead


def extract_rooms(text: str, lead: LeadInfo) -> LeadInfo:
    if any(p in text for p in ["not bedroom", "not bedrooms", "not the bedrooms", "not rooms", "not the rooms"]):
        return lead

    room_match = re.search(r"\b(\d{1,2})\s*(room|rooms|bedroom|bedrooms)\b", text)
    if room_match:
        lead.rooms = int(room_match.group(1))
        return lead
    spoken_match = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(room|rooms|bedroom|bedrooms)\b", text)
    if spoken_match:
        try:
            lead.rooms = int(w2n.word_to_num(spoken_match.group(1)))
            return lead
        except ValueError:
            pass
    if any(p in text for p in ["one room", "a room", "one bedroom", "a bedroom", "bedroom", "guest room"]):
        lead.rooms = lead.rooms or 1
    return lead




def parse_small_number(text: str) -> int | None:
    """Parse short follow-up answers like "two" or "2".

    This is intentionally conservative so a normal sentence does not get
    accidentally converted into a number. It is used with conversation context
    from the previous missing question.
    """
    cleaned = normalize_text(text).strip()
    cleaned = re.sub(r"[^a-z0-9\s-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if re.fullmatch(r"\d{1,2}", cleaned):
        return int(cleaned)
    # Allow short forms such as "two", "two stories", "2 story".
    cleaned = re.sub(r"\b(story|stories|storey|storeys|rooms|room|bedrooms|bedroom|doors|drawers)\b", "", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned.split()) > 3:
        return None
    try:
        value = int(w2n.word_to_num(cleaned))
        return value
    except Exception:
        return None


def extract_contextual_followup_answer(message: str, before: LeadInfo, lead: LeadInfo) -> LeadInfo:
    """Interpret very short replies using the question the bot just asked.

    Example: if the bot asks "How many stories is the property?" and the caller
    says "two", rules alone cannot know whether that means stories, bedrooms,
    or something else. The previous missing field gives us the context.
    """
    text = normalize_text(message)
    before_missing = missing_fields(before)
    if not before_missing:
        return lead
    expected = next_question_field(before_missing)
    value = parse_small_number(message)

    if expected == "stories" and value is not None and 1 <= value <= 5:
        lead.stories = value
        return lead

    if expected == "cabinet_details" and value is not None and 1 <= value <= 200:
        lead.cabinet_count = value
        return lead

    if expected == "room_size_sqft" and value is not None and 25 <= value <= 10000:
        lead.room_size_sqft = value
        return lead

    if expected == "exterior_scope":
        parts: list[str] = []
        if any(p in text for p in ["full", "whole", "entire", "all exterior", "whole house"]):
            lead.project_scope = "full exterior"
            return lead
        if any(p in text for p in ["front", "front only"]):
            parts.append("front exterior")
        if "siding" in text:
            parts.append("siding")
        if "trim" in text:
            lead.trim = True
            parts.append("trim")
        if "garage" in text:
            parts.append("garage door")
        if "back" in text:
            parts.append("back exterior")
        if "side" in text:
            parts.append("side exterior")
        if parts:
            lead.project_scope = " and ".join(dict.fromkeys(parts))
            return lead
        if any(p in text for p in ["one area", "just one area", "partial"]):
            lead.project_scope = "one exterior area"
            return lead

    return lead


def extract_surfaces(text: str, lead: LeadInfo) -> LeadInfo:
    # Do not treat comparison phrases as actual scope.
    # Example: "is fixing bubbling paint and one wall cheaper than repainting the whole room"
    # mentions "whole room" only as a pricing comparison, not as the requested scope.
    comparison_only = any(p in text for p in [
        "cheaper than repainting", "cheaper than painting", "instead of repainting",
        "instead of painting", "rather than repainting", "rather than painting",
    ])
    if any(p in text for p in ["walls only", "only the walls", "just the walls", "just walls"]):
        lead.walls_only, lead.ceiling, lead.trim = True, False, False
    if any(p in text for p in ["walls and ceiling", "walls, ceiling", "ceiling and walls"]):
        lead.walls_only, lead.ceiling = False, True
    if not comparison_only and any(p in text for p in ["everything", "whole room", "entire room"]):
        lead.walls_only, lead.ceiling, lead.trim = False, True, True
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
        lead.ceiling = False if lead.ceiling is None else lead.ceiling
        lead.trim = False if lead.trim is None else lead.trim
    return lead


def extract_timeline_and_callback(text: str, lead: LeadInfo) -> LeadInfo:
    # Callback timing: "call me tomorrow afternoon".
    callback_match = re.search(
        r"\b(?:call|callback|call me|reach me)\s+(?:around|at|after|before)?\s*"
        r"([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?|tomorrow morning|tomorrow afternoon|tomorrow evening|morning|afternoon|evening|tonight)\b",
        text,
    )
    if callback_match:
        lead.preferred_callback_time = callback_match.group(1).strip()
    elif "morning is best" in text:
        lead.preferred_callback_time = "morning"
    elif "afternoon is best" in text:
        lead.preferred_callback_time = "afternoon"
    elif "evening is best" in text:
        lead.preferred_callback_time = "evening"

    schedule_pref = re.search(r"\b(?:only\s+)?(?:after\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)|over the weekend|on the weekend|weekend|after hours|after-hours)\b(?:\s+or\s+(?:over the weekend|on the weekend|weekend|after\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)))?", text)
    if schedule_pref and not lead.preferred_callback_time:
        lead.preferred_callback_time = schedule_pref.group(0).strip().replace("only ", "")

    # Appointment/availability requests: do not promise this time, but capture it
    # as a preferred appointment/callback time for the painter to confirm.
    appointment_match = re.search(
        r"\b(?:come|visit|stop by|take a look|send someone)\s+"
        r"((?:today|tomorrow|this|next)\s+(?:morning|afternoon|evening|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+[0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?)?|"
        r"(?:morning|afternoon|evening)\s+at\s+[0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?|"
        r"[0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm))\b",
        text,
    )
    if appointment_match and not lead.preferred_callback_time:
        lead.preferred_callback_time = appointment_match.group(1).strip()

    # If the user is only giving callback timing, do not overwrite project timeline.
    project_timeline_context = any(p in text for p in [
        "need it", "completed", "complete", "done", "finished", "finish", "paint",
        "project", "job", "tenants", "move-in", "move in", "before guests",
        "open house", "listing photos", "realtor",
    ])
    callback_only = bool(callback_match) and not project_timeline_context
    if callback_only:
        return lead

    urgent_phrases = [
        "as soon as possible", "asap", "urgent", "today", "tomorrow", "tonight",
        "before move-in", "before move in", "tenants are moving in", "moving in soon",
        "tenants move in", "tenant move in", "open house", "listing photos",
        "before my open house", "before the open house", "before photos", "before listing photos",
        "before next showing", "before the next showing", "next showing",
    ]
    soon_phrases = [
        "before next weekend", "this week", "next week", "this weekend", "next weekend",
        "end of the month", "before the end of the month", "next friday", "before next friday",
        "before monday", "before tuesday", "before wednesday", "before thursday",
        "before friday", "before saturday", "before sunday",
    ]
    flexible_phrases = ["not urgent", "flexible", "whenever", "sometime", "next month"]

    deadline_match = re.search(
        r"\b(before\s+(?:my\s+|the\s+)?(?:open house|listing photos|photos)|"
        r"before\s+next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"before\s+(?:the\s+)?next\s+showing|"
        r"next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
        text,
    )
    if deadline_match:
        lead.timeline = deadline_match.group(1)
    else:
        timeline_phrases = urgent_phrases + soon_phrases + flexible_phrases + ["after friday", "after saturday", "next year", "this month"]
        for phrase in timeline_phrases:
            if phrase in text:
                lead.timeline = phrase
                break
    if any(p in text for p in urgent_phrases) or (lead.timeline and (lead.timeline.startswith("before next") or "open house" in lead.timeline or "photos" in lead.timeline)):
        lead.urgency = "urgent"
    elif any(p in text for p in soon_phrases):
        lead.urgency = "soon"
    elif any(p in text for p in flexible_phrases):
        lead.urgency = "flexible"
    return lead

def extract_phone(message: str, lead: LeadInfo) -> LeadInfo:
    normalized = normalize_text(message)
    phone_match = re.search(r"\b(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b", normalized)
    if phone_match:
        digits = re.sub(r"\D", "", phone_match.group(1))
        if len(digits) == 10:
            lead.phone = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
            return lead
    tokens = re.findall(r"\b[a-zA-Z0-9]+\b", normalized)
    digits: list[str] = []
    for token in tokens:
        if token.isdigit():
            digits.extend(list(token))
        elif token in NUMBER_WORDS:
            digits.append(NUMBER_WORDS[token])
        elif len(digits) >= 10:
            break
    if len(digits) >= 10:
        phone = "".join(digits[-10:])
        lead.phone = f"{phone[:3]}-{phone[3:6]}-{phone[6:]}"
    return lead


NON_NAME_WORDS = {
    "paint", "painting", "painter", "project", "job", "rental", "property",
    "house", "home", "place", "wall", "walls", "cabinet", "cabinets",
    "repair", "repairs", "interior", "exterior", "bedroom", "bedrooms",
    "kitchen", "hallway", "window", "bubbling", "drywall", "service", "quote",
}


def looks_like_person_name(candidate: str) -> bool:
    candidate = re.sub(r"\s+", " ", candidate.strip())
    if not candidate or len(candidate.split()) > 3:
        return False
    lowered = candidate.lower()
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


def extract_name(message: str, lead: LeadInfo) -> LeadInfo:
    normalized = message.replace("’", "'").replace("‘", "'").strip()
    for pattern in [r"\bmy name is ([a-zA-Z][a-zA-Z .'-]{0,40})", r"\bi['’]?m ([a-zA-Z][a-zA-Z .'-]{0,40})", r"\bi am ([a-zA-Z][a-zA-Z .'-]{0,40})", r"\bthis is ([a-zA-Z][a-zA-Z .'-]{0,40})"]:
        for match in re.finditer(pattern, normalized, re.IGNORECASE):
            name = re.split(r"\band\b|\bmy phone\b|\bphone\b|\bnumber\b|,|[.?!]", match.group(1).strip(), maxsplit=1)[0].strip()
            name = re.sub(r"\s+", " ", name)
            if looks_like_person_name(name):
                lead.name = name.title()
                return lead
    return lead




def extract_bare_name(message: str, before: LeadInfo, lead: LeadInfo) -> LeadInfo:
    """Accept short name-only replies like "Andy" or "Andy Chen".

    The browser used to reject messages shorter than five characters, and the
    backend only accepted phrases like "my name is". On a real phone call, if
    the receptionist asks for a name, callers usually just answer with the name.
    """
    if lead.name:
        return lead
    raw = message.strip()
    cleaned = re.sub(r"[^A-Za-z .'-]", "", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or len(cleaned.split()) > 3:
        return lead
    lower = cleaned.lower()
    blocked = {
        "interior", "exterior", "painting", "paint", "painter", "yes", "no",
        "maybe", "soon", "today", "tomorrow", "thanks", "thank you", "hi", "hello",
        "one story", "two story", "two stories", "three story", "three stories",
    }
    if lower in blocked or lower in SERVICE_CITIES or lower in CITY_ALIASES:
        return lead
    if lower in NUMBER_WORDS or re.fullmatch(r"\d{1,3}", lower):
        return lead
    if any(word in lower.split() for word in ["bedroom", "bedrooms", "room", "rooms", "story", "stories", "city"]):
        return lead
    if not looks_like_person_name(cleaned):
        return lead

    before_missing = missing_fields(before)
    # Do not turn short answers like "two" or "front" into a name when the
    # previous question was asking for project details.
    expected_field = next_question_field(before_missing) if before_missing else None
    if expected_field not in {None, "name", "phone"}:
        return lead
    likely_name_turn = expected_field == "name"
    has_some_lead_context = bool(before.city or before.service or before.timeline or before.phone)
    if likely_name_turn or has_some_lead_context:
        lead.name = cleaned.title()
    return lead


def normalize_spoken_email(message: str) -> str:
    """Convert simple spoken email forms like 'andy dot chen at gmail dot com'."""
    text = message.lower().strip()
    text = re.sub(r"^.*?\b(?:email|e-mail)\s+(?:is|as)?\s*", "", text)
    text = re.sub(r"^.*?\b(?:contact me at|send it to)\s+", "", text)
    text = re.sub(r"\b(at sign|at)\b", "@", text)
    text = re.sub(r"\b(dot|period)\b", ".", text)
    text = re.sub(r"\s*@\s*", "@", text)
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"\s+", "", text)
    return text


def extract_email(message: str, lead: LeadInfo) -> LeadInfo:
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", message)
    if email_match:
        lead.email = email_match.group(0).lower()
        return lead
    spoken = normalize_spoken_email(message)
    spoken_match = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", spoken)
    if spoken_match:
        lead.email = spoken_match.group(0).lower()
    return lead


def extract_address(message: str, lead: LeadInfo) -> LeadInfo:
    address_match = re.search(r"\b(\d{1,6}\s+[A-Za-z0-9 .'-]{2,60}\s+(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|court|ct|way|place|pl|blvd|boulevard))\b", message, re.IGNORECASE)
    if address_match:
        lead.address = re.sub(r"\s+", " ", address_match.group(1)).strip().title()
    return lead


def extract_property_details(text: str, lead: LeadInfo) -> LeadInfo:
    if any(p in text for p in ["single family", "single-family", "house", "home"]):
        lead.property_type = "house"
    elif "condo" in text:
        lead.property_type = "condo"
    elif "apartment" in text:
        lead.property_type = "apartment"
    elif any(p in text for p in ["dental office", "office", "commercial", "storefront", "business"]):
        lead.property_type = "commercial office" if "office" in text else "business"
    elif "rental" in text or "landlord" in text or "tenant" in text or "unit" in text:
        lead.property_type = "rental"

    story_match = re.search(r"\b(\d+)\s*(story|stories|storey|storeys)\b", text)
    if story_match:
        lead.stories = int(story_match.group(1))
    elif any(p in text for p in ["one story", "one-story", "single story"]):
        lead.stories = 1
    elif any(p in text for p in ["two story", "two stories", "two-story", "2 story", "2 stories"]):
        lead.stories = 2
    elif any(p in text for p in ["three story", "three stories", "three-story", "3 story", "3 stories"]):
        lead.stories = 3

    # Scope extraction should preserve multiple areas/items when stated together,
    # including mixed interior + exterior jobs.
    areas: list[str] = []
    for area in ["hallway", "kitchen", "living room", "dining room", "bathroom", "waiting room", "conference room", "lobby", "office"]:
        if area in text and area not in areas:
            areas.append(area)
    if "bedrooms" in text or "bedroom" in text:
        areas.append("bedrooms" if "bedrooms" in text else "bedroom")

    exterior_parts: list[str] = []
    if "front door" in text:
        exterior_parts.append("front door")
    if any(p in text for p in ["siding", "exterior siding"]):
        exterior_parts.append("siding")
    if any(p in text for p in ["exterior trim", "outside trim", "trim"]):
        lead.trim = True
        exterior_parts.append("exterior trim" if "exterior" in text or "outside" in text else "trim")
    if any(p in text for p in ["garage door", "garage doors"]):
        exterior_parts.append("garage door")

    mixed_service = lead.service and "+" in lead.service
    if mixed_service and (exterior_parts or areas):
        combined = exterior_parts + [f"{a} touch-ups" if "touch" in text and a == "hallway" else a for a in areas]
        lead.project_scope = " and ".join(dict.fromkeys(combined))
    elif any(p in text for p in ["front only", "front of my", "front of the", "front looks", "front exterior", "outside front"]):
        # If a specific front-door item is stated, keep that specific item rather
        # than broadening it to the whole front exterior.
        lead.project_scope = "front door" if "front door" in text else "front exterior only"
    elif len(exterior_parts) >= 2:
        lead.project_scope = " and ".join(dict.fromkeys(exterior_parts))
    elif exterior_parts:
        lead.project_scope = exterior_parts[0]
    elif any(p in text for p in ["whole house", "full exterior", "entire exterior"]):
        lead.project_scope = "full exterior"
    elif any(p in text for p in ["whole interior", "entire interior"]):
        lead.project_scope = "whole interior"
    elif len(areas) >= 2:
        lead.project_scope = " and ".join(dict.fromkeys(areas))
    elif areas:
        lead.project_scope = areas[0]
    elif any(p in text for p in ["touch up", "touch-up", "small area"]):
        lead.project_scope = "touch-up/small area"

    repair_scope_parts: list[str] = []
    if "ceiling" in text and any(p in text for p in ["stain", "stains", "water"]):
        repair_scope_parts.append("ceiling stains")
    if "bathroom" in text and "peeling" in text:
        repair_scope_parts.append("bathroom peeling paint")
    if "bubbling" in text and "window" in text:
        if "under" in text:
            repair_scope_parts.append("bubbling under window")
        else:
            repair_scope_parts.append("bubbling near window")
    elif "bubbling" in text and "wall" in text:
        repair_scope_parts.append("bubbling paint and one wall" if "one wall" in text else "bubbling paint")
    elif "one wall" in text:
        repair_scope_parts.append("one wall")
    if repair_scope_parts:
        lead.project_scope = " and ".join(dict.fromkeys(repair_scope_parts))

    if any(p in text for p in ["occupied", "living there", "tenant is there", "people live there"]):
        lead.occupied = True
    elif any(p in text for p in ["vacant", "empty", "no one lives there"]):
        lead.occupied = False
    if any(p in text for p in ["repair", "repairs", "patch", "holes", "cracks", "peeling", "damage", "drywall", "rough", "bubbling", "stain", "stains"]):
        lead.repairs_needed = True
    elif any(p in text for p in ["no repair", "no repairs", "no damage"]):
        lead.repairs_needed = False
    if any(p in text for p in ["i have photos", "have photos", "can send photos", "pictures", "photos"]):
        lead.photos_available = True
    elif any(p in text for p in ["no photos", "don't have photos", "do not have photos"]):
        lead.photos_available = False
    cabinet_match = re.search(r"\b(\d{1,3})\s*(cabinet doors|doors|drawers|cabinets)\b", text)
    if cabinet_match and "cabinet" in (lead.service or ""):
        lead.cabinet_count = int(cabinet_match.group(1))
    return lead


def extract_notes(text: str, raw_message: str, lead: LeadInfo) -> LeadInfo:
    if any(k in text for k in ["damage", "repair", "holes", "cracks", "peeling", "water damage", "nail holes", "wall prep", "patch", "mold", "stain", "rough", "tenant"]):
        append_note(lead, raw_message)
    return lead


def field_changed(before: LeadInfo, after: LeadInfo) -> bool:
    """Whether rule extraction changed any meaningful lead field."""
    ignore = {"lead_score", "lead_priority", "saved", "notes"}
    for field in before.model_fields:
        if field in ignore:
            continue
        if getattr(before, field) != getattr(after, field):
            return True
    return False


def looks_like_simple_rule_turn(text: str) -> bool:
    """Tiny answers should stay fast unless prior context expects AI.

    Examples: "San Mateo", "Andy", "650-555-1234", "yes", "two".
    These are best handled by deterministic context-followup rules.
    """
    cleaned = normalize_text(text)
    if re.fullmatch(r"(?:yes|no|yeah|yep|nope|thanks|thank you)", cleaned):
        return True
    if re.fullmatch(r"[a-z]+(?:\s+[a-z]+){0,2}", cleaned) and len(cleaned.split()) <= 3:
        return True
    if re.fullmatch(r"[0-9() .+-]{2,20}", cleaned):
        return True
    return False



def explain_ai_patch_route(message: str, before: LeadInfo, after: LeadInfo) -> tuple[bool, str]:
    """Return whether this turn should use the slower Reasoner and why.

    This mirrors should_use_ai_patch but also exposes a human-readable trigger
    for UI/debugging. Keep this lightweight; it runs every turn.
    """
    text = normalize_text(message)
    token_count = len(text.split())

    if token_count <= 3 and looks_like_simple_rule_turn(text) and not message_has_project_correction(text):
        return False, "simple_short_rule_turn"

    correction_signals = [
        "actually", "ignore that", "correction", "i meant", "i mean", "instead", "not outside",
        "not exterior", "not inside", "not interior", "not bedrooms", "not bedroom", "sorry",
        "scratch that", "change that", "rather", "instead of",
    ]
    mixed_scope_signals = [
        "hallway and kitchen", "living room and hallway", "cabinets and", "plus maybe",
        "but also", "also", "both", "as well", "i live in", "but the property", "but the project",
        "rental that needs", "property that needs", "not sure if", "do you do both",
        "inside and outside", "outside and inside", "inside", "outside",
    ]
    messy_signals = [
        "not sure", "maybe", "kind of", "sort of", "rental", "tenant", "moving in",
        "looks rough", "worn down", "peeling", "fix up", "cleaned up", "realtor",
        "open house", "listing photos", "drywall damage", "water damage", "bubbling",
        "before", "deadline", "hoa", "selling", "move in",
    ]
    complex_question_signals = [
        "how much", "how long", "can someone come", "can you come", "what if",
        "do you also", "can you also", "would you be able", "need advice", "cheaper than",
    ]

    llm_required_signals = (
        token_count >= 18
        and ("not sure" in text or "or both" in text or "explained that badly" in text)
        and any(signal in text for signal in ["repair", "repair job", "bubbling", "water", "drywall", "stain"])
        and any(signal in text for signal in ["cabinet", "cabinets", "touch up", "painted", "painting"])
    )
    mixed_inside_outside = (
        not any(neg in text for neg in ["not outside", "not the outside", "not exterior", "not the exterior"])
        and not any(neg in text for neg in ["not inside", "not interior", "not bedrooms", "not bedroom"])
        and any(signal in text for signal in ["outside", "exterior", "front door", "siding", "garage door"])
        and any(signal in text for signal in ["inside", "interior", "hallway", "living room", "bedroom", "kitchen"])
        and any(signal in text for signal in ["also", "but also", "both", "plus", "and"])
    )

    if llm_required_signals:
        return True, "llm_required_complex_semantic_turn"
    if looks_like_price_question(text) and token_count >= 5:
        return True, "price_or_budget_signal"
    if mixed_inside_outside:
        return True, "mixed_inside_outside_scope_signal"
    if any(signal in text for signal in correction_signals):
        return True, "correction_signal"
    if any(signal in text for signal in mixed_scope_signals):
        return True, "mixed_scope_or_project_city_signal"
    if "?" in message and token_count >= 8 and any(signal in text for signal in complex_question_signals):
        return True, "complex_question_signal"
    if token_count >= 10 and any(p in text for p in ["after 6", "after-hours", "after hours", "over the weekend", "patients", "during the day"]):
        return True, "schedule_constraint_signal"
    if token_count >= 8 and len(re.findall(r"\b(?:san|santa)\s+[a-z]+(?:\s+[a-z]+)?|[a-z]+\s+city\b", text)) >= 2:
        return True, "multiple_city_disambiguation_signal"

    service = (after.service or "").lower()
    scope = (after.project_scope or "").lower()
    if "interior" in service and any(x in scope for x in ["exterior", "siding", "garage door"]):
        return True, "suspicious_interior_exterior_state"
    if "exterior" in service and after.rooms:
        return True, "suspicious_exterior_rooms_state"

    if token_count >= 4 and not field_changed(before, after) and not get_fast_company_answer(message):
        return True, "no_rule_extraction_from_nontrivial_turn"

    if token_count >= 9 and any(signal in text for signal in messy_signals):
        return True, "descriptive_messy_turn_enrichment"

    return False, "rules_sufficient"

def should_use_ai_patch(message: str, before: LeadInfo, after: LeadInfo) -> bool:
    """Compatibility wrapper for old tests/callers."""
    should_route, _reason = explain_ai_patch_route(message, before, after)
    return should_route

def apply_lead_patch(lead: LeadInfo, patch: dict[str, Any]) -> LeadInfo:
    """Apply structured set/clear patch from AI extractor.

    Clearing happens before setting, so a correction can safely erase stale
    fields and then set the corrected scope. Contact fields are protected.
    This function also guards the LeadInfo schema because LLMs sometimes return
    descriptive strings for boolean fields like repairs_needed.
    """
    if not isinstance(patch, dict):
        return lead

    protected = {"name", "phone", "email"}
    for field in patch.get("clear", []) or []:
        if field in protected or not hasattr(lead, field):
            continue
        current = getattr(lead, field)
        if isinstance(current, list):
            setattr(lead, field, [])
        else:
            setattr(lead, field, None)

    def boolish(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"true", "yes", "y", "needed", "repair needed"}:
                return True
            if low in {"false", "no", "n", "none", "not needed"}:
                return False
            if any(w in low for w in ["repair", "damage", "stain", "bubbling", "leak", "water", "patch", "crack"]):
                return True
        return None

    set_values = patch.get("set", {}) or {}
    if isinstance(set_values, dict):
        for field, value in set_values.items():
            if not hasattr(lead, field) or value in (None, "", []):
                continue
            if field in {"repairs_needed", "photos_available", "walls_only", "ceiling", "trim", "occupied", "handoff_required"}:
                parsed = boolish(value)
                if parsed is not None:
                    setattr(lead, field, parsed)
                    if field == "repairs_needed" and isinstance(value, str) and len(value.strip()) > 8:
                        append_note(lead, f"Repair details: {value.strip()}")
                continue
            if field in {"stories", "room_size_sqft", "rooms", "cabinet_count"}:
                if isinstance(value, int):
                    setattr(lead, field, value)
                elif isinstance(value, str):
                    digits = re.search(r"\d+", value)
                    if digits:
                        setattr(lead, field, int(digits.group(0)))
                continue
            if field == "notes":
                if isinstance(value, list):
                    for note in value:
                        append_note(lead, str(note)[:250])
                else:
                    append_note(lead, str(value)[:250])
                continue
            setattr(lead, field, value)

    reason = patch.get("reason")
    source = patch.get("source")
    if reason and patch.get("is_correction"):
        append_note(lead, f"Correction handled by {source or 'AI patch'}: {reason}")
    return lead



def sanitize_lead_schema(lead: LeadInfo) -> LeadInfo:
    """Force the in-memory lead back into the LeadInfo schema after any LLM patch.

    This prevents FastAPI/browser failures when an LLM returns a list/object/string
    for a scalar field. Keep this conservative: preserve meaning, but coerce types.
    """
    string_fields = {
        "name", "phone", "email", "address", "city", "service", "property_type",
        "project_scope", "timeline", "urgency", "preferred_callback_time", "lead_priority", "intent",
    }
    int_fields = {"room_size_sqft", "rooms", "stories", "cabinet_count", "lead_score"}
    bool_fields = {"walls_only", "ceiling", "trim", "occupied", "repairs_needed", "photos_available", "saved", "handoff_required"}

    for field in string_fields:
        value = getattr(lead, field, None)
        if value is None:
            continue
        if isinstance(value, list):
            setattr(lead, field, ", ".join(str(x).strip() for x in value if x) or None)
        elif isinstance(value, dict):
            setattr(lead, field, str(value)[:500])
        else:
            setattr(lead, field, str(value).strip() or None)

    for field in int_fields:
        value = getattr(lead, field, None)
        if value is None or isinstance(value, int):
            continue
        match = re.search(r"\d+", str(value))
        setattr(lead, field, int(match.group(0)) if match else None)

    for field in bool_fields:
        value = getattr(lead, field, None)
        if value is None or isinstance(value, bool):
            continue
        low = str(value).strip().lower()
        if low in {"true", "yes", "y", "1", "needed"}:
            setattr(lead, field, True)
        elif low in {"false", "no", "n", "0", "none", "not needed"}:
            setattr(lead, field, False)
        elif field == "repairs_needed" and any(w in low for w in ["repair", "damage", "stain", "bubbling", "peeling", "leak"]):
            append_note(lead, f"Repair details: {str(value)[:200]}")
            setattr(lead, field, True)
        else:
            setattr(lead, field, None)

    if not isinstance(lead.notes, list):
        lead.notes = [str(lead.notes)[:250]] if lead.notes else []
    else:
        lead.notes = [str(n)[:250] for n in lead.notes if n]
    return lead

def score_lead(lead: LeadInfo) -> LeadInfo:
    score = 0
    if lead.phone: score += 25
    if lead.name: score += 10
    if lead.city and looks_like_supported_city(lead.city): score += 15
    if lead.service: score += 10
    if lead.timeline: score += 10
    if lead.urgency == "urgent": score += 15
    elif lead.urgency == "soon": score += 10
    if lead.photos_available: score += 5
    if lead.repairs_needed: score += 5
    if lead.service and any(s in lead.service for s in ["exterior", "cabinet", "commercial"]): score += 10
    if lead.rooms and lead.rooms >= 3: score += 10
    lead.lead_score = min(score, 100)

    # Urgent operational triggers should look important even before every contact field is collected.
    hot_context = (
        lead.urgency == "urgent"
        and (
            lead.property_type in {"rental", "business"}
            or bool(lead.project_scope)
            or (lead.service and any(s in lead.service.lower() for s in ["exterior", "commercial", "cabinet"]))
        )
    )

    if lead.lead_score >= 75 or hot_context:
        lead.lead_priority = "Hot"
    elif lead.lead_score >= 45 or lead.urgency == "soon":
        lead.lead_priority = "Warm"
    else:
        lead.lead_priority = "Normal"
    return lead


def extract_info(message: str, lead: LeadInfo, debug: dict[str, Any] | None = None, *, live_mode: bool | None = None) -> LeadInfo:
    if live_mode is None:
        live_mode = live_call_mode_default()
    if debug is None:
        debug = {}
    debug.update({
        "reasoner_source": "rule",
        "reasoner_used": False,
        "reasoner_trigger": "rules_sufficient",
        "reasoner_confidence": None,
        "reasoner_latency_ms": 0,
        "reasoner_reason": "Handled by deterministic rules/context follow-up.",
        "llm_configured": llm_available(),
        "live_mode": live_mode,
        "llm_deferred": False,
    })
    before = lead.model_copy(deep=True)
    raw = message.strip()
    text = normalize_text(raw)
    lead.intent = None
    lead = clear_project_fields_for_correction(text, lead)
    lead = quick_detect_intent(raw, lead)
    lead = extract_city(text, lead)
    lead = extract_service(text, lead)
    lead = extract_size(text, lead)
    lead = extract_rooms(text, lead)
    lead = extract_surfaces(text, lead)
    lead = extract_timeline_and_callback(text, lead)
    lead = extract_phone(raw, lead)
    lead = extract_name(raw, lead)
    lead = extract_email(raw, lead)
    lead = extract_address(raw, lead)
    lead = extract_property_details(text, lead)
    lead = extract_contextual_followup_answer(raw, before, lead)
    lead = extract_bare_name(raw, before, lead)
    lead = extract_notes(text, raw, lead)

    should_route_to_ai, route_reason = explain_ai_patch_route(raw, before, lead)
    debug["reasoner_trigger"] = route_reason

    if should_route_to_ai:
        try:
            ai_start = time.perf_counter()
            # In live phone-call mode, never block on a slow real LLM call.
            # Use the fast heuristic patch now; run real AI later through post-call cleanup
            # or by disabling live mode from the UI.
            use_llm = not live_mode
            patch = extract_lead_patch(raw, before, use_llm=use_llm, reasoner_mode="live" if live_mode else "smart")
            elapsed_ms = int((time.perf_counter() - ai_start) * 1000)
            lead = apply_lead_patch(lead, patch)
            debug.update({
                "reasoner_source": patch.get("source", "unknown"),
                "reasoner_used": True,
                "reasoner_confidence": patch.get("confidence"),
                "reasoner_latency_ms": patch.get("latency_ms", elapsed_ms),
                "reasoner_reason": patch.get("reason") or "Structured patch extractor ran.",
                "reasoner_patch": patch,
                "llm_configured": patch.get("llm_configured", llm_available()),
                "llm_deferred": patch.get("llm_deferred", False),
                "live_mode": live_mode,
                "llm_error": patch.get("llm_error"),
            })
            logger.info(
                "AI patch extraction source=%s confidence=%s latency=%.3fs trigger=%s patch=%s",
                patch.get("source"), patch.get("confidence"), time.perf_counter() - ai_start, route_reason, patch,
            )
        except Exception as exc:
            debug.update({
                "reasoner_source": "error",
                "reasoner_used": True,
                "reasoner_reason": f"AI patch extraction failed; continued with rules: {exc!r}",
            })
            logger.exception("AI patch extraction failed; continuing with rules.")

    lead = sanitize_price_comparison_fields(text, lead)
    return score_lead(lead)


def sanitize_price_comparison_fields(text: str, lead: LeadInfo) -> LeadInfo:
    """Clean fields that were mentioned only as a pricing comparison, not actual scope.

    The user may ask whether a small repair is cheaper than repainting a whole room.
    In that case "whole room" should not set ceiling/trim/walls flags, and the
    response should preserve the smaller repair/touch-up scope.
    """
    if lead.intent != "price_question":
        return lead
    comparison_only = any(p in text for p in [
        "cheaper than repainting", "cheaper than painting", "instead of repainting",
        "instead of painting", "rather than repainting", "rather than painting",
    ])
    if comparison_only:
        # These were likely produced by the "whole room" comparison, not by the
        # actual requested scope. Leave explicit project_scope intact.
        lead.walls_only = None
        if "ceiling" not in text:
            lead.ceiling = None
        if not any(p in text for p in ["trim", "baseboard", "baseboards", "molding", "moulding"]):
            lead.trim = None
    return lead


def missing_fields(lead: LeadInfo) -> list[str]:
    missing: list[str] = []
    service = (lead.service or "").lower().strip()
    # A vague service like "painting project" means we still do not know
    # what kind of help the customer needs. Clarify the painting type before
    # asking city; otherwise a vague opener like "I just need help with paint"
    # sounds robotic: "What city is the project in?"
    if is_generic_or_unknown_service(lead):
        missing.append("service")
    if not lead.city:
        missing.append("city")
    if not lead.service and "service" not in missing:
        missing.append("service")
    if service_needs_square_feet(lead) and not lead.room_size_sqft:
        missing.append("room_size_sqft")
    # Walls/ceiling/trim is useful, but do not force it before collecting
    # contact information. Real receptionist flow should avoid too many
    # estimator-style questions up front.
    if "cabinet" in service and not lead.cabinet_count:
        missing.append("cabinet_details")
    if "exterior" in service:
        if not lead.project_scope:
            missing.append("exterior_scope")
        # Story count is useful for full exterior jobs, but not for small/mixed
        # jobs like "front door outside + hallway touch-ups inside".
        scope = (lead.project_scope or "").lower()
        small_exterior_scope = ("touch-up" in service or "+" in service or service.startswith("exterior door"))
        if not lead.stories and not small_exterior_scope:
            missing.append("stories")
    if not lead.timeline:
        missing.append("timeline")
    if not lead.name:
        missing.append("name")
    if not lead.phone:
        missing.append("phone")
    return missing


def next_question_field(missing: list[str]) -> str | None:
    """Return the field actually being asked next, matching next_missing_question priority."""
    for field in [
        "service", "city", "room_size_sqft", "walls_ceiling_trim",
        "exterior_scope", "stories", "name", "phone", "timeline", "cabinet_details",
    ]:
        if field in missing:
            return field
    return None


def next_missing_question(missing: list[str]) -> str:
    # Clarify vague painting type before collecting city. This is the natural
    # receptionist flow for messages like: "I just need help with paint."
    if "service" in missing: return "Is this for interior, exterior, cabinets, or touch-up painting?"
    if "city" in missing: return "What city is the project in?"
    if "room_size_sqft" in missing: return "About how large is the area? You can say the square footage, or dimensions like ten by twelve."
    if "walls_ceiling_trim" in missing: return "Is this walls only, or also the ceiling or trim?"
    if "exterior_scope" in missing: return "Is this the full exterior or just one area?"
    if "stories" in missing: return "How many stories is the property?"
    if "name" in missing: return "May I get your name?"
    if "phone" in missing: return "What is the best phone number for a call back?"
    if "timeline" in missing: return "When are you hoping to get this completed?"
    if "cabinet_details" in missing: return "About how many cabinet doors and drawers need painting?"
    return ""


def service_area_reply(lead: LeadInfo, missing: list[str]) -> str:
    if lead.city:
        if looks_like_supported_city(lead.city):
            follow_up = next_missing_question(missing)
            return f"Yes, we can help with painting projects in {lead.city}. {follow_up}" if follow_up else f"Yes, we can help with painting projects in {lead.city}. I have the details and can pass this to the painter."
        return f"I’m not fully sure whether {lead.city} is in the regular service area. The usual service area is {SUPPORTED_CITY_DISPLAY}. I can still take your details and have the painter confirm."
    return f"The regular service area includes {SUPPORTED_CITY_DISPLAY}. What city is your project in?"



def is_generic_or_unknown_service(lead: LeadInfo) -> bool:
    service = (lead.service or "").lower().strip()
    return service in {"", "painting project", "painting", "paint"}


def context_acknowledgement(lead: LeadInfo) -> str:
    """Briefly acknowledge newly understood project context. Use sparingly."""
    service = (lead.service or "").lower()
    if "exterior" in service and "interior" in service:
        city_part = f" in {lead.city}" if lead.city else ""
        scope_part = f" for {lead.project_scope}" if lead.project_scope else ""
        return f"Got it — mixed interior and exterior painting{city_part}{scope_part}."
    if "repair" in service and "cabinet" in service:
        return "Got it — possible repair, painting, and cabinet work."
    if service in {"paint repair / repainting", "drywall repair + painting"}:
        city_part = f" in {lead.city}" if lead.city else ""
        scope_part = f" for {lead.project_scope}" if lead.project_scope else ""
        return f"Got it — paint repair and repainting{city_part}{scope_part}."
    if "touch-up" in service or "paint repair" in service or "door painting" in service:
        city_part = f" in {lead.city}" if lead.city else ""
        rental_part = " for the rental" if lead.property_type == "rental" else ""
        return f"Got it — touch-up and paint repair work{city_part}{rental_part}."
    if "exterior" in service:
        if lead.project_scope == "front exterior only" and lead.property_type == "rental" and lead.urgency == "urgent":
            return "Got it — front exterior on a rental with a tight timeline."
        if lead.project_scope == "front exterior only":
            return "Got it — front exterior painting."
        if lead.urgency == "urgent":
            return "Got it — exterior painting with a tight timeline."
        return "Got it — exterior painting."
    if lead.property_type == "commercial office" and ("interior" in service or "commercial" in service):
        city_part = f" in {lead.city}" if lead.city else ""
        scope_part = f" for the {lead.project_scope}" if lead.project_scope else ""
        return f"Got it — commercial interior painting{city_part}{scope_part}."
    if "interior" in service:
        if lead.project_scope and lead.rooms and lead.rooms > 1:
            city_part = f" in {lead.city}" if lead.city else ""
            return f"No problem — interior painting for {lead.project_scope}{city_part}."
        if lead.rooms and lead.rooms > 1:
            return f"Got it — interior painting for {lead.rooms} rooms."
        if lead.project_scope:
            return f"Got it — interior painting for {lead.project_scope}."
        return "Got it — interior painting."
    if "cabinet" in service:
        return "Got it — cabinet painting."
    if "commercial" in service or lead.property_type == "business":
        return "Got it — commercial painting."
    if lead.repairs_needed:
        return "Got it — prep or repair may be needed."
    return "Got it."


def should_acknowledge_project_context(before: LeadInfo, after: LeadInfo) -> bool:
    """Only repeat a rich project summary when project context was just discovered.

    Without this guard, every later answer repeats the same summary after the
    caller gives city/name/phone, which sounds robotic on a phone call.
    """
    project_fields = [
        "service", "property_type", "project_scope", "urgency",
        "repairs_needed", "rooms", "room_size_sqft", "cabinet_count", "stories",
    ]
    for field in project_fields:
        if getattr(before, field) != getattr(after, field) and getattr(after, field) not in (None, "", []):
            return True
    return False


def light_acknowledgement(before: LeadInfo, after: LeadInfo) -> str:
    """Natural acknowledgement for non-project details, without re-summarizing."""
    if before.name != after.name and after.name:
        return f"Thanks, {after.name}."
    if before.city != after.city and after.city:
        return "Thanks."
    if before.phone != after.phone and after.phone:
        return "Thanks."
    if before.timeline != after.timeline and after.timeline:
        return "Got it."
    if before.photos_available != after.photos_available and after.photos_available is not None:
        return "Got it."
    return ""


def safe_price_reply(lead: LeadInfo, missing: list[str]) -> str:
    service = (lead.service or "").lower()
    # Repair/touch-up price-comparison questions should get a safe pricing answer,
    # not a diagnostic location question. Never imply a dollar estimate without
    # company-approved pricing rules.
    if lead.repairs_needed or "repair" in service or "touch-up" in service or (lead.project_scope and any(p in lead.project_scope.lower() for p in ["bubbling", "one wall", "stain", "peeling"])):
        follow_up = next_missing_question(missing)
        base = "Pricing depends on the condition, prep work, and whether repair is needed, but I can note the smaller repair or touch-up scope for follow-up."
        return f"{base} {follow_up}" if follow_up else base
    if is_generic_or_unknown_service(lead):
        return "Pricing depends on whether it is interior, exterior, cabinets, or touch-up work. Is this for interior, exterior, cabinets, or touch-up painting?"
    if "exterior" in service:
        follow_up = next_missing_question(missing)
        base = "Exterior pricing depends on home size, stories, prep work, access, and paint condition, so the painter needs details before a reliable quote."
        return f"{base} {follow_up}" if follow_up else base
    if "cabinet" in service:
        follow_up = next_missing_question(missing)
        base = "Cabinet pricing depends on the number of doors and drawers, finish, prep work, and whether spraying is needed."
        return f"{base} {follow_up}" if follow_up else base
    if "commercial" in service:
        follow_up = next_missing_question(missing)
        base = "Commercial pricing depends on square footage, access hours, prep work, and the type of space."
        return f"{base} {follow_up}" if follow_up else base
    # Default interior pricing answer: do not generate a dollar estimate from
    # placeholder rules. For a live receptionist, safe pricing should avoid
    # implying a quote unless the company has approved pricing data.
    follow_up = next_missing_question(missing)
    base = "Pricing depends on room size, prep work, paint type, wall condition, and number of coats. I can collect the details so someone can follow up with a reliable estimate."
    return f"{base} {follow_up}" if follow_up else base


def safe_duration_reply(lead: LeadInfo, missing: list[str]) -> str:
    service = (lead.service or "").lower()
    if is_generic_or_unknown_service(lead):
        return "Timing depends on the project type. Is this for interior, exterior, cabinets, or touch-up painting?"
    if "exterior" in service:
        follow_up = next_missing_question(missing)
        base = "Exterior timing depends on size, stories, prep work, weather, and crew schedule."
        return f"{base} {follow_up}" if follow_up else base
    if "cabinet" in service:
        follow_up = next_missing_question(missing)
        base = "Cabinet projects usually depend on the number of doors and drawers, prep, drying time, and finish."
        return f"{base} {follow_up}" if follow_up else base
    if "room_size_sqft" in missing:
        return "I can give a rough time estimate. About how large is the room in square feet?"
    estimate = estimate_painting_duration(lead)
    follow_up = next_missing_question(missing)
    return f"{estimate} {follow_up}" if follow_up else estimate

def generate_reply(message: str, lead: LeadInfo, before: LeadInfo | None = None) -> str:
    text = normalize_text(message)
    missing = missing_fields(lead)
    service_lower = (lead.service or "").lower()
    if lead.intent == "incomplete_phone":
        ack = context_acknowledgement(lead)
        if ack:
            return f"{ack} What’s the best full phone number for a callback?"
        return "What’s the best full phone number for a callback?"
    # Safety/price intent must win before repair-location clarifying questions.
    # Example: "is fixing bubbling paint and one wall cheaper than repainting the whole room?"
    # should receive a safe price-comparison response, not repeat "interior or exterior?".
    if lead.intent == "price_question":
        return safe_price_reply(lead, missing)
    # If the customer explicitly says they are unsure about bubbling, do not
    # over-classify it as a repair job yet. Ask the clarifying service question.
    if "bubbling" in text and "cabinet" not in text and any(p in text for p in ["not sure", "unsure", "don't know", "do not know"]):
        if lead.city or lead.timeline:
            follow_up = next_missing_question(missing)
            city_part = f" in {lead.city}" if lead.city else ""
            base = f"Got it — possible paint repair and repainting{city_part}."
            return f"{base} {follow_up}" if follow_up else base
        return "Got it. Is the bubbling on an interior wall or on the outside of the property?"
    # Complex mixed jobs should be acknowledged as lead context, not treated as
    # a simple cabinet FAQ. Example: "not sure if this is a painting job,
    # repair job, or both" + cabinets.
    if "repair" in service_lower and "cabinet" in service_lower:
        follow_up = next_missing_question(missing)
        base = "Got it — possible repair, painting, and cabinet work."
        return f"{base} {follow_up}" if follow_up else base
    if lead.handoff_required or lead.intent == "handoff_request":
        return "No problem. I’ll have someone from the team follow up directly." + (" What is the best phone number for a call back?" if not lead.phone else "")
    fast_answer = get_fast_company_answer(message)
    if fast_answer:
        follow_up = next_missing_question(missing)
        if (lead.service or lead.city or lead.timeline) and follow_up:
            return f"{fast_answer} {follow_up}"
        return fast_answer
    if lead.intent == "unsafe_price_schedule_request":
        follow_up = next_missing_question(missing)
        base = "I can’t promise same-day availability or pricing from here, especially for a whole-house project. I can collect the details and have someone follow up with a reliable estimate."
        return f"{base} {follow_up}" if follow_up else base
    if lead.intent == "availability_question":
        base = "Possibly — availability depends on the project size, location, and crew schedule. I can collect the details and have the team confirm."
        follow_up = next_missing_question(missing)
        return f"{base} {follow_up}" if follow_up else base
    if (lead.repairs_needed or "bubbling" in text) and is_generic_or_unknown_service(lead):
        if "bubbling" in text:
            return "Got it. Is the bubbling on an interior wall or on the outside of the property?"
        return "Got it — prep or repair may be needed. Is this for interior or exterior painting?"
    faq_answer = find_faq_answer(message)
    if faq_answer and lead.intent not in {"price_question", "duration_question", "service_area_question"}:
        follow_up = next_missing_question(missing)
        return f"{faq_answer} {follow_up}" if follow_up else faq_answer
    if lead.intent == "service_area_question":
        return service_area_reply(lead, missing)
    if lead.intent == "repair_question":
        base = "Yes, minor prep like small cracks, nail holes, or peeling paint can usually be handled before painting. Bigger damage may need an inspection first."
        follow_up = next_missing_question(missing)
        return f"{base} {follow_up}" if follow_up else base
    if lead.intent == "price_question":
        return safe_price_reply(lead, missing)
    if lead.intent == "duration_question":
        return safe_duration_reply(lead, missing)
    if lead.intent == "booking_request":
        follow_up = next_missing_question(missing)
        return f"Sure, I can help pass your request to the painter. {follow_up}" if follow_up else "Thanks. I have your contact information and project details. I’ll send this to the painter so they can follow up."
    follow_up = next_missing_question(missing)
    if follow_up:
        if any(p in text for p in ["thank", "thanks"]):
            return f"You’re welcome. {follow_up}"

        if before and should_acknowledge_project_context(before, lead):
            return f"{context_acknowledgement(lead)} {follow_up}"

        ack = light_acknowledgement(before, lead) if before else ""
        return f"{ack} {follow_up}".strip() if ack else follow_up
    return "Thanks. I have the project details and will send them to the painter for follow-up."


def build_conversation_summary(lead: LeadInfo) -> str:
    parts: list[str] = []
    for label, value in [
        ("Customer", lead.name), ("Service", lead.service), ("City", lead.city), ("Address", lead.address),
        ("Property", lead.property_type), ("Scope", lead.project_scope), ("Timeline", lead.timeline),
        ("Callback preference", lead.preferred_callback_time), ("Priority", lead.lead_priority),
    ]:
        if value:
            parts.append(f"{label}: {value}.")
    if lead.stories: parts.append(f"Stories: {lead.stories}.")
    if lead.room_size_sqft: parts.append(f"Approximate size: {lead.room_size_sqft} sq ft.")
    if lead.rooms: parts.append(f"Rooms: {lead.rooms}.")
    if lead.repairs_needed is True: parts.append("Repairs or prep may be needed.")
    if lead.photos_available is True: parts.append("Customer has photos available.")
    if lead.notes: parts.append(f"Notes: {'; '.join(lead.notes)}.")
    return " ".join(parts) if parts else "No project details collected yet."


def build_final_call_json(session_id: str, lead: LeadInfo, missing: list[str], ready: bool, latency_ms: int | None = None) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "created_at": datetime.utcnow().isoformat(),
        "customer": {"name": lead.name, "phone": lead.phone, "email": lead.email, "preferred_callback_time": lead.preferred_callback_time},
        "project": {
            "address": lead.address, "city": lead.city, "service": lead.service, "property_type": lead.property_type,
            "stories": lead.stories, "occupied": lead.occupied, "project_scope": lead.project_scope,
            "room_size_sqft": lead.room_size_sqft, "rooms": lead.rooms, "walls_only": lead.walls_only,
            "ceiling": lead.ceiling, "trim": lead.trim, "repairs_needed": lead.repairs_needed,
            "photos_available": lead.photos_available, "cabinet_count": lead.cabinet_count,
            "timeline": lead.timeline, "urgency": lead.urgency, "notes": lead.notes,
        },
        "lead_status": {
            "ready_to_send_to_painter": ready, "missing_fields": missing, "intent": lead.intent,
            "handoff_required": lead.handoff_required, "lead_score": lead.lead_score, "lead_priority": lead.lead_priority,
        },
        "conversation": {"summary": build_conversation_summary(lead), "transcript": get_transcript(session_id)},
        "metrics": {"latency_ms": latency_ms},
    }


def is_ready_to_save(lead: LeadInfo) -> bool:
    return len(missing_fields(lead)) == 0


def start_background_llm_reasoner(session_id: str, message: str, before: LeadInfo, trigger: str) -> None:
    """Run real AI in the background in live-call mode.

    The caller gets a fast heuristic response immediately. If a real LLM is
    configured, this background task refines the session lead a moment later so
    the next turn/dashboard sees cleaner AI-extracted state.
    """
    if not llm_available():
        return

    def _run() -> None:
        with BACKGROUND_REASONER_LOCK:
            BACKGROUND_REASONER_STATUS[session_id] = {
                "status": "running",
                "trigger": trigger,
                "started_at": datetime.utcnow().isoformat(),
            }
        try:
            start = time.perf_counter()
            patch = extract_lead_patch(message, before, use_llm=True, reasoner_mode="background_live_cleanup")
            current = get_lead(session_id)
            apply_lead_patch(current, patch)
            score_lead(current)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            with BACKGROUND_REASONER_LOCK:
                BACKGROUND_REASONER_STATUS[session_id] = {
                    "status": "complete",
                    "trigger": trigger,
                    "source": patch.get("source"),
                    "confidence": patch.get("confidence"),
                    "latency_ms": elapsed_ms,
                    "reason": patch.get("reason"),
                    "completed_at": datetime.utcnow().isoformat(),
                }
        except Exception as exc:
            logger.exception("Background LLM reasoner failed.")
            with BACKGROUND_REASONER_LOCK:
                BACKGROUND_REASONER_STATUS[session_id] = {
                    "status": "error",
                    "trigger": trigger,
                    "error": repr(exc),
                    "completed_at": datetime.utcnow().isoformat(),
                }

    threading.Thread(target=_run, daemon=True).start()


def get_background_reasoner_status(session_id: str) -> dict[str, Any] | None:
    with BACKGROUND_REASONER_LOCK:
        status = BACKGROUND_REASONER_STATUS.get(session_id)
        return dict(status) if status else None


def handle_message(session_id: str, message: str, live_mode: bool | None = None) -> dict[str, Any]:
    start = time.perf_counter()
    lead = get_lead(session_id)
    before = lead.model_copy(deep=True)

    # Voice/browser echo guard: if the customer input is just the receptionist's
    # previous reply, do not let it corrupt lead state.
    previous_ai = last_ai_reply(session_id)
    if is_probable_assistant_echo(message, previous_ai):
        latency_ms = int((time.perf_counter() - start) * 1000)
        reply = previous_ai or generate_reply(message, lead, before)
        missing = missing_fields(lead)
        return {
            "reply": reply,
            "lead": lead,
            "missing_fields": missing,
            "ready_to_send_to_painter": len(missing) == 0,
            "handoff_required": lead.handoff_required,
            "final_call_json": None,
            "metrics": {
                "latency_ms": latency_ms,
                "lead_score": lead.lead_score,
                "lead_priority": lead.lead_priority,
                "reasoner_source": "echo_guard",
                "reasoner_used": False,
                "reasoner_trigger": "assistant_echo_ignored",
                "reasoner_confidence": 1.0,
                "reasoner_latency_ms": 0,
                "reasoner_reason": "Ignored probable echo of the receptionist's previous reply; lead state was not changed.",
                "reasoner_patch": None,
                "llm_configured": llm_available(),
                "llm_deferred": False,
                "live_mode": live_call_mode_default() if live_mode is None else live_mode,
                "llm_error": None,
                "background_llm_started": False,
                "background_reasoner_status": get_background_reasoner_status(session_id),
            },
        }

    add_transcript_message(session_id, "customer", message.strip())
    extract_start = time.perf_counter()
    if live_mode is None:
        live_mode = live_call_mode_default()
    reasoner_debug: dict[str, Any] = {}
    lead = extract_info(message, lead, reasoner_debug, live_mode=live_mode)
    if (
        live_mode
        and reasoner_debug.get("llm_deferred")
        and reasoner_debug.get("llm_configured")
        and reasoner_debug.get("reasoner_used")
    ):
        start_background_llm_reasoner(session_id, message, before, reasoner_debug.get("reasoner_trigger", "live_deferred"))
        reasoner_debug["background_llm_started"] = True
    else:
        reasoner_debug["background_llm_started"] = False
    extraction_latency = time.perf_counter() - extract_start
    reply_start = time.perf_counter()
    draft_reply = generate_reply(message, lead, before)
    reply, talker_debug = personalize_reply(message, lead, before, draft_reply, live_mode=live_mode)
    reply_latency = time.perf_counter() - reply_start
    add_transcript_message(session_id, "ai", reply)
    missing = missing_fields(lead)
    ready = len(missing) == 0
    final_call_json: dict[str, Any] | None = None
    latency_ms = int((time.perf_counter() - start) * 1000)
    save_start = time.perf_counter()
    if (ready or lead.handoff_required) and not lead.saved:
        try:
            final_call_json = build_final_call_json(session_id, lead, missing, ready, latency_ms)
            save_lead_to_db(session_id, lead, final_call_json)
            save_final_call_json(session_id, final_call_json)
            lead.saved = True
        except Exception:
            logger.exception("Failed to save lead/final call JSON.")
    logger.info(
        "Receptionist latency session=%s extraction=%.3fs reply=%.3fs save=%.3fs total=%dms ready=%s missing=%s score=%s",
        session_id, extraction_latency, reply_latency, time.perf_counter() - save_start, latency_ms, ready, missing, lead.lead_score,
    )
    return {
        "reply": reply,
        "lead": lead,
        "missing_fields": missing,
        "ready_to_send_to_painter": ready,
        "handoff_required": lead.handoff_required,
        "final_call_json": final_call_json,
        "metrics": {
            "latency_ms": latency_ms,
            "lead_score": lead.lead_score,
            "lead_priority": lead.lead_priority,
            "reasoner_source": reasoner_debug.get("reasoner_source", "rule"),
            "reasoner_used": reasoner_debug.get("reasoner_used", False),
            "reasoner_trigger": reasoner_debug.get("reasoner_trigger", "rules_sufficient"),
            "reasoner_confidence": reasoner_debug.get("reasoner_confidence"),
            "reasoner_latency_ms": reasoner_debug.get("reasoner_latency_ms", 0),
            "reasoner_reason": reasoner_debug.get("reasoner_reason"),
            "reasoner_patch": reasoner_debug.get("reasoner_patch"),
            "llm_configured": reasoner_debug.get("llm_configured", llm_available()),
            "llm_deferred": reasoner_debug.get("llm_deferred", False),
            "live_mode": reasoner_debug.get("live_mode", live_mode),
            "llm_error": reasoner_debug.get("llm_error"),
            "background_llm_started": reasoner_debug.get("background_llm_started", False),
            "background_reasoner_status": get_background_reasoner_status(session_id),
            "talker_source": talker_debug.get("talker_source"),
            "talker_used": talker_debug.get("talker_used"),
            "talker_latency_ms": talker_debug.get("talker_latency_ms", 0),
            "talker_reason": talker_debug.get("talker_reason"),
        },
    }

# ---------------------------------------------------------------------------
# v19 LLM-FIRST LIVE RECEPTIONIST
# ---------------------------------------------------------------------------
# Keep the old rule-heavy implementation available as a fallback, but prefer a
# fast LLM for natural understanding + personality whenever an LLM is configured.
_legacy_handle_message = handle_message


def _v19_llm_first_enabled() -> bool:
    return os.getenv("AI_RECEPTIONIST_LLM_FIRST", "true").strip().lower() not in {"0", "false", "no", "off"}


def handle_message(session_id: str, message: str, live_mode: bool | None = None) -> dict[str, Any]:  # type: ignore[override]
    """LLM-first handler with deterministic guardrails.

    v19 design:
    - LLM does the messy understanding and warm customer wording.
    - Code applies a strict lead patch, validates safety, and saves state.
    - If LLM is unavailable or errors, fall back to the previous rule engine.
    """
    if not _v19_llm_first_enabled():
        return _legacy_handle_message(session_id, message, live_mode=live_mode)

    from app.llm_receptionist import llm_first_available, run_llm_receptionist, sanitize_llm_reply
    from app.faq_sheet import deterministic_faq_answer, is_standalone_faq
    from app.ai_response_cache import stats as ai_cache_stats

    # Fast FAQ cache: simple company-fact questions do not need an LLM call.
    if is_standalone_faq(message):
        faq = deterministic_faq_answer(message)
        if faq:
            answer, faq_key = faq
            lead = get_lead(session_id)
            add_transcript_message(session_id, "customer", message.strip())
            add_transcript_message(session_id, "ai", answer)
            latency_ms = 0
            missing = missing_fields(lead)
            return {
                "reply": answer,
                "lead": lead,
                "missing_fields": missing,
                "ready_to_send_to_painter": len(missing) == 0,
                "handoff_required": lead.handoff_required,
                "final_call_json": None,
                "metrics": {
                    "latency_ms": latency_ms,
                    "architecture": "llm_first_with_faq_cache",
                    "reasoner_source": "faq_cache",
                    "reasoner_trigger": f"standalone_faq:{faq_key}",
                    "reasoner_used": False,
                    "llm_configured": llm_first_available(),
                    "cache_hit": True,
                    "cache_kind": "faq",
                    "cache_stats": ai_cache_stats(),
                    "talker_source": "faq_cache",
                    "talker_used": True,
                    "talker_latency_ms": 0,
                    "talker_reason": "Answered from deterministic company FAQ cache; no LLM call needed.",
                },
            }

    # If no real LLM is configured, preserve the old fast project behavior.
    if not llm_first_available():
        result = _legacy_handle_message(session_id, message, live_mode=live_mode)
        result.setdefault("metrics", {})["architecture"] = "legacy_rules_no_llm_configured"
        return result

    start = time.perf_counter()
    lead = get_lead(session_id)
    before = lead.model_copy(deep=True)

    previous_ai = last_ai_reply(session_id)
    if is_probable_assistant_echo(message, previous_ai):
        latency_ms = int((time.perf_counter() - start) * 1000)
        missing = missing_fields(lead)
        return {
            "reply": previous_ai or "Sorry, I may have picked up my own audio there. Could you repeat that?",
            "lead": lead,
            "missing_fields": missing,
            "ready_to_send_to_painter": len(missing) == 0,
            "handoff_required": lead.handoff_required,
            "final_call_json": None,
            "metrics": {
                "latency_ms": latency_ms,
                "architecture": "llm_first",
                "reasoner_source": "echo_guard",
                "reasoner_trigger": "assistant_echo_ignored",
                "reasoner_used": False,
                "llm_configured": True,
            },
        }

    add_transcript_message(session_id, "customer", message.strip())

    try:
        draft_reply, patch, debug = run_llm_receptionist(message, lead, get_transcript(session_id), timeout_label="live_llm")
        lead = apply_lead_patch(lead, patch)
        lead = sanitize_lead_schema(lead)
        lead = score_lead(lead)
        reply = sanitize_llm_reply(draft_reply, message, lead)
    except Exception as exc:
        logger.exception("LLM-first receptionist failed; falling back to legacy rules.")
        # Remove just-added customer message? Keep it; it is useful transcript.
        result = _legacy_handle_message(session_id, message, live_mode=live_mode)
        result.setdefault("metrics", {})["architecture"] = "legacy_fallback_after_llm_error"
        result["metrics"]["llm_error"] = str(exc)[:300]
        return result

    add_transcript_message(session_id, "ai", reply)
    missing = missing_fields(lead)
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

    return {
        "reply": reply,
        "lead": lead,
        "missing_fields": missing,
        "ready_to_send_to_painter": ready,
        "handoff_required": lead.handoff_required,
        "final_call_json": final_call_json,
        "metrics": {
            "latency_ms": latency_ms,
            "architecture": "llm_first",
            "lead_score": lead.lead_score,
            "lead_priority": lead.lead_priority,
            "reasoner_source": "llm_first",
            "reasoner_used": True,
            "reasoner_trigger": "llm_first_live_turn",
            "reasoner_confidence": debug.get("confidence"),
            "reasoner_latency_ms": debug.get("latency_ms"),
            "reasoner_reason": debug.get("reason"),
            "reasoner_patch": patch,
            "llm_configured": True,
            "llm_model": debug.get("model"),
            "llm_deferred": False,
            "live_mode": live_call_mode_default() if live_mode is None else live_mode,
            "talker_source": "llm_first_reply",
            "talker_used": True,
            "talker_latency_ms": debug.get("latency_ms"),
            "talker_reason": "Same fast LLM generated the warm reply and structured lead patch; deterministic guardrails sanitized the final reply.",
            "cache_hit": bool(debug.get("cache_hit")),
            "cache_kind": "ai_response" if debug.get("cache_hit") else None,
            "cache_stats": ai_cache_stats(),
        },
    }
