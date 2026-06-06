"""Company-specific configuration for the painting receptionist.

Keep business facts here instead of hardcoding them in conversation logic.
Edit this file when the painting company changes service areas, hours, policies,
or handoff details.
"""

COMPANY_CONFIG = {
    "company_name": "BrightLine Painting",
    "service_areas": [
        "San Jose",
        "Berkeley",
        "Fremont",
        "Milpitas",
        "Sunnyvale",
        "Santa Clara",
        "Oakland",
        "Palo Alto",
        "Mountain View",
        "Cupertino",
        "Redwood City",
        "San Mateo",
        "Foster City",
        "Daly City",
        "Menlo Park",
    ],
    "services": [
        "interior painting",
        "exterior painting",
        "cabinet painting",
        "touch-up painting",
        "drywall repair",
        "trim painting",
        "commercial painting",
    ],
    "business_hours": {
        "monday_friday": "8 AM - 6 PM",
        "saturday": "9 AM - 2 PM",
        "sunday": "Closed",
    },
    "estimate_policy": "Free estimates are available.",
    "pricing_policy": (
        "Do not guarantee exact pricing over the phone. Give rough ranges only "
        "when enough project details are available, and always frame them as estimates."
    ),
    "availability_policy": (
        "Do not promise exact availability. Collect project details and have the team confirm."
    ),
    "licensed_insured": True,
    "human_handoff_phone": "408-555-1234",
}


def service_area_display() -> str:
    """Return a human-friendly service-area list."""
    areas = COMPANY_CONFIG["service_areas"]
    if len(areas) <= 1:
        return ", ".join(areas)
    return ", ".join(areas[:-1]) + f", and {areas[-1]}"
