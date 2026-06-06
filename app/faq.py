"""Fast FAQ layer for common painting-company questions.

This is intentionally simple and deterministic so common answers are fast.
The receptionist can still ask a follow-up question after answering.
"""

from app.company_config import COMPANY_CONFIG, service_area_display


def _licensed_answer() -> str:
    if COMPANY_CONFIG.get("licensed_insured"):
        return "Yes, the team is licensed and insured. I can also have someone follow up with details if needed."
    return "I can have someone from the team confirm the license and insurance details for you."


FAQS = {
    "free_estimate": {
        "patterns": ["free estimate", "free quote", "estimate free", "quote free"],
        "answer": "Yes, free estimates are available. I can collect a few details and have the team follow up.",
    },
    "licensed_insured": {
        "patterns": ["licensed", "insured", "insurance", "bonded"],
        "answer": _licensed_answer(),
    },
    "business_hours": {
        "patterns": ["hours", "open", "closed", "business hours", "when are you open"],
        "answer": (
            "The usual hours are Monday through Friday 8 AM to 6 PM, Saturday 9 AM to 2 PM, "
            "and Sunday closed. I can still take your details now."
        ),
    },
    "service_area": {
        "patterns": ["service area", "where do you work", "what cities", "what areas"],
        "answer": f"The regular service area includes {service_area_display()}.",
    },
    "cabinet_painting": {
        "patterns": ["cabinet", "cabinets", "kitchen cabinets", "bathroom cabinets"],
        "answer": "Yes, cabinet painting is available. Is this for kitchen cabinets, bathroom cabinets, or built-ins?",
    },
    "pricing": {
        "patterns": ["how much", "price", "pricing", "cost", "quote"],
        "answer": (
            "Pricing depends on size, prep work, paint type, and surface condition. "
            "I can collect a few details so the team can give a more accurate estimate."
        ),
    },
}


def find_faq_answer(text: str) -> str | None:
    cleaned = text.lower()

    for faq in FAQS.values():
        for pattern in faq["patterns"]:
            if pattern in cleaned:
                return faq["answer"]

    return None
