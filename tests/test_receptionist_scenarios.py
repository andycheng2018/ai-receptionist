"""Scenario tests for the painting AI receptionist.

Run from project root:
    python tests/test_receptionist_scenarios.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.receptionist import handle_message, reset_session


def run_conversation(session_id, messages):
    reset_session(session_id)
    last = None
    for message in messages:
        last = handle_message(session_id, message)
    return last


def test_exterior_rental_lead():
    result = run_conversation(
        "scenario_exterior_rental",
        [
            "I have a rental where the front looks rough and tenants are moving in soon.",
            "It is in San Mateo.",
            "front exterior only, two story house",
            "I can send photos and need it before next weekend",
            "My name is Andy and my phone is 650-555-1212, call me tomorrow afternoon",
        ],
    )
    lead = result["lead"]
    assert result["ready_to_send_to_painter"] is True
    assert lead.service == "exterior painting"
    assert lead.city == "San Mateo"
    assert lead.project_scope == "front exterior only"
    assert lead.stories == 2
    assert lead.photos_available is True
    assert lead.timeline == "before next weekend"
    assert lead.preferred_callback_time == "tomorrow afternoon"
    assert lead.lead_priority == "Hot"


def test_interior_price_flow():
    result = run_conversation(
        "scenario_interior_price",
        [
            "How much to paint a bedroom in Palo Alto?",
            "ten by twelve, walls only",
            "next week",
            "This is Maya, 415 555 9999",
        ],
    )
    lead = result["lead"]
    assert lead.city == "Palo Alto"
    assert lead.service == "interior painting"
    assert lead.room_size_sqft == 120
    assert lead.walls_only is True
    assert result["ready_to_send_to_painter"] is True


def test_visible_first_reply_and_safe_price_question():
    reset_session("scenario_visible")
    result = handle_message("scenario_visible", "I have a rental where the front looks rough and tenants are moving in soon.")
    assert "front exterior" in result["reply"].lower()
    assert "tight timeline" in result["reply"].lower()
    assert result["lead"].lead_priority == "Hot"

    reset_session("scenario_safe_price")
    result = handle_message("scenario_safe_price", "How much to paint my house?")
    assert "interior, exterior" in result["reply"].lower()
    assert "rough price range" not in result["reply"].lower()


def test_handoff_request_saves_minimum():
    reset_session("scenario_handoff")
    result = handle_message("scenario_handoff", "I am upset and want to talk to a real person. My number is 650-555-0000.")
    assert result["handoff_required"] is True
    assert result["lead"].phone == "650-555-0000"


def test_no_repeated_project_summary_during_contact_collection():
    reset_session("scenario_no_repeat")
    first = handle_message("scenario_no_repeat", "I have a rental where the front looks rough and tenants are moving in soon.")
    second = handle_message("scenario_no_repeat", "san mateo")
    third = handle_message("scenario_no_repeat", "my name is andy")

    assert "front exterior" in first["reply"].lower()
    assert "front exterior" not in second["reply"].lower()
    assert "tight timeline" not in second["reply"].lower()
    assert "front exterior" not in third["reply"].lower()
    assert "tight timeline" not in third["reply"].lower()
    assert third["reply"].startswith("Thanks, Andy.")


def test_correction_clears_stale_exterior_fields():
    reset_session("scenario_correction")
    first = handle_message(
        "scenario_correction",
        "I have a rental in San Mateo. The front exterior looks pretty bad, paint is peeling around the trim, and tenants are moving in next Friday. I need someone to take a look soon.",
    )
    assert first["lead"].service == "exterior painting"
    assert first["lead"].city == "San Mateo"
    assert first["lead"].project_scope == "front exterior only"
    assert first["lead"].trim is True

    second = handle_message(
        "scenario_correction",
        "Actually sorry, it’s not the outside. It’s the inside hallway and two bedrooms in a rental unit in Daly City.",
    )
    lead = second["lead"]
    assert lead.service == "interior painting"
    assert lead.city == "Daly City"
    assert lead.rooms == 2
    assert lead.property_type == "rental"
    assert lead.project_scope == "hallway and bedrooms"
    assert lead.trim is None
    assert lead.timeline is None
    assert lead.urgency is None
    assert lead.repairs_needed is None
    assert "front exterior" not in (lead.project_scope or "").lower()
    assert "May I get your name" in second["reply"]


def test_interior_to_exterior_correction_preserves_city_and_clears_rooms():
    reset_session("scenario_interior_to_exterior")
    first = handle_message("scenario_interior_to_exterior", "I need two bedrooms painted in San Bruno.")
    assert first["lead"].city == "San Bruno"
    assert first["lead"].service == "interior painting"
    assert first["lead"].rooms == 2

    second = handle_message("scenario_interior_to_exterior", "Actually it’s the exterior siding, not bedrooms.")
    lead = second["lead"]
    assert lead.city == "San Bruno"
    assert lead.service == "exterior painting"
    assert lead.rooms is None
    assert "What city" not in second["reply"]
    assert "exterior" in second["reply"].lower()


def test_take_a_look_is_not_duration_question():
    reset_session("scenario_take_a_look")
    result = handle_message(
        "scenario_take_a_look",
        "I have a rental in San Mateo. The front exterior looks pretty bad, paint is peeling around the trim, and tenants are moving in next Friday. I need someone to take a look soon.",
    )
    assert result["lead"].intent != "duration_question"
    assert not result["reply"].lower().startswith("exterior timing depends")


def test_ignore_that_replaces_exterior_with_hallway_and_kitchen():
    reset_session("scenario_ignore_that")
    first = handle_message(
        "scenario_ignore_that",
        "The outside front of my rental in San Mateo needs paint before tenants move in.",
    )
    assert first["lead"].service == "exterior painting"
    assert first["lead"].city == "San Mateo"
    assert first["lead"].property_type == "rental"

    second = handle_message(
        "scenario_ignore_that",
        "Actually ignore that, it’s the inside hallway and kitchen in Daly City.",
    )
    lead = second["lead"]
    assert lead.city == "Daly City"
    assert lead.service == "interior painting"
    assert lead.project_scope == "hallway and kitchen"
    assert lead.property_type is None
    assert lead.timeline is None
    assert lead.urgency is None
    assert "hallway and kitchen" in second["reply"].lower()


def test_customer_city_vs_project_city():
    reset_session("scenario_customer_project_city")
    result = handle_message(
        "scenario_customer_project_city",
        "I live in San Jose, but the house that needs painting is in San Mateo.",
    )
    assert result["lead"].city == "San Mateo"
    assert result["lead"].city != "San Jose"


def test_mixed_scope_keeps_both_areas():
    reset_session("scenario_mixed_scope")
    result = handle_message(
        "scenario_mixed_scope",
        "We want kitchen cabinets painted, and maybe touch up the living room walls too.",
    )
    lead = result["lead"]
    assert "cabinet" in (lead.service or "")
    assert "living room" in (lead.project_scope or "")


def test_all_in_one_exterior_scope_timeline_and_stories_followup():
    reset_session("scenario_all_in_one_exterior")
    first = handle_message(
        "scenario_all_in_one_exterior",
        "Hi, I’m Andy Chen. My number is 650-555-1234. I need the exterior trim and garage door painted on a rental in San Mateo before next Friday.",
    )
    lead = first["lead"]
    assert lead.name == "Andy Chen"
    assert lead.phone == "650-555-1234"
    assert lead.city == "San Mateo"
    assert lead.service == "exterior painting"
    assert "trim" in (lead.project_scope or "")
    assert "garage door" in (lead.project_scope or "")
    assert lead.timeline == "before next friday"
    assert "How many stories" in first["reply"]

    second = handle_message("scenario_all_in_one_exterior", "two")
    assert second["lead"].stories == 2
    assert "how many stories" not in second["reply"].lower()


def test_short_name_reply_is_valid_backend_extraction():
    reset_session("scenario_short_name")
    handle_message("scenario_short_name", "I need two bedrooms painted in San Bruno.")
    result = handle_message("scenario_short_name", "Andy")
    assert result["lead"].name == "Andy"
    assert "phone" in result["reply"].lower()



def test_spoken_email_and_ambiguous_damage():
    reset_session("scenario_spoken_email")
    email_result = handle_message("scenario_spoken_email", "My email is andy dot chen at gmail dot com.")
    assert email_result["lead"].email == "andy.chen@gmail.com"

    reset_session("scenario_ambiguous_damage")
    damage_result = handle_message(
        "scenario_ambiguous_damage",
        "The paint is bubbling near the window and I’m not sure what needs to be done.",
    )
    assert damage_result["lead"].repairs_needed is True
    assert "interior wall" in damage_result["reply"].lower()
    assert "outside" in damage_result["reply"].lower()


def test_availability_request_captures_preferred_time_safely():
    reset_session("scenario_availability_time")
    result = handle_message(
        "scenario_availability_time",
        "Can someone come tomorrow morning at 8? I need this done before my open house.",
    )
    assert result["lead"].preferred_callback_time == "tomorrow morning at 8"
    assert result["lead"].timeline == "before open house"
    assert result["lead"].urgency == "urgent"
    assert "confirm" in result["reply"].lower()
    assert not result["reply"].lower().startswith("yes")



def test_impossible_price_and_same_day_request_is_safe():
    reset_session("scenario_impossible_price")
    result = handle_message(
        "scenario_impossible_price",
        "Can you paint my entire house tonight for under $200?",
    )
    reply = result["reply"].lower()
    lead = result["lead"]
    assert lead.intent == "unsafe_price_schedule_request"
    assert lead.service == "painting project"
    assert lead.property_type == "house"
    assert lead.timeline == "tonight"
    assert lead.urgency == "urgent"
    assert "can’t promise" in reply or "can't promise" in reply
    assert "same-day" in reply or "same day" in reply
    assert "$200" not in reply



def test_reasoner_debug_metadata_for_rule_and_complex_turns():
    reset_session("scenario_reasoner_rule")
    simple = handle_message("scenario_reasoner_rule", "650-555-1234")
    assert simple["metrics"]["reasoner_source"] == "rule"
    assert simple["metrics"]["reasoner_used"] is False

    reset_session("scenario_reasoner_complex")
    complex_result = handle_message(
        "scenario_reasoner_complex",
        "We want kitchen cabinets painted and maybe touch up the living room walls too.",
    )
    assert complex_result["metrics"]["reasoner_used"] is True
    assert complex_result["metrics"]["reasoner_source"] in {"heuristic", "llm"}
    assert complex_result["metrics"]["reasoner_trigger"] in {
        "mixed_scope_or_project_city_signal",
        "descriptive_messy_turn_enrichment",
        "no_rule_extraction_from_nontrivial_turn",
    }


def test_complex_semantic_turn_requires_llm_and_does_not_fake_name():
    reset_session("scenario_complex_semantic_llm_required")
    result = handle_message(
        "scenario_complex_semantic_llm_required",
        "I’m trying to get the place ready before my sister moves in. The walls are fine except the area around the bay window is bubbling, and the kitchen cabinets look dated. I’m not sure if this is a painting job, repair job, or both. The property is in San Mateo, but I live in San Jose.",
    )
    lead = result["lead"]
    metrics = result["metrics"]
    assert lead.name is None
    assert lead.city == "San Mateo"
    assert "repair" in (lead.service or "")
    assert "cabinet" in (lead.service or "")
    assert lead.repairs_needed is True
    assert "bay window" in (lead.project_scope or "")
    assert "kitchen cabinets" in (lead.project_scope or "")
    assert lead.timeline == "before sister moves in"
    assert metrics["reasoner_used"] is True
    assert metrics["reasoner_trigger"] == "llm_required_complex_semantic_turn"
    assert "May I get your name" in result["reply"]


def test_live_mode_defers_real_llm_and_stays_fast():
    reset_session("scenario_live_mode_defers_llm")
    result = handle_message(
        "scenario_live_mode_defers_llm",
        "I’m trying to get the place ready before my sister moves in. The walls are fine except the area around the bay window is bubbling, and the kitchen cabinets look dated. I’m not sure if this is a painting job, repair job, or both. The property is in San Mateo, but I live in San Jose.",
        live_mode=True,
    )
    metrics = result["metrics"]
    assert metrics["reasoner_used"] is True
    assert metrics["reasoner_trigger"] == "llm_required_complex_semantic_turn"
    assert metrics["reasoner_source"] == "heuristic"
    assert metrics["llm_deferred"] is True
    assert metrics["live_mode"] is True
    assert metrics["reasoner_latency_ms"] < 100
    assert "possible repair, painting, and cabinet work" in result["reply"]


def test_smart_mode_is_allowed_to_call_real_llm_or_fallback():
    reset_session("scenario_smart_mode_can_block")
    result = handle_message(
        "scenario_smart_mode_can_block",
        "We want kitchen cabinets painted and maybe touch up the living room walls too.",
        live_mode=False,
    )
    metrics = result["metrics"]
    assert metrics["reasoner_used"] is True
    assert metrics["live_mode"] is False
    assert metrics["llm_deferred"] is False
    assert metrics["reasoner_source"] in {"heuristic", "llm"}



def test_post_call_cleanup_sanitizer_latest_correction_wins():
    from app.models import LeadInfo
    from app.llm_extractor import extract_lead_patch
    from app.receptionist import apply_lead_patch, score_lead

    stale_lead = LeadInfo(
        city="San Mateo",
        service="interior painting",
        project_scope="hallway and kitchen",
        repairs_needed=True,
    )
    cleanup_message = """Post-call cleanup. Review transcript.

Transcript:
Customer: I may have explained this wrong earlier. My mom is moving into the place soon, but I’m not sure if we need repainting, repair, or cabinets. The main issue is the paint bubbling under the bay window, some old water staining near the kitchen ceiling, and the cabinet doors look worn. I live in San Jose, but the property is in San Mateo. I don’t need the whole place painted, just whatever needs to be fixed before she moves in.
AI Receptionist: Got it — possible repair, painting, and cabinet work. May I get your name?
Customer: Actually forget what I said about bedrooms. It’s not really a repaint job. The tenant moved out and now there are scuffs along the hallway, a small patch of bubbling paint near the window, and the front door needs to look better before showings next week. The property is in Daly City, but I’m calling from San Bruno.
AI Receptionist: Got it — interior painting for hallway and bedrooms. May I get your name?"""
    patch = extract_lead_patch(cleanup_message, stale_lead, use_llm=False, reasoner_mode="post_call_cleanup")
    cleaned = score_lead(apply_lead_patch(stale_lead, patch))

    assert cleaned.city == "Daly City"
    assert cleaned.service == "interior touch-up + paint repair + door painting"
    assert cleaned.property_type == "rental"
    assert cleaned.repairs_needed is True
    assert isinstance(cleaned.repairs_needed, bool)
    assert "hallway wall scuffs" in (cleaned.project_scope or "")
    assert "bubbling paint near window" in (cleaned.project_scope or "")
    assert "front door" in (cleaned.project_scope or "")
    assert cleaned.timeline == "before showings next week"
    assert cleaned.urgency == "urgent"
    assert cleaned.lead_priority == "Hot"
    assert "hallway and kitchen" not in (cleaned.project_scope or "")



def test_live_correction_city_before_rental_not_service_area():
    reset_session("scenario_city_before_rental")
    handle_message(
        "scenario_city_before_rental",
        "I may have explained this wrong earlier. My mom is moving into the place soon, but I’m not sure if we need repainting, repair, or cabinets. The main issue is the paint bubbling under the bay window, some old water staining near the kitchen ceiling, and the cabinet doors look worn. I live in San Jose, but the property is in San Mateo. I don’t need the whole place painted, just whatever needs to be fixed before she moves in.",
        live_mode=True,
    )
    result = handle_message(
        "scenario_city_before_rental",
        "Actually forget that first version. The tenant moved out, and what we really need is touch-up work in the Daly City rental: scuffs along the hallway walls, bubbling paint near the front window, and the front door painted before showings next week. I’m calling from San Bruno.",
        live_mode=True,
    )
    lead = result["lead"]
    assert lead.city == "Daly City"
    assert lead.city != "San Mateo"
    assert lead.intent != "service_area_question"
    assert lead.service == "interior touch-up + paint repair + door painting"
    assert lead.property_type == "rental"
    assert lead.repairs_needed is True
    assert lead.timeline == "before showings next week"
    assert lead.urgency == "urgent"
    assert "touch-up and paint repair" in result["reply"].lower()

def test_mixed_front_door_and_hallway_touchups():
    reset_session("scenario_mixed_inside_outside_v11")
    result = handle_message(
        "scenario_mixed_inside_outside_v11",
        "I need the front door painted outside, but also some hallway touch-ups inside. It’s a rental in San Mateo and I’d like it done before new tenants arrive next weekend.",
        live_mode=True,
    )
    lead = result["lead"]
    assert lead.city == "San Mateo"
    assert lead.service == "exterior door painting + interior touch-up"
    assert lead.project_scope == "front door and hallway touch-ups"
    assert lead.property_type == "rental"
    assert lead.timeline == "before new tenants arrive next weekend"
    assert lead.urgency == "urgent"
    assert lead.lead_priority == "Hot"
    assert "stories" not in result["reply"].lower()
    assert result["metrics"]["reasoner_trigger"] == "mixed_inside_outside_scope_signal"


def test_landlord_unit_uncertain_repair():
    reset_session("scenario_landlord_unit_uncertain_repair")
    result = handle_message(
        "scenario_landlord_unit_uncertain_repair",
        "I’m calling for my landlord. The unit is in Foster City. There are stains on the ceiling and the bathroom paint is peeling. I’m not sure what he wants done yet.",
        live_mode=True,
    )
    lead = result["lead"]
    assert lead.city == "Foster City"
    assert lead.property_type == "rental"
    assert lead.service == "paint repair / repainting"
    assert lead.repairs_needed is True
    assert "ceiling stains" in (lead.project_scope or "")
    assert "bathroom peeling paint" in (lead.project_scope or "")


def test_commercial_schedule_constraint():
    reset_session("scenario_commercial_schedule_constraint")
    result = handle_message(
        "scenario_commercial_schedule_constraint",
        "We run a dental office in Burlingame and need the waiting room painted, but only after 6pm or over the weekend because patients are there during the day.",
        live_mode=True,
    )
    lead = result["lead"]
    assert lead.city == "Burlingame"
    assert lead.property_type == "commercial office"
    assert lead.service == "interior painting"
    assert lead.project_scope == "waiting room"
    assert lead.preferred_callback_time in {"after 6pm or weekend", "after 6pm or over the weekend"}


def test_city_regex_does_not_capture_and():
    reset_session("scenario_city_no_and")
    result = handle_message(
        "scenario_city_no_and",
        "I need painting in San Mateo and I want it done soon.",
        live_mode=True,
    )
    assert result["lead"].city == "San Mateo"
    assert result["lead"].city != "San Mateo And"

# v12 regression: price-comparison questions involving bubbling paint should not
# get stuck in a repeated diagnostic question, and "whole room" should not set
# ceiling/trim scope flags when it is only mentioned as a comparison.
def test_price_comparison_bubbling_repair_safe_reply():
    reset_session("scenario_price_comparison_bubbling_v12")
    result = handle_message(
        "scenario_price_comparison_bubbling_v12",
        "I don’t know exactly what I need, but can you tell me if fixing bubbling paint and repainting one wall would be cheaper than repainting the whole room?",
        live_mode=True,
    )
    lead = result["lead"]
    reply = result["reply"].lower()
    assert lead.intent == "price_question"
    assert lead.service == "paint repair / repainting"
    assert lead.repairs_needed is True
    assert lead.project_scope == "bubbling paint and one wall"
    assert lead.ceiling is None
    assert lead.trim is None
    assert lead.walls_only is None
    assert "pricing depends" in reply
    assert "what city" in reply
    assert "interior wall or on the outside" not in reply


# v13 regression: "bubbling under one window" is a location, not a price
# request just because it contains the word "under". It should capture the
# urgent repair timeline and avoid the price-script reply.
def test_moisture_under_window_not_price_question():
    reset_session("scenario_moisture_under_window_v13")
    result = handle_message(
        "scenario_moisture_under_window_v13",
        "The last painter said it might be moisture, not just paint. There’s bubbling under one window, a stain near the ceiling, and I’m not sure whether this needs repair first or just repainting. It’s for a rental in Daly City and I need it presentable before the next showing.",
        live_mode=True,
    )
    lead = result["lead"]
    reply = result["reply"].lower()
    assert lead.intent != "price_question"
    assert lead.city == "Daly City"
    assert lead.property_type == "rental"
    assert lead.service == "paint repair / repainting"
    assert lead.repairs_needed is True
    assert "bubbling" in (lead.project_scope or "")
    assert "ceiling" in (lead.project_scope or "")
    assert lead.timeline in {"before next showing", "before the next showing", "next showing"}
    assert lead.urgency == "urgent"
    assert lead.lead_priority == "Hot"
    assert "pricing depends" not in reply
    assert "may i get your name" in reply


# v14 regression: explicit budget/under-$ questions must be treated as safe
# price intent, not as a normal interior lead with a generated dollar estimate.
def test_under_500_bedroom_budget_is_safe_price_question():
    reset_session("scenario_under_500_budget_v14")
    result = handle_message(
        "scenario_under_500_budget_v14",
        "Can you repaint one bedroom for under $500 if I already bought the paint?",
        live_mode=True,
    )
    lead = result["lead"]
    reply = result["reply"].lower()
    assert lead.intent == "price_question"
    assert lead.service == "interior painting"
    assert lead.rooms == 1
    assert lead.project_scope == "bedroom"
    assert "pricing depends" in reply
    assert "reliable estimate" in reply
    assert "$500" not in reply
    assert "rough price range" not in reply
    assert result["metrics"]["reasoner_used"] is True
    assert result["metrics"]["reasoner_trigger"] == "price_or_budget_signal"



def test_incomplete_phone_prioritizes_full_callback_number():
    reset_session("scenario_incomplete_phone")
    result = handle_message("scenario_incomplete_phone", "My number is 650-555 and I need my hallway painted in San Mateo.")
    lead = result["lead"]
    assert lead.city == "San Mateo"
    assert lead.service == "interior painting"
    assert lead.project_scope == "hallway"
    assert lead.phone is None
    assert lead.intent == "incomplete_phone"
    assert "full phone number" in result["reply"].lower()


def test_assistant_echo_is_ignored_and_does_not_mutate_lead():
    session = "scenario_echo_guard_v16"
    reset_session(session)
    first = handle_message(session, "My number is 650-555 and I need my hallway painted in San Mateo.")
    assert first["lead"].city == "San Mateo"
    assert first["lead"].project_scope == "hallway"
    assert first["lead"].phone is None
    # Simulate browser voice pickup/manual copy-paste of the AI's own reply.
    second = handle_message(session, first["reply"])
    assert second["metrics"]["reasoner_source"] == "echo_guard"
    assert second["metrics"]["reasoner_trigger"] == "assistant_echo_ignored"
    assert second["lead"].city == "San Mateo"
    assert second["lead"].project_scope == "hallway"
    assert second["lead"].phone is None
    assert second["reply"] == first["reply"]

# v17 regression: the Personality Talker should add warmth/personality while
# preserving deterministic safety and one-question-at-a-time behavior.
def test_personality_talker_adds_warmth_without_extra_questions():
    reset_session("scenario_personality_talker_v17")
    result = handle_message(
        "scenario_personality_talker_v17",
        "The last painter said it might be moisture, not just paint. There’s bubbling under one window, a stain near the ceiling, and I’m not sure whether this needs repair first or just repainting. It’s for a rental in Daly City and I need it presentable before the next showing.",
        live_mode=True,
    )
    reply = result["reply"]
    assert result["metrics"].get("talker_source") in {"template", "template_preserved", "llm", "disabled"}
    assert reply.count("?") <= 1
    assert "$" not in reply
    assert "May I get your name" in reply
    assert any(phrase in reply.lower() for phrase in ["worth flagging", "possible paint repair", "prep or repair"])


def test_personality_talker_preserves_safe_price_language():
    reset_session("scenario_personality_price_v17")
    result = handle_message(
        "scenario_personality_price_v17",
        "Can you repaint one bedroom for under $500 if I already bought the paint?",
        live_mode=True,
    )
    reply = result["reply"].lower()
    assert result["lead"].intent == "price_question"
    assert "pricing depends" in reply
    assert "reliable estimate" in reply
    assert "$500" not in reply
    assert result["reply"].count("?") <= 1


def test_vague_paint_request_clarifies_service_before_city():
    reset_session("scenario_vague_paint_v18")
    result = handle_message("scenario_vague_paint_v18", "I just need help with paint.", live_mode=True)
    lead = result["lead"]
    reply = result["reply"].lower()
    assert lead.service == "painting project"
    assert lead.city is None
    assert "interior, exterior, cabinets, or touch-up" in reply
    assert "what city" not in reply
    assert result["missing_fields"][0] == "service"
    assert result["metrics"].get("talker_source") == "template"


def run_all_tests():
    tests = [
        test_exterior_rental_lead,
        test_interior_price_flow,
        test_visible_first_reply_and_safe_price_question,
        test_handoff_request_saves_minimum,
        test_no_repeated_project_summary_during_contact_collection,
        test_correction_clears_stale_exterior_fields,
        test_interior_to_exterior_correction_preserves_city_and_clears_rooms,
        test_take_a_look_is_not_duration_question,
        test_ignore_that_replaces_exterior_with_hallway_and_kitchen,
        test_customer_city_vs_project_city,
        test_mixed_scope_keeps_both_areas,
        test_all_in_one_exterior_scope_timeline_and_stories_followup,
        test_short_name_reply_is_valid_backend_extraction,
        test_spoken_email_and_ambiguous_damage,
        test_availability_request_captures_preferred_time_safely,
        test_impossible_price_and_same_day_request_is_safe,
        test_complex_semantic_turn_requires_llm_and_does_not_fake_name,
        test_reasoner_debug_metadata_for_rule_and_complex_turns,
        test_live_mode_defers_real_llm_and_stays_fast,
        test_smart_mode_is_allowed_to_call_real_llm_or_fallback,
        test_post_call_cleanup_sanitizer_latest_correction_wins,
        test_live_correction_city_before_rental_not_service_area,
        test_mixed_front_door_and_hallway_touchups,
        test_landlord_unit_uncertain_repair,
        test_commercial_schedule_constraint,
        test_city_regex_does_not_capture_and,
        test_price_comparison_bubbling_repair_safe_reply,
        test_moisture_under_window_not_price_question,
        test_under_500_bedroom_budget_is_safe_price_question,
        test_incomplete_phone_prioritizes_full_callback_number,
        test_assistant_echo_is_ignored_and_does_not_mutate_lead,
        test_personality_talker_adds_warmth_without_extra_questions,
        test_personality_talker_preserves_safe_price_language,
        test_vague_paint_request_clarifies_service_before_city,
    ]
    for test in tests:
        test()
    print("All receptionist scenario tests passed.")


if __name__ == "__main__":
    run_all_tests()




def test_multiple_city_sentence_does_not_crash_or_use_wrong_city():
    reset_session("scenario_multiple_city_v21")
    result = handle_message(
        "scenario_multiple_city_v21",
        "I used to live in Daly City, I’m currently in San Jose, but the house that needs painting is in San Bruno.",
    )
    assert result["lead"].city == "San Bruno"
    assert result["lead"].property_type == "house"
    assert "Invalid request" not in result["reply"]


def test_llm_type_sanitize_protects_response_schema():
    from app.models import LeadInfo
    from app.receptionist import sanitize_lead_schema

    lead = LeadInfo()
    lead.city = ["San Bruno"]  # type: ignore[assignment]
    lead.service = {"kind": "exterior painting"}  # type: ignore[assignment]
    lead.repairs_needed = "bubbling paint around window"  # type: ignore[assignment]
    lead.rooms = "two bedrooms"  # type: ignore[assignment]
    lead.notes = "LLM returned a single note"  # type: ignore[assignment]

    lead = sanitize_lead_schema(lead)
    assert lead.city == "San Bruno"
    assert isinstance(lead.service, str)
    assert lead.repairs_needed is True
    assert lead.rooms is None or isinstance(lead.rooms, int)
    assert isinstance(lead.notes, list)
