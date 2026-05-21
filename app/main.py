from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.models import CustomerMessage, ReceptionistResponse
from app.receptionist import handle_message
from app.database import init_db, get_all_leads, update_lead_status
from app.voice import router as voice_router


app = FastAPI(title="AI Receptionist Prototype")

init_db()

app.include_router(voice_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "AI Receptionist Prototype"
    }


@app.post("/chat", response_model=ReceptionistResponse)
def chat(req: CustomerMessage):
    return handle_message(req.session_id or "default", req.message)


@app.get("/leads")
def get_leads():
    return get_all_leads()


@app.post("/leads/{lead_id}/status")
def update_status(lead_id: int, payload: dict):
    status = payload.get("status")
    result = update_lead_status(lead_id, status)

    return {
        "success": True,
        "lead_id": result["lead_id"],
        "status": result["status"]
    }