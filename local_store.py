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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS local_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
    print(f"Local persona database ready at {db_path()}")


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row else None


def list_personas() -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM personas ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


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
    persona_id: Optional[str] = None,
) -> dict:
    if direction not in ("inbound", "outbound", "both"):
        direction = "both"
    if audio_mode not in ("gemini", "deepgram"):
        audio_mode = "gemini"
    now = datetime.now().isoformat(timespec="seconds")
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
                       source = ?, source_ref = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    name.strip(), agent_name.strip(), direction, voice.strip() or "Sulafat",
                    model.strip() or "gemini-3.1-flash-live-preview", audio_mode, system_prompt or "",
                    enabled_tools or "[]", source or "manual", source_ref or "", now, persona_id,
                ),
            )
        else:
            persona_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO personas (
                    id, name, agent_name, direction, voice, model, audio_mode,
                    system_prompt, enabled_tools, source, source_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_id, name.strip(), agent_name.strip(), direction,
                    voice.strip() or "Sulafat", model.strip() or "gemini-3.1-flash-live-preview",
                    audio_mode, system_prompt or "", enabled_tools or "[]",
                    source or "manual", source_ref or "", now, now,
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
    return fields
