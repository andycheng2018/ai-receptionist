
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
    room_size_sqft: Optional[int] = None
    rooms: Optional[int] = None
    walls_only: Optional[bool] = None
    ceiling: Optional[bool] = None
    trim: Optional[bool] = None
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