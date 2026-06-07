from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from app.models import CustomerMessage
from app.llm_receptionist import (
    handle_message,
    get_lead,
    build_conversation_summary,
    get_background_reasoner_status,
    llm_available,
)
from app.database import (
    init_db,
    get_all_call_records,
    get_all_leads,
    update_lead_status,
)
from app.voice import router as voice_router
from app.tts import router as tts_router


app = FastAPI(title="AI Receptionist Prototype")


# Serve frontend/static files.
# For a prototype, directory="." is okay.
# Later, you can change this to directory="app/static" for tighter control.
app.mount("/static", StaticFiles(directory="."), name="static")


# Initialize SQLite/database tables on startup.
init_db()


# Add Twilio voice routes and TTS routes.
app.include_router(voice_router)
app.include_router(tts_router)


# Allow browser frontend to call the backend.
# For production, restrict allow_origins to your actual frontend domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    """Serve the main web demo page."""
    return FileResponse("index.html")


@app.get("/health")
def health_check():
    """Simple backend health check."""
    return {
        "status": "ok",
        "service": "AI Receptionist Prototype",
        "llm_configured": llm_available(),
    }


@app.post("/chat")
def chat(req: CustomerMessage):
    """Main chat endpoint for the AI receptionist.

    The frontend sends a customer message here.
    The receptionist returns:
    - reply
    - updated lead
    - missing fields
    - readiness status
    - metrics
    """
    try:
        message = clean_message(req.message)

        return handle_message(
            session_id=req.session_id or "default",
            message=message,
        )

    except Exception as exc:
        return JSONResponse(
            status_code=200,
            content={
                "reply": "Sorry — I had trouble processing that. Could you say that one more time?",
                "lead": {},
                "missing_fields": [],
                "ready_to_send_to_painter": False,
                "handoff_required": False,
                "final_call_json": None,
                "metrics": {
                    "reasoner_source": "server_error_fallback",
                    "reasoner_trigger": "chat_exception_caught",
                    "reasoner_used": False,
                    "error": str(exc)[:300],
                },
            },
        )


@app.get("/sessions/{session_id}/lead")
def get_session_lead(session_id: str):
    """Return the current in-memory lead for a session."""
    lead = get_lead(session_id)

    return {
        "lead": lead,
        "summary": build_conversation_summary(lead),
        "background_reasoner_status": get_background_reasoner_status(session_id),
        "llm_configured": llm_available(),
    }


@app.get("/leads")
def get_leads():
    """Return all saved leads for the dashboard."""
    return get_all_leads()


@app.get("/call-records")
def get_call_records():
    """Return saved call records and transcripts."""
    return get_all_call_records()


@app.post("/leads/{lead_id}/status")
def update_status(lead_id: int, payload: dict):
    """Update a lead's dashboard status."""
    status = payload.get("status")
    result = update_lead_status(lead_id, status)

    return {
        "success": True,
        "lead_id": result["lead_id"],
        "status": result["status"],
    }


def clean_message(message: str | None) -> str:
    """Normalize common smart quotes from browser/voice input."""
    return (
        (message or "")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .strip()
    )