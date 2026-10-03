"""Create, list, select, edit, and call caller personas.

The dashboard routes and the MCP server both use this module, so a persona
saved from either place is the same row in the local database.
"""

import json
import os
import random
import ssl
from typing import Optional

import aiohttp

from local_store import (
    active_persona_id,
    call_fields,
    get_active_persona,
    get_local_setting,
    get_persona,
    list_personas,
    save_persona,
    set_active_persona,
)
from persona_builder import build_from_brief, build_from_target


class AgentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


AGENT_TOOLS = [
    {
        "name": "lookup_contact",
        "description": "Reads this caller's past calls, appointments, and remembered notes.",
        "when": "After the first sentence, not before.",
    },
    {
        "name": "check_availability",
        "description": "Checks a date and time before booking.",
        "when": "When the caller suggests a slot.",
    },
    {
        "name": "book_appointment",
        "description": "Books the appointment after the caller confirms the details. Creates Google Calendar event and sends email when email is provided.",
        "when": "Only after they agree.",
    },
    {
        "name": "send_sms_confirmation",
        "description": "Sends a confirmation text. Skips itself if Twilio is not configured.",
        "when": "After a booking.",
    },
    {
        "name": "send_email",
        "description": "Sends a branded confirmation email with the persona's theme colors.",
        "when": "After a booking when email is available, or when the caller asks for email confirmation.",
    },
    {
        "name": "create_google_calendar_event",
        "description": "Creates a Google Calendar event for the appointment.",
        "when": "After book_appointment succeeds, when Google OAuth is configured.",
    },
    {
        "name": "http_request",
        "description": "Calls a user-configured HTTP/API integration tool by name. The configured tools are listed in the prompt's {{tools}} section.",
        "when": "When the caller needs something handled by a custom API integration you have configured.",
    },
    {
        "name": "remember_details",
        "description": "Stores a useful fact about this caller for the next call.",
        "when": "Whenever they mention a preference, objection, or callback time.",
    },
    {
        "name": "book_calcom",
        "description": "Creates the same booking in Cal.com when that account is configured.",
        "when": "After book_appointment succeeds.",
    },
    {
        "name": "cancel_calcom",
        "description": "Cancels a Cal.com booking by its id.",
        "when": "When the caller wants that booking cancelled.",
    },
    {
        "name": "transfer_to_human",
        "description": "Transfers the live call to DEFAULT_TRANSFER_NUMBER.",
        "when": "When they ask for a person or the request is outside the script.",
    },
    {
        "name": "hangup",
        "description": "Says goodbye and ends the call. No outcome needed — auto-detects from context.",
        "when": "Whenever the conversation has naturally ended.",
    },
    {
        "name": "end_call",
        "description": "Logs a specific outcome and hangs up.",
        "when": "When you need to log a specific outcome (booked, not_interested, wrong_number, etc).",
    },
]


def _tools_field(value) -> str:
    if value is None or value == "":
        return "[]"
    if isinstance(value, list):
        return json.dumps([str(item) for item in value])
    text = str(value).strip()
    if not text:
        return "[]"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentError("enabled_tools must be a JSON list of tool names") from exc
    if not isinstance(parsed, list):
        raise AgentError("enabled_tools must be a JSON list of tool names")
    return json.dumps([str(item) for item in parsed])


def _enabled_names(raw: str) -> list:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _check_audio(audio_mode: str) -> str:
    if audio_mode not in ("gemini", "deepgram"):
        raise AgentError("audio_mode must be gemini or deepgram")
    return audio_mode


def _check_direction(direction: str) -> str:
    if direction not in ("inbound", "outbound", "both"):
        raise AgentError("direction must be inbound, outbound, or both")
    return direction


def _save_built(built: dict, audio_mode: str, voice: str) -> dict:
    built["audio_mode"] = _check_audio(audio_mode)
    if voice:
        built["voice"] = voice
    return save_persona(
        name=built["name"],
        agent_name=built.get("agent_name") or "",
        direction=built.get("direction") or "both",
        voice=built.get("voice") or "Sulafat",
        model=built.get("model") or "gemini-3.1-flash-live-preview",
        audio_mode=built["audio_mode"],
        system_prompt=built.get("system_prompt") or "",
        source=built.get("source") or "manual",
        source_ref=built.get("source_ref") or "",
        prompt_vars=built.get("prompt_vars") or None,
    )


async def create_from_website(url: str, direction: str = "both", audio_mode: str = "gemini", voice: str = "Sulafat") -> dict:
    """Scrape a website, or Google Maps if the site is missing, and save the persona."""
    try:
        built = await build_from_target(url, _check_direction(direction))
    except Exception as exc:
        raise AgentError(str(exc)) from exc
    return _save_built(built, audio_mode, voice)


def create_from_prompt(
    name: str,
    system_prompt: str,
    agent_name: str = "",
    direction: str = "both",
    voice: str = "Sulafat",
    audio_mode: str = "gemini",
    enabled_tools=None,
    prompt_vars: Optional[dict] = None,
) -> dict:
    """Save a persona whose prompt was written by hand."""
    if not (name or "").strip():
        raise AgentError("Name is required")
    if not (system_prompt or "").strip():
        raise AgentError("Prompt is required")
    return save_persona(
        name=name,
        agent_name=agent_name,
        direction=_check_direction(direction),
        voice=voice or "Sulafat",
        audio_mode=_check_audio(audio_mode),
        system_prompt=system_prompt,
        enabled_tools=_tools_field(enabled_tools),
        source="manual",
        prompt_vars=prompt_vars,
    )


async def create_from_ai(
    brief: str,
    name: str = "",
    direction: str = "both",
    audio_mode: str = "gemini",
    voice: str = "Sulafat",
) -> dict:
    """Ask Gemini to write the full prompt from a short brief, then save it."""
    try:
        built = await build_from_brief(brief, _check_direction(direction), name)
    except Exception as exc:
        raise AgentError(str(exc)) from exc
    return _save_built(built, audio_mode, voice)


def list_agents() -> dict:
    inbound_id = active_persona_id("inbound")
    outbound_id = active_persona_id("outbound")
    return {
        "agents": list_personas(),
        "inbound_agent_id": inbound_id,
        "outbound_agent_id": outbound_id,
    }


def _tool_report(persona: dict) -> list:
    enabled = _enabled_names(persona.get("enabled_tools") or "[]")
    use_all = not enabled
    report = []
    for tool in AGENT_TOOLS:
        report.append({
            **tool,
            "enabled": use_all or tool["name"] in enabled,
        })
    return report


def get_agent(agent_id: str) -> dict:
    persona = get_persona(agent_id)
    if not persona:
        raise AgentError("Agent not found", 404)
    tools = _tool_report(persona)
    mode = persona.get("audio_mode") or "gemini"
    if mode == "deepgram":
        audio = "Deepgram listens. Gemini 2.5 Flash decides what to say. Gemini speaks in the saved voice."
        model = "gemini-2.5-flash"
    else:
        audio = "Gemini hears the caller and speaks on the same live model."
        model = persona.get("model") or "gemini-3.1-flash-live-preview"
    active_for = [side for side in ("inbound", "outbound") if active_persona_id(side) == persona["id"]]
    return {
        **persona,
        "tools": tools,
        "capabilities": {
            "audio": audio,
            "voice": persona.get("voice") or "Sulafat",
            "model": model,
            "speaks_first": "Says the first real line when the call connects. There is no separate greeting.",
            "tools_enabled": [tool["name"] for tool in tools if tool["enabled"]],
            "active_for": active_for,
        },
    }


def select_agent(side: str, agent_id: Optional[str]) -> dict:
    if side not in ("inbound", "outbound"):
        raise AgentError("side must be inbound or outbound")
    try:
        set_active_persona(side, agent_id or None)
    except KeyError as exc:
        raise AgentError("Agent not found", 404) from exc
    return {
        "side": side,
        "agent_id": active_persona_id(side),
        "agent": get_active_persona(side),
    }


def edit_selected_prompt(side: str, system_prompt: str) -> dict:
    """Replace the prompt on whichever persona is currently selected for that side."""
    if side not in ("inbound", "outbound"):
        raise AgentError("side must be inbound or outbound")
    if not (system_prompt or "").strip():
        raise AgentError("Prompt is required")
    persona = get_active_persona(side)
    if not persona:
        raise AgentError(f"No agent is selected for {side}", 404)
    return save_persona(
        persona_id=persona["id"],
        name=persona["name"],
        agent_name=persona.get("agent_name") or "",
        direction=persona.get("direction") or "both",
        voice=persona.get("voice") or "Sulafat",
        model=persona.get("model") or "gemini-3.1-flash-live-preview",
        audio_mode=persona.get("audio_mode") or "gemini",
        system_prompt=system_prompt,
        enabled_tools=persona.get("enabled_tools") or "[]",
        source=persona.get("source") or "manual",
        source_ref=persona.get("source_ref") or "",
    )


async def _env_or_setting(key: str) -> str:
    try:
        from db import get_setting
        saved = await get_setting(key, "")
    except Exception:
        saved = ""
    return saved or os.getenv(key, "")


async def place_outbound_call(
    phone: str,
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "site visit",
    system_prompt: Optional[str] = None,
    agent_id: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
    sip_provider: Optional[str] = None,
    agent_name: Optional[str] = None,
    project_name: Optional[str] = None,
    project_type: Optional[str] = None,
    project_location: Optional[str] = None,
    project_status: Optional[str] = None,
    key_benefit_1: Optional[str] = None,
    key_benefit_2: Optional[str] = None,
    key_benefit_3: Optional[str] = None,
    site_visit_day_1: Optional[str] = None,
    site_visit_day_2: Optional[str] = None,
) -> dict:
    """Dial one number with the chosen agent, or the active outbound agent."""
    url = await _env_or_setting("LIVEKIT_URL")
    key = await _env_or_setting("LIVEKIT_API_KEY")
    secret = await _env_or_setting("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        raise AgentError("LiveKit credentials are not configured")

    phone = (phone or "").strip()
    if not phone.startswith("+"):
        raise AgentError("Phone must be in E.164 format, like +919876543210")

    effective_prompt = system_prompt
    effective_voice = None
    effective_model = None
    effective_tools = None
    effective_audio = None
    chosen = None

    if agent_id:
        chosen = get_persona(agent_id)
        if not chosen:
            raise AgentError("Agent not found", 404)
    elif agent_profile_id:
        try:
            from db import get_agent_profile
            profile = await get_agent_profile(agent_profile_id)
        except Exception:
            profile = None
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")
    else:
        chosen = get_active_persona("outbound")

    if chosen:
        fields = call_fields(chosen)
        if not effective_prompt:
            effective_prompt = fields.get("system_prompt")
        effective_voice = effective_voice or fields.get("voice_override")
        effective_model = effective_model or fields.get("model_override")
        effective_tools = effective_tools or fields.get("tools_override")
        effective_audio = fields.get("audio_mode")
        agent_name = fields.get("agent_name") or agent_name
        # "Green Valley Builders" is the old single-call form default, not this persona.
        if not business_name or business_name in ("our company", "Green Valley Builders"):
            business_name = chosen.get("name") or business_name or "our company"
        # The persona prompt is the script. The single-call real-estate
        # fields must not rewrite it.
        project_name = None
        project_type = None
        project_location = None
        project_status = None
        key_benefit_1 = None
        key_benefit_2 = None
        key_benefit_3 = None
        site_visit_day_1 = None
        site_visit_day_2 = None
        if not service_type or service_type == "site visit":
            service_type = None

    if not effective_prompt:
        try:
            from db import get_setting
            saved_prompt = await get_setting("system_prompt", "")
        except Exception:
            saved_prompt = ""
        effective_prompt = get_local_setting("system_prompt", "") or saved_prompt or None

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata = {
        "phone_number": phone,
        "lead_name": lead_name,
        "system_prompt": effective_prompt,
        "sip_provider": sip_provider or os.getenv("SIP_PROVIDER") or await _env_or_setting("SIP_PROVIDER") or "twilio",
    }
    if business_name:
        metadata["business_name"] = business_name
    if service_type:
        metadata["service_type"] = service_type
    for field_name, value in (
        ("agent_name", agent_name),
        ("project_name", project_name),
        ("project_type", project_type),
        ("project_location", project_location),
        ("project_status", project_status),
        ("key_benefit_1", key_benefit_1),
        ("key_benefit_2", key_benefit_2),
        ("key_benefit_3", key_benefit_3),
        ("site_visit_day_1", site_visit_day_1),
        ("site_visit_day_2", site_visit_day_2),
    ):
        if value:
            metadata[field_name] = value
    if effective_voice:
        metadata["voice_override"] = effective_voice
    if effective_model:
        metadata["model_override"] = effective_model
    if effective_tools:
        metadata["tools_override"] = effective_tools
    if effective_audio:
        metadata["audio_mode"] = effective_audio
    if chosen:
        metadata["persona_id"] = chosen["id"]

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=3600, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)
            )
        )
        await lk.aclose()
        await session.close()
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError(f"Dispatch failed: {exc}", 500) from exc

    try:
        from db import log_error
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
    except Exception:
        pass
    return {
        "status": "dispatched",
        "room": room_name,
        "phone": phone,
        "agent_id": chosen["id"] if chosen else None,
        "agent_name": (chosen or {}).get("name") if chosen else agent_name,
    }
