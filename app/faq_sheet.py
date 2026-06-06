"""Company FAQ sheet used by both deterministic FAQ cache and LLM prompt."""

from __future__ import annotations

import re

from app.company_config import COMPANY_CONFIG, service_area_display

FAQ_SHEET_TEXT = f"""
Company facts for the painting receptionist:
- Service area: {service_area_display()}.
- Business hours: Monday-Friday {COMPANY_CONFIG['business_hours']['monday_friday']}; Saturday {COMPANY_CONFIG['business_hours']['saturday']}; Sunday {COMPANY_CONFIG['business_hours']['sunday']}.
- Free estimates: {COMPANY_CONFIG['estimate_policy']}.
- Licensed/insured: {'yes' if COMPANY_CONFIG.get('licensed_insured') else 'ask team to confirm'}.
- Services: {', '.join(COMPANY_CONFIG['services'])}.
- Pricing policy: do not quote exact prices; pricing depends on size, prep work, paint type, surface condition, and number of coats.
- Availability policy: do not promise exact appointments; note preferred times and say the team will confirm.
""".strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def is_standalone_faq(text: str) -> bool:
    """True only for simple company-fact questions without project details."""
    t = _norm(text)
    if len(t.split()) > 18:
        return False
    project_terms = [
        "my ", "rental", "house", "home", "bedroom", "hallway", "kitchen", "exterior", "interior",
        "painted", "painting", "repaint", "touch", "peeling", "bubbling", "tenant", "showing", "photos",
    ]
    faq_terms = [
        "hours", "open", "closed", "licensed", "insured", "free estimate", "free quote",
        "service area", "where do you serve", "what cities", "what services", "do you do cabinets",
    ]
    if not any(term in t for term in faq_terms):
        return False
    # Allow service capability FAQ like "do you do cabinets".
    if "do you do" in t or "can you do" in t:
        return len(t.split()) <= 8
    return not any(term in t for term in project_terms)


def deterministic_faq_answer(text: str) -> tuple[str, str] | None:
    t = _norm(text)
    if any(p in t for p in ["what are your hours", "business hours", "when are you open", "are you open", "open saturday", "open sunday"]):
        h = COMPANY_CONFIG["business_hours"]
        return (f"Our hours are Monday through Friday {h['monday_friday']}, Saturday {h['saturday']}, and Sunday {h['sunday']}.", "business_hours")
    if any(p in t for p in ["licensed", "insured", "insurance", "bonded"]):
        ans = "Yes, the team is licensed and insured." if COMPANY_CONFIG.get("licensed_insured") else "I can have someone from the team confirm that for you."
        return (ans, "licensed_insured")
    if any(p in t for p in ["free estimate", "free quote", "estimate free", "quote free", "do estimates cost"]):
        return ("Yes, free estimates are available. I can collect a few details whenever you're ready.", "free_estimate")
    if any(p in t for p in ["service area", "where do you serve", "what cities", "what areas"]):
        return (f"We currently serve {service_area_display()}.", "service_area")
    if any(p in t for p in ["what services", "services do you offer", "what do you do"]):
        return (f"We can help with {', '.join(COMPANY_CONFIG['services'])}.", "services")
    if "cabinet" in t and ("do you" in t or "can you" in t):
        return ("Yes, cabinet painting is available. Are you looking to paint existing cabinets?", "cabinet_capability")
    return None
