from app.models import LeadInfo


def estimate_painting_duration(lead: LeadInfo) -> str:
    """
    Rule-based time estimate.
    This gives a rough duration, not a guaranteed quote.
    """

    size = lead.room_size_sqft or 100
    rooms = lead.rooms or 1
    total_sqft = size * rooms

    if total_sqft <= 150:
        base = "0.5 to 1 day"
    elif total_sqft <= 300:
        base = "1 to 2 days"
    elif total_sqft <= 600:
        base = "2 to 3 days"
    else:
        base = "3+ days"

    modifiers = []

    if lead.ceiling:
        modifiers.append("ceiling painting may add extra time")

    if lead.trim:
        modifiers.append("trim/detail work may add extra time")

    notes_text = " ".join(lead.notes).lower()

    if "damage" in notes_text or "repair" in notes_text:
        modifiers.append("wall repair or heavy prep may extend the timeline")

    modifier_text = ""
    if modifiers:
        modifier_text = " Factors: " + "; ".join(modifiers) + "."

    return (
        f"For about {total_sqft} sq ft of interior painting, "
        f"the project usually takes around {base}, depending on prep work, "
        f"number of coats, paint drying time, and wall condition."
        f"{modifier_text}"
    )


def estimate_painting_price(lead: LeadInfo) -> str:
    """
    Rough rule-based price estimate.
    This is not a final quote.
    """

    size = lead.room_size_sqft or 100
    rooms = lead.rooms or 1
    total_sqft = size * rooms

    low = total_sqft * 2
    high = total_sqft * 5

    if lead.ceiling:
        high += 100

    if lead.trim:
        high += 150

    notes_text = " ".join(lead.notes).lower()

    if "damage" in notes_text or "repair" in notes_text:
        high += 200

    return (
        f"For about {total_sqft} sq ft of interior painting, "
        f"a rough price range could be around ${low} to ${high}. "
        "This is only an estimate. The painter should confirm the final quote "
        "after checking prep work, paint type, wall condition, and number of coats."
    )