from app.models import LeadInfo


INTERIOR_KEYWORDS = {
    "interior painting",
    "interior",
    "room painting",
    "bedroom painting",
    "living room painting",
    "walls",
}


def is_interior_project(lead: LeadInfo) -> bool:
    """
    Only allow this estimator for interior-style projects.
    Exterior, cabinet, commercial, and drywall-only jobs need human review.
    """
    service = (lead.service or "").lower()
    scope = (lead.project_scope or "").lower()

    combined_text = f"{service} {scope}"

    return any(keyword in combined_text for keyword in INTERIOR_KEYWORDS)


def get_total_sqft(lead: LeadInfo) -> int | None:
    """
    Return estimated total square footage only if enough info exists.
    Avoid quietly guessing 100 sqft because that can mislead the customer.
    """
    if lead.room_size_sqft and lead.rooms:
        return lead.room_size_sqft * lead.rooms

    if lead.room_size_sqft:
        return lead.room_size_sqft

    return None


def get_notes_text(lead: LeadInfo) -> str:
    """
    Safely combine notes into lowercase text.
    Handles None, strings, or lists.
    """
    if not lead.notes:
        return ""

    if isinstance(lead.notes, list):
        return " ".join(str(note) for note in lead.notes).lower()

    return str(lead.notes).lower()


def estimate_painting_duration(lead: LeadInfo) -> str:
    """
    Rule-based rough duration estimate for simple interior painting only.
    This is not a guaranteed timeline.
    """

    if not is_interior_project(lead):
        return (
            "For this type of project, the timeline really depends on the exact scope, "
            "prep work, access, surface condition, and number of coats. The painter should "
            "review the details before giving a reliable time estimate."
        )

    total_sqft = get_total_sqft(lead)

    if not total_sqft:
        return (
            "I can give a rough timeline, but I would need one more detail first: "
            "about how many rooms, or roughly how many square feet, are we talking about?"
        )

    if total_sqft <= 150:
        base = "about half a day to 1 day"
    elif total_sqft <= 300:
        base = "about 1 to 2 days"
    elif total_sqft <= 600:
        base = "about 2 to 3 days"
    else:
        base = "3 or more days"

    modifiers = []

    if lead.ceiling:
        modifiers.append("ceilings can add time")

    if lead.trim:
        modifiers.append("trim or detail work can add time")

    notes_text = get_notes_text(lead)

    if any(word in notes_text for word in ["damage", "repair", "patch", "hole", "crack", "water"]):
        modifiers.append("wall repair or heavier prep can extend the timeline")

    modifier_text = ""
    if modifiers:
        modifier_text = " A few things that could affect that are: " + "; ".join(modifiers) + "."

    return (
        f"For roughly {total_sqft} sq ft of interior painting, a typical ballpark timeline "
        f"could be {base}. This is not a guaranteed schedule, since prep work, drying time, "
        f"paint type, wall condition, and number of coats can change the final timeline."
        f"{modifier_text}"
    )


def estimate_painting_price(lead: LeadInfo) -> str:
    """
    Rule-based rough price range for simple interior painting only.
    This is not a final quote.
    """

    if not is_interior_project(lead):
        return (
            "For this type of project, I would not want to guess a price without a painter "
            "reviewing the details. The final quote depends on the scope, prep work, surface "
            "condition, paint type, access, and number of coats."
        )

    total_sqft = get_total_sqft(lead)

    if not total_sqft:
        return (
            "I can give a rough ballpark, but I would need one more detail first: "
            "about how many rooms, or roughly how many square feet, are we talking about?"
        )

    low = total_sqft * 2
    high = total_sqft * 5

    adjustment_notes = []

    if lead.ceiling:
        high += 100
        adjustment_notes.append("ceilings may increase the price")

    if lead.trim:
        high += 150
        adjustment_notes.append("trim/detail work may increase the price")

    notes_text = get_notes_text(lead)

    if any(word in notes_text for word in ["damage", "repair", "patch", "hole", "crack", "water"]):
        high += 200
        adjustment_notes.append("wall repair or heavy prep may increase the price")

    adjustment_text = ""
    if adjustment_notes:
        adjustment_text = " Factors that could affect the price include: " + "; ".join(adjustment_notes) + "."

    return (
        f"For roughly {total_sqft} sq ft of interior painting, a very rough ballpark range "
        f"could be around ${low:,} to ${high:,}. This is not a quote or guaranteed price. "
        f"The painter should confirm the final quote after checking prep work, paint type, "
        f"wall condition, access, and number of coats."
        f"{adjustment_text}"
    )