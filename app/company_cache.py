import re

from app.company_config import COMPANY_CONFIG, service_area_display


SERVICE_AREAS_LOWER = {
    city.lower(): city
    for city in COMPANY_CONFIG["service_areas"]
}

SERVICES_LOWER = {
    service.lower(): service
    for service in COMPANY_CONFIG["services"]
}


def normalize(text: str) -> str:
    """
    Normalize customer text so matching is easier.

    Example:
    "  Do   you serve   San Mateo? "
    becomes:
    "do you serve san mateo?"
    """
    return re.sub(r"\s+", " ", text.lower().strip())


def find_city_in_message(message: str) -> str | None:
    """
    Quickly detect if the customer mentioned a supported city.
    Returns the nice/original city name from company_config.py.
    """
    text = normalize(message)

    for city_lower, original_city in SERVICE_AREAS_LOWER.items():
        if city_lower in text:
            return original_city

    return None


def find_service_in_message(message: str) -> str | None:
    """
    Quickly detect if the customer mentioned a painting service.
    """
    text = normalize(message)

    negative_interior = any(p in text for p in ["not bedroom", "not bedrooms", "not inside", "not interior"])
    negative_exterior = any(p in text for p in ["not outside", "not exterior"])

    # Strong exterior words should win over stale interior words in corrections
    # like: "Actually it’s exterior siding, not bedrooms."
    if any(word in text for word in ["outside", "exterior", "siding", "house exterior"]) and not negative_exterior:
        return "exterior painting"

    if any(word in text for word in ["cabinet", "cabinets", "kitchen cabinets"]):
        return "cabinet painting"

    # Direct match from company_config.py
    for service_lower, original_service in SERVICES_LOWER.items():
        if service_lower in text:
            return original_service

    # Common customer wording
    if any(word in text for word in ["inside", "interior", "bedroom", "bedrooms", "living room", "room", "hallway"]) and not negative_interior:
        return "interior painting"

    if any(word in text for word in ["touch up", "touch-up", "small paint fix"]):
        return "touch-up painting"

    if any(word in text for word in ["drywall", "wall repair", "patch"]):
        return "drywall repair"

    if any(word in text for word in ["trim", "baseboard", "baseboards", "molding"]):
        return "trim painting"

    if any(word in text for word in ["commercial", "office", "business", "storefront"]):
        return "commercial painting"

    return None


def get_fast_company_answer(message: str) -> str | None:
    """
    Layer 1: Instant company answer.

    This should only answer stable company-fact questions.
    If it returns None, receptionist.py continues normal conversation logic.
    """
    text = normalize(message)

    # Full service-area list
    if any(phrase in text for phrase in [
        "where do you serve",
        "what areas do you serve",
        "what cities do you serve",
        "which cities do you serve",
        "service area",
        "service areas",
    ]):
        return f"We currently serve {service_area_display()}."

    # Specific service-area question
    if any(phrase in text for phrase in [
        "do you serve",
        "do you work in",
        "are you available in",
        "can you come to",
    ]):
        city = find_city_in_message(message)

        if city:
            return f"Yes, {city} is in our service area."

        return (
            f"We currently serve {service_area_display()}. "
            "What city is your project in?"
        )

    # Business hours. Avoid treating project timelines like "next weekend" as an hours question.
    explicit_hours_question = any(phrase in text for phrase in [
        "what are your hours", "business hours", "when are you open",
        "are you open", "are you closed", "open on saturday",
        "open saturday", "open on sunday", "open sunday", "weekend hours",
    ])
    if explicit_hours_question:
        hours = COMPANY_CONFIG["business_hours"]

        return (
            f"Our hours are Monday through Friday {hours['monday_friday']}, "
            f"Saturday {hours['saturday']}, and Sunday {hours['sunday']}."
        )

    # Free estimate
    if any(phrase in text for phrase in [
        "free estimate",
        "free quote",
        "estimate free",
        "quote free",
        "do estimates cost",
    ]):
        return COMPANY_CONFIG["estimate_policy"]

    # Licensed / insured
    if any(word in text for word in ["licensed", "insured", "insurance"]):
        if COMPANY_CONFIG["licensed_insured"]:
            return "Yes, the team is licensed and insured."

        return "I can have someone from the team confirm the license and insurance details."

    # Services list
    if any(phrase in text for phrase in [
        "what services",
        "services do you offer",
        "what do you do",
        "do you do painting",
    ]):
        services = ", ".join(COMPANY_CONFIG["services"])
        return f"We can help with {services}."

    # Specific service question
    if text.startswith("do you do") or text.startswith("can you do"):
        service = find_service_in_message(message)

        if service:
            return f"Yes, we can help with {service}."

    return None