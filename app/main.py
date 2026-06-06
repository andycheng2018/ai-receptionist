from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from app.models import CustomerMessage
from app.receptionist import handle_message, get_lead, get_transcript, apply_lead_patch, score_lead, build_conversation_summary, get_background_reasoner_status
from app.llm_extractor import extract_lead_patch, llm_available
from app.database import init_db, get_all_call_records, get_all_leads, update_lead_status
from app.voice import router as voice_router
from app.tts import router as tts_router


app = FastAPI(title="AI Receptionist Prototype")

# Serve files inside app/static at /static/...
app.mount("/static", StaticFiles(directory="."), name="static")

init_db()

app.include_router(voice_router)
app.include_router(tts_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    return FileResponse("index.html")

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "AI Receptionist Prototype"
    }


@app.post("/chat")
def chat(req: CustomerMessage):
    """Chat endpoint.

    v21 intentionally does not use a strict response_model here because LLM
    outputs can occasionally produce type noise before sanitation. The handler
    sanitizes the lead and returns a JSON-safe payload; if something still goes
    wrong, return a graceful demo-safe response instead of a browser-level
    "Invalid request".
    """
    try:
        message = (req.message or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
        result = handle_message(req.session_id or "default", message, live_mode=req.live_mode)
        return result
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
    """Return current in-memory lead plus background AI status for live demos."""
    lead = get_lead(session_id)
    return {
        "lead": lead,
        "summary": build_conversation_summary(lead),
        "background_reasoner_status": get_background_reasoner_status(session_id),
        "llm_configured": llm_available(),
    }


@app.post("/sessions/{session_id}/ai-cleanup")
def ai_cleanup(session_id: str):
    """Run a slower real-LLM cleanup pass after/during the call.

    This is intentionally separate from /chat so the live phone reply is not
    blocked by a multi-second model call.
    """
    if not llm_available():
        return {"success": False, "error": "Real AI/LLM is not configured. Set OPENAI_API_KEY or an OpenAI-compatible Qwen endpoint."}

    lead = get_lead(session_id)
    transcript_text = "\n".join(f"{m.get('speaker')}: {m.get('text')}" for m in get_transcript(session_id))
    message = (
        "Post-call cleanup. Review this painting-receptionist transcript and the current lead. "
        "Return a structured patch that fixes stale fields, fills missing obvious fields, and keeps contact info.\n\n"
        f"Transcript:\n{transcript_text}"
    )
    patch = extract_lead_patch(message, lead, use_llm=True, reasoner_mode="post_call_cleanup")
    lead = apply_lead_patch(lead, patch)
    lead = score_lead(lead)
    return {
        "success": True,
        "lead": lead,
        "summary": build_conversation_summary(lead),
        "patch": patch,
        "llm_configured": True,
    }


@app.get("/leads")
def get_leads():
    return get_all_leads()


@app.get("/call-records")
def get_call_records():
    return get_all_call_records()


@app.post("/leads/{lead_id}/status")
def update_status(lead_id: int, payload: dict):
    status = payload.get("status")
    result = update_lead_status(lead_id, status)

    return {
        "success": True,
        "lead_id": result["lead_id"],
        "status": result["status"]
    }