import json
import sqlite3
from pathlib import Path
from datetime import datetime
<<<<<<< HEAD
=======
from typing import Any
>>>>>>> 3895666 (Deploy AI receptionist)

from app.models import LeadInfo


DB_PATH = Path("leads.db")


def get_connection():
<<<<<<< HEAD
    """
    Opens a connection to the SQLite database.
    leads.db will be created automatically if it does not exist.
    """
    return sqlite3.connect(DB_PATH)


def add_column_if_missing(column_name: str, column_definition: str):
    """
    Adds a column to the leads table only if it does not already exist.

    This is useful because SQLite does not automatically update old tables
    when you change the CREATE TABLE statement.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(leads)")
    existing_columns = [row[1] for row in cursor.fetchall()]

    if column_name not in existing_columns:
        cursor.execute(
            f"ALTER TABLE leads ADD COLUMN {column_name} {column_definition}"
        )
        conn.commit()

=======
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
>>>>>>> 3895666 (Deploy AI receptionist)
    conn.close()


def init_db():
<<<<<<< HEAD
    """
    Creates the leads table if it does not already exist.

    Also runs small migrations for columns added later, such as status.
    """
    conn = get_connection()
    cursor = conn.cursor()

=======
    """Create tables and run lightweight migrations for local development."""
    conn = get_connection()
    cursor = conn.cursor()
>>>>>>> 3895666 (Deploy AI receptionist)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            name TEXT,
            phone TEXT,
<<<<<<< HEAD
=======
            email TEXT,
            address TEXT,
>>>>>>> 3895666 (Deploy AI receptionist)
            city TEXT,
            service TEXT,
            room_size_sqft INTEGER,
            rooms INTEGER,
            walls_only INTEGER,
            ceiling INTEGER,
            trim INTEGER,
<<<<<<< HEAD
            timeline TEXT,
            notes TEXT,
            intent TEXT,
=======
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
>>>>>>> 3895666 (Deploy AI receptionist)
            status TEXT DEFAULT 'New',
            created_at TEXT
        )
        """
    )
<<<<<<< HEAD

    conn.commit()
    conn.close()

    # Migration support for older leads.db files.
    add_column_if_missing("status", "TEXT DEFAULT 'New'")


def bool_to_int(value):
    """
    Converts Python booleans into SQLite-friendly values.

    True  -> 1
    False -> 0
    None  -> None
    """
    if value is None:
        return None

=======
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
>>>>>>> 3895666 (Deploy AI receptionist)
    return 1 if value else 0


def int_to_bool(value):
<<<<<<< HEAD
    """
    Converts SQLite integer booleans back into Python booleans.

    1    -> True
    0    -> False
    None -> None
    """
    if value is None:
        return None

    return bool(value)


def safe_parse_notes(notes_text):
    """
    Converts notes stored as JSON text back into a Python list.

    If notes are missing or corrupted, return an empty list.
    """
    if not notes_text:
        return []

    try:
        parsed = json.loads(notes_text)

        if isinstance(parsed, list):
            return parsed

        return [str(parsed)]

    except json.JSONDecodeError:
        return []


def save_lead_to_db(session_id: str, lead: LeadInfo):
    """
    Saves a completed lead to SQLite.

    New leads always start with status = 'New'.
    """
    init_db()

    conn = get_connection()
    cursor = conn.cursor()
=======
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
>>>>>>> 3895666 (Deploy AI receptionist)

    cursor.execute(
        """
        INSERT INTO leads (
<<<<<<< HEAD
            session_id,
            name,
            phone,
            city,
            service,
            room_size_sqft,
            rooms,
            walls_only,
            ceiling,
            trim,
            timeline,
            notes,
            intent,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
=======
            session_id, name, phone, email, address, city, service,
            room_size_sqft, rooms, walls_only, ceiling, trim,
            property_type, stories, occupied, repairs_needed, photos_available,
            cabinet_count, project_scope, timeline, urgency, preferred_callback_time,
            lead_score, lead_priority, notes, intent, handoff_required,
            final_call_json, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
>>>>>>> 3895666 (Deploy AI receptionist)
        """,
        (
            session_id,
            lead.name,
            lead.phone,
<<<<<<< HEAD
=======
            lead.email,
            lead.address,
>>>>>>> 3895666 (Deploy AI receptionist)
            lead.city,
            lead.service,
            lead.room_size_sqft,
            lead.rooms,
            bool_to_int(lead.walls_only),
            bool_to_int(lead.ceiling),
            bool_to_int(lead.trim),
<<<<<<< HEAD
            lead.timeline,
            json.dumps(lead.notes),
            lead.intent,
=======
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
>>>>>>> 3895666 (Deploy AI receptionist)
            "New",
            datetime.utcnow().isoformat(),
        ),
    )
<<<<<<< HEAD

=======
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
>>>>>>> 3895666 (Deploy AI receptionist)
    conn.commit()
    conn.close()


def get_all_leads():
<<<<<<< HEAD
    """
    Returns all saved leads as a list of dictionaries.
    Latest leads appear first.
    """
    init_db()

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM leads
        ORDER BY created_at DESC
        """
    )

=======
    init_db()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM leads ORDER BY created_at DESC")
>>>>>>> 3895666 (Deploy AI receptionist)
    rows = cursor.fetchall()
    conn.close()

    leads = []
<<<<<<< HEAD

    for row in rows:
        lead = dict(row)

        lead["notes"] = safe_parse_notes(lead.get("notes"))

        lead["walls_only"] = int_to_bool(lead.get("walls_only"))
        lead["ceiling"] = int_to_bool(lead.get("ceiling"))
        lead["trim"] = int_to_bool(lead.get("trim"))

        if not lead.get("status"):
            lead["status"] = "New"

        leads.append(lead)

    return leads


def update_lead_status(lead_id: int, status: str):
    """
    Updates a lead's status.

    Allowed status flow:
    New -> Contacted -> Scheduled -> Closed / Lost
    """
    allowed_statuses = {"New", "Contacted", "Scheduled", "Closed", "Lost"}

    if status not in allowed_statuses:
        raise ValueError(f"Invalid status: {status}")

    init_db()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE leads
        SET status = ?
        WHERE id = ?
        """,
        (status, lead_id),
    )

    conn.commit()

    updated_rows = cursor.rowcount
    conn.close()

    if updated_rows == 0:
        raise ValueError(f"No lead found with id: {lead_id}")

    return {
        "lead_id": lead_id,
        "status": status,
    }


def delete_all_leads():
    """
    Optional helper for development/testing.

    This deletes all leads from the database.
    Do not expose this in production unless protected.
    """
    init_db()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM leads")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'leads'")

    conn.commit()
    conn.close()
=======
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
>>>>>>> 3895666 (Deploy AI receptionist)
