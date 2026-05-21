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