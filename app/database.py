import json
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Any

from app.models import LeadInfo


DB_PATH = Path("leads.db")


def get_connection():
    """Open a SQLite connection. leads.db is created automatically."""
    return sqlite3.connect(DB_PATH)


def add_column_if_missing(table_name: str, column_name: str, column_definition: str):
    """Add a column to a SQLite table only if it does not already exist."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing_columns = [row[1] for row in cursor.fetchall()]
    if column_name not in existing_columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
        conn.commit()
    conn.close()


def init_db():
    """Create tables and run lightweight migrations for local development."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            name TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            city TEXT,
            service TEXT,
            room_size_sqft INTEGER,
            rooms INTEGER,
            walls_only INTEGER,
            ceiling INTEGER,
            trim INTEGER,
            property_type TEXT,
            stories INTEGER,
            occupied INTEGER,
            repairs_needed INTEGER,
            photos_available INTEGER,
            cabinet_count INTEGER,
            project_scope TEXT,
            timeline TEXT,
            urgency TEXT,
            preferred_callback_time TEXT,
            lead_score INTEGER,
            lead_priority TEXT,
            notes TEXT,
            intent TEXT,
            handoff_required INTEGER DEFAULT 0,
            final_call_json TEXT,
            status TEXT DEFAULT 'New',
            created_at TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS call_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            final_json TEXT,
            summary TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    lead_migrations = {
        "status": "TEXT DEFAULT 'New'",
        "email": "TEXT",
        "address": "TEXT",
        "property_type": "TEXT",
        "stories": "INTEGER",
        "occupied": "INTEGER",
        "repairs_needed": "INTEGER",
        "photos_available": "INTEGER",
        "cabinet_count": "INTEGER",
        "project_scope": "TEXT",
        "timeline": "TEXT",
        "urgency": "TEXT",
        "preferred_callback_time": "TEXT",
        "lead_score": "INTEGER",
        "lead_priority": "TEXT",
        "handoff_required": "INTEGER DEFAULT 0",
        "final_call_json": "TEXT",
    }
    for column, definition in lead_migrations.items():
        add_column_if_missing("leads", column, definition)


def bool_to_int(value):
    if value is None:
        return None
    return 1 if value else 0


def int_to_bool(value):
    if value is None:
        return None
    return bool(value)


def safe_parse_json(text: str | None, fallback: Any):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def save_lead_to_db(session_id: str, lead: LeadInfo, final_call_json: dict[str, Any] | None = None):
    """Save a completed lead to SQLite. New leads start with status = New."""
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    final_json_text = json.dumps(final_call_json, indent=2) if final_call_json else None

    cursor.execute(
        """
        INSERT INTO leads (
            session_id, name, phone, email, address, city, service,
            room_size_sqft, rooms, walls_only, ceiling, trim,
            property_type, stories, occupied, repairs_needed, photos_available,
            cabinet_count, project_scope, timeline, urgency, preferred_callback_time,
            lead_score, lead_priority, notes, intent, handoff_required,
            final_call_json, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            lead.name,
            lead.phone,
            lead.email,
            lead.address,
            lead.city,
            lead.service,
            lead.room_size_sqft,
            lead.rooms,
            bool_to_int(lead.walls_only),
            bool_to_int(lead.ceiling),
            bool_to_int(lead.trim),
            lead.property_type,
            lead.stories,
            bool_to_int(lead.occupied),
            bool_to_int(lead.repairs_needed),
            bool_to_int(lead.photos_available),
            lead.cabinet_count,
            lead.project_scope,
            lead.timeline,
            lead.urgency,
            lead.preferred_callback_time,
            lead.lead_score,
            lead.lead_priority,
            json.dumps(lead.notes),
            lead.intent,
            bool_to_int(lead.handoff_required),
            final_json_text,
            "New",
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def save_final_call_json(session_id: str, final_data: dict[str, Any]):
    """Save the full transcript + structured JSON for a completed call/chat."""
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    summary = final_data.get("conversation", {}).get("summary") if isinstance(final_data, dict) else None
    cursor.execute(
        """
        INSERT INTO call_records (session_id, final_json, summary, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, json.dumps(final_data, indent=2), summary, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_leads():
    init_db()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM leads ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()

    leads = []
    for row in rows:
        lead = dict(row)
        lead["notes"] = safe_parse_json(lead.get("notes"), [])
        lead["final_call_json"] = safe_parse_json(lead.get("final_call_json"), None)
        for field in [
            "walls_only", "ceiling", "trim", "occupied", "repairs_needed",
            "photos_available", "handoff_required",
        ]:
            lead[field] = int_to_bool(lead.get(field))
        if not lead.get("status"):
            lead["status"] = "New"
        leads.append(lead)
    return leads


def get_all_call_records():
    """Return full saved call JSON records, latest first."""
    init_db()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM call_records ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    records = []
    for row in rows:
        record = dict(row)
        record["final_json"] = safe_parse_json(record.get("final_json"), {})
        records.append(record)
    return records


def update_lead_status(lead_id: int, status: str):
    allowed_statuses = {"New", "Contacted", "Scheduled", "Closed", "Lost"}
    if status not in allowed_statuses:
        raise ValueError(f"Invalid status: {status}")
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    conn.commit()
    updated_rows = cursor.rowcount
    conn.close()
    if updated_rows == 0:
        raise ValueError(f"No lead found with id: {lead_id}")
    return {"lead_id": lead_id, "status": status}


def delete_all_leads():
    """Development helper. Do not expose in production unless protected."""
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM leads")
    cursor.execute("DELETE FROM call_records")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'leads'")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'call_records'")
    conn.commit()
    conn.close()
