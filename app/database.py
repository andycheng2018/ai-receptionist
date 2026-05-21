import json
import sqlite3
from pathlib import Path
from datetime import datetime

from app.models import LeadInfo


DB_PATH = Path("leads.db")


def get_connection():
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

    conn.close()


def init_db():
    """
    Creates the leads table if it does not already exist.

    Also runs small migrations for columns added later, such as status.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            name TEXT,
            phone TEXT,
            city TEXT,
            service TEXT,
            room_size_sqft INTEGER,
            rooms INTEGER,
            walls_only INTEGER,
            ceiling INTEGER,
            trim INTEGER,
            timeline TEXT,
            notes TEXT,
            intent TEXT,
            status TEXT DEFAULT 'New',
            created_at TEXT
        )
        """
    )

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

    return 1 if value else 0


def int_to_bool(value):
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

    cursor.execute(
        """
        INSERT INTO leads (
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
        """,
        (
            session_id,
            lead.name,
            lead.phone,
            lead.city,
            lead.service,
            lead.room_size_sqft,
            lead.rooms,
            bool_to_int(lead.walls_only),
            bool_to_int(lead.ceiling),
            bool_to_int(lead.trim),
            lead.timeline,
            json.dumps(lead.notes),
            lead.intent,
            "New",
            datetime.utcnow().isoformat(),
        ),
    )

    conn.commit()
    conn.close()


def get_all_leads():
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

    rows = cursor.fetchall()
    conn.close()

    leads = []

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