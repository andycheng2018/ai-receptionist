<<<<<<< HEAD

# Imports BaseModel from Pydantic
# Validate incoming request data, organize response data, generate API docs
from pydantic import BaseModel
# Imports type hints
# Optional[str] means this value can be string, or it can be missing/None
# List[str] means this is a list of strings
from typing import Optional, List
from pydantic import Field

# What customer sends to the API
class CustomerMessage(BaseModel):
    message: str
    session_id: Optional[str] = "default" # tracks conversation
# If 2 customers call at the same time, we want seperate memory


# Stores all the info we collect from the customer
class LeadInfo(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    service: Optional[str] = None
=======
from pydantic import BaseModel, Field
from typing import Optional, List, Any


class CustomerMessage(BaseModel):
    """What the customer sends to the API."""
    message: str
    session_id: Optional[str] = "default"
    # True = phone-call mode: never block the reply on a slow real LLM call.
    # False = smart web-chat mode: allow real LLM calls before replying.
    live_mode: Optional[bool] = True


class LeadInfo(BaseModel):
    """Structured information collected during the receptionist conversation."""
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    service: Optional[str] = None

    # Interior/simple estimate fields
>>>>>>> 3895666 (Deploy AI receptionist)
    room_size_sqft: Optional[int] = None
    rooms: Optional[int] = None
    walls_only: Optional[bool] = None
    ceiling: Optional[bool] = None
    trim: Optional[bool] = None
<<<<<<< HEAD
    timeline: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
    saved: bool = False
    intent: Optional[str] = None

# What the API sends back
class ReceptionistResponse(BaseModel):
    reply: str
    lead: LeadInfo # Customer info (for debugging)
    missing_fields: List[str] # Whether the lead is complete enough to send to painter, or if we need more info
    ready_to_send_to_painter: bool # Whether the lead is complete enough to send to painter
=======

    # Painting-company specific qualifier fields
    property_type: Optional[str] = None  # house, condo, apartment, business, rental
    stories: Optional[int] = None
    occupied: Optional[bool] = None
    repairs_needed: Optional[bool] = None
    photos_available: Optional[bool] = None
    cabinet_count: Optional[int] = None
    project_scope: Optional[str] = None  # whole home, full exterior, front only, touch-up, etc.

    # Lead operations fields
    timeline: Optional[str] = None
    urgency: Optional[str] = None  # emergency, urgent, soon, flexible
    preferred_callback_time: Optional[str] = None
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
    metrics: dict[str, Any] | None = None
>>>>>>> 3895666 (Deploy AI receptionist)
