from pydantic import BaseModel, Field
from typing import Optional, List, Any


class CustomerMessage(BaseModel):
    """What the customer sends to the API."""
    message: str
    session_id: Optional[str] = "default"


class LeadInfo(BaseModel):
    """Structured information collected during the receptionist conversation."""
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    service: Optional[str] = None

    # Interior/simple estimate fields
    room_size_sqft: Optional[int] = None
    rooms: Optional[int] = None
    walls_only: Optional[bool] = None
    ceiling: Optional[bool] = None
    trim: Optional[bool] = None

    # Painting-company specific qualifier fields
    property_type: Optional[str] = None  # house, condo, apartment, business, rental
    stories: Optional[int] = None
    occupied: Optional[bool] = None
    repairs_needed: Optional[bool] = None
    cabinet_count: Optional[int] = None
    project_scope: Optional[str] = None  # whole home, full exterior, front only, touch-up, etc.

    # Lead operations fields
    timeline: Optional[str] = None
    urgency: Optional[str] = None  # urgent, soon, flexible
    preferred_callback_time: Optional[str] = None
    # Optional only: useful if the customer volunteers it, never required.
    photos_available: Optional[bool] = None
    lead_score: Optional[int] = None
    lead_priority: Optional[str] = None  # Hot, Warm, Normal

    notes: List[str] = Field(default_factory=list)
    saved: bool = False
    intent: Optional[str] = None
    handoff_required: bool = False


class ReceptionistResponse(BaseModel):
    reply: str
    lead: LeadInfo
    missing_fields: list[str]
    ready_to_send_to_painter: bool
    handoff_required: bool = False
    final_call_json: dict[str, Any] | None = None
    should_end: bool = False
    metrics: dict[str, Any] | None = None