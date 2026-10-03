"""Local SQLite store for caller personas and the AI prompt.

Supabase is optional. Profiles, which persona answers inbound, which persona
places outbound calls, and the global AI prompt are saved here.
"""

import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


def db_path() -> str:
    explicit = os.getenv("DB_PATH", "").strip()
    if explicit:
        path = Path(explicit)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
    folder = Path(__file__).resolve().parent / "data"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / "voice.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_local_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS personas (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                agent_name TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'both',
                voice TEXT NOT NULL DEFAULT 'Sulafat',
                model TEXT NOT NULL DEFAULT 'gemini-3.1-flash-live-preview',
                audio_mode TEXT NOT NULL DEFAULT 'gemini',
                system_prompt TEXT NOT NULL DEFAULT '',
                enabled_tools TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'manual',
                source_ref TEXT NOT NULL DEFAULT '',
                email_theme TEXT NOT NULL DEFAULT '{}',
                prompt_vars TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS appointments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                service TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'booked',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_logs (
                id TEXT PRIMARY KEY,
                phone_number TEXT NOT NULL,
                lead_name TEXT,
                outcome TEXT NOT NULL,
                reason TEXT,
                duration_seconds INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL,
                recording_url TEXT,
                notes TEXT,
                ended_by TEXT DEFAULT 'unknown'
            );
            CREATE TABLE IF NOT EXISTS contact_memory (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                insight TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Add email_theme column to existing databases that don't have it
        try:
            conn.execute("SELECT email_theme FROM personas LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE personas ADD COLUMN email_theme TEXT NOT NULL DEFAULT '{}'")
        # Add prompt_vars column to existing databases that don't have it
        try:
            conn.execute("SELECT prompt_vars FROM personas LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE personas ADD COLUMN prompt_vars TEXT NOT NULL DEFAULT '{}'")
    print(f"Local persona database ready at {db_path()}")


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    import json as _json
    if not row:
        return None
    d = dict(row)
    # Parse email_theme from JSON string to dict
    if "email_theme" in d and isinstance(d["email_theme"], str):
        try:
            parsed = _json.loads(d["email_theme"])
            d["email_theme"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            d["email_theme"] = {}
    # Parse prompt_vars from JSON string to dict
    if "prompt_vars" in d and isinstance(d["prompt_vars"], str):
        try:
            parsed = _json.loads(d["prompt_vars"])
            d["prompt_vars"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            d["prompt_vars"] = {}
    return d


def list_personas() -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personas ORDER BY updated_at DESC"
        ).fetchall()
    return [_row(r) for r in rows]


def get_persona(persona_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()
    return _row(row)


def save_persona(
    *,
    name: str,
    agent_name: str = "",
    direction: str = "both",
    voice: str = "Sulafat",
    model: str = "gemini-3.1-flash-live-preview",
    audio_mode: str = "gemini",
    system_prompt: str = "",
    enabled_tools: str = "[]",
    source: str = "manual",
    source_ref: str = "",
    email_theme: Optional[str] = None,
    prompt_vars: Optional[dict] = None,
    persona_id: Optional[str] = None,
) -> dict:
    import json as _json
    if direction not in ("inbound", "outbound", "both"):
        direction = "both"
    if audio_mode not in ("gemini", "deepgram"):
        audio_mode = "gemini"
    now = datetime.now().isoformat(timespec="seconds")

    # Serialize prompt_vars for storage
    if prompt_vars is None:
        prompt_vars = {}
    prompt_vars_json = _json.dumps(prompt_vars)

    # Assign theme if not provided
    if not email_theme or email_theme == "{}":
        try:
            from inbound_google import assign_theme
            theme = assign_theme(name)
            email_theme = _json.dumps(theme)
        except Exception:
            # Fallback: assign a simple numbered theme
            import hashlib as _hashlib
            idx = int(_hashlib.md5(name.encode()).hexdigest(), 16) % 10
            email_theme = _json.dumps({"name": f"Theme {idx}", "primary": "#1a73e8", "secondary": "#e8f0fe", "accent": "#174ea6", "header_bg": "linear-gradient(135deg, #1a73e8, #4285f4)"})

    with _connect() as conn:
        if persona_id:
            existing = conn.execute(
                "SELECT id, created_at FROM personas WHERE id = ?", (persona_id,)
            ).fetchone()
            if not existing:
                raise KeyError(persona_id)
            conn.execute(
                """
                UPDATE personas
                   SET name = ?, agent_name = ?, direction = ?, voice = ?, model = ?,
                       audio_mode = ?, system_prompt = ?, enabled_tools = ?,
                       source = ?, source_ref = ?, email_theme = ?, prompt_vars = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    name.strip(), agent_name.strip(), direction, voice.strip() or "Sulafat",
                    model.strip() or "gemini-3.1-flash-live-preview", audio_mode, system_prompt or "",
                    enabled_tools or "[]", source or "manual", source_ref or "", email_theme,
                    prompt_vars_json, now, persona_id,
                ),
            )
        else:
            persona_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO personas (
                    id, name, agent_name, direction, voice, model, audio_mode,
                    system_prompt, enabled_tools, source, source_ref, email_theme,
                    prompt_vars, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_id, name.strip(), agent_name.strip(), direction,
                    voice.strip() or "Sulafat", model.strip() or "gemini-3.1-flash-live-preview",
                    audio_mode, system_prompt or "", enabled_tools or "[]",
                    source or "manual", source_ref or "", email_theme,
                    prompt_vars_json, now, now,
                ),
            )
    persona = get_persona(persona_id)
    if persona is None:
        raise RuntimeError("Persona save failed")
    return persona


def delete_persona(persona_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM personas WHERE id = ?", (persona_id,))
        deleted = cur.rowcount > 0
        if deleted:
            conn.execute(
                "DELETE FROM local_settings WHERE key IN (?, ?) AND value = ?",
                ("active_inbound_persona_id", "active_outbound_persona_id", persona_id),
            )
    return deleted


def get_local_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM local_settings WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return default
    return row["value"]


def set_local_setting(key: str, value: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO local_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def active_persona_id(side: str) -> str:
    key = "active_inbound_persona_id" if side == "inbound" else "active_outbound_persona_id"
    return get_local_setting(key, "")


def set_active_persona(side: str, persona_id: Optional[str]) -> None:
    if side not in ("inbound", "outbound"):
        raise ValueError("side must be inbound or outbound")
    key = "active_inbound_persona_id" if side == "inbound" else "active_outbound_persona_id"
    if not persona_id:
        with _connect() as conn:
            conn.execute("DELETE FROM local_settings WHERE key = ?", (key,))
        return
    if not get_persona(persona_id):
        raise KeyError(persona_id)
    set_local_setting(key, persona_id)


def get_active_persona(side: str) -> Optional[dict]:
    persona_id = active_persona_id(side)
    if not persona_id:
        return None
    return get_persona(persona_id)


def call_fields(persona: Optional[dict]) -> dict:
    """Fields a dispatch can copy onto LiveKit job metadata."""
    if not persona:
        return {}
    fields = {
        "persona_id": persona["id"],
        "audio_mode": persona.get("audio_mode") or "gemini",
    }
    if persona.get("system_prompt"):
        fields["system_prompt"] = persona["system_prompt"]
    if persona.get("voice"):
        fields["voice_override"] = persona["voice"]
    if persona.get("model"):
        fields["model_override"] = persona["model"]
    if persona.get("enabled_tools"):
        fields["tools_override"] = persona["enabled_tools"]
    if persona.get("agent_name"):
        fields["agent_name"] = persona["agent_name"]
    if persona.get("prompt_vars"):
        fields["prompt_vars"] = persona["prompt_vars"]
    return fields


def local_check_slot(date: str, time: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM appointments WHERE date = ? AND time = ? AND status = 'booked' LIMIT 1",
            (date, time)
        ).fetchone()
    return row is None


def local_get_next_available(date: str, time: str) -> str:
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            d_str = dt.strftime("%Y-%m-%d")
            t_str = dt.strftime("%H:%M")
            if local_check_slot(d_str, t_str):
                return f"{d_str} at {t_str}"
    return "no open slots found in the next 7 days"


def local_insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    now_iso = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO appointments (id, name, phone, date, time, service, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'booked', ?)",
            (booking_id, name, phone, date, time, service, now_iso)
        )
    return booking_id


def local_get_all_appointments(date_filter: Optional[str] = None) -> list:
    with _connect() as conn:
        if date_filter:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE date = ? ORDER BY time ASC", (date_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appointments ORDER BY date ASC, time ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def local_log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    ended_by: str = "unknown",
) -> None:
    call_id = str(uuid.uuid4())
    now_iso = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO call_logs 
            (id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, recording_url, notes, ended_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (call_id, phone_number, lead_name, outcome, reason, duration_seconds, now_iso, recording_url, notes, ended_by)
        )


def local_get_all_calls(page: int = 1, limit: int = 20) -> list:
    offset = (page - 1) * limit
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM call_logs ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    return [dict(r) for r in rows]


def local_get_calls_by_phone(phone: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM call_logs WHERE phone_number = ? ORDER BY timestamp DESC LIMIT 10",
            (phone,)
        ).fetchall()
    return [dict(r) for r in rows]


def local_add_contact_memory(phone: str, insight: str) -> None:
    mem_id = str(uuid.uuid4())
    now_iso = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO contact_memory (id, phone, insight, created_at) VALUES (?, ?, ?, ?)",
            (mem_id, phone, insight, now_iso)
        )


def local_get_contact_memory(phone: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contact_memory WHERE phone = ? ORDER BY created_at DESC LIMIT 10",
            (phone,)
        ).fetchall()
    return [dict(r) for r in rows]
