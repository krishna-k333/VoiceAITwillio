import asyncio
import json
import logging
import os
import ssl
import sys
import time
import certifi
from typing import Optional

# Force UTF-8 stdout/stderr so emoji in print() don't crash on Windows cp1252 consoles
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from dotenv import load_dotenv

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_call as _db_log_call, log_call_sync as _db_log_call_sync, log_error, get_enabled_tools, get_setting
from prompts import build_prompt, INBOUND_SYSTEM_PROMPT
from tools import AppointmentTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings table into os.environ before worker starts."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value") and row["key"] not in os.environ:
                os.environ[row["key"]] = row["value"]
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(
    tools: list,
    system_prompt: str,
    *,
    model: Optional[str] = None,
    voice: Optional[str] = None,
    audio_mode: Optional[str] = None,
) -> AgentSession:
    """
    Build AgentSession with Gemini Live or pipeline fallback.

    CRITICAL SILENCE-PREVENTION CONFIG — all 3 required:
    1. SessionResumptionConfig(transparent=True) → auto-reconnects after timeout
    2. ContextWindowCompressionConfig → sliding window prevents token limit freeze
    3. RealtimeInputConfig(END_SENSITIVITY_LOW) → less aggressive VAD, 2s silence threshold

    ⚠️ EndSensitivity MUST use full string form: END_SENSITIVITY_LOW (not .LOW — AttributeError!)
    """
    gemini_model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-live")
    gemini_voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    if audio_mode == "deepgram":
        use_realtime = False
    elif audio_mode == "gemini":
        use_realtime = True
    else:
        use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        try:
            from google.genai import types as _gt
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=600,
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention config applied (VAD LOW, transparent resumption, context compression)")
        except Exception as _cfg_err:
            logger.warning("Could not build silence-prevention config: %s", _cfg_err)
            _realtime_input_cfg = None
            _session_resumption_cfg = None
            _ctx_compression_cfg = None

        realtime_kwargs: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
            realtime_kwargs["session_resumption"]         = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        return AgentSession(llm=RealtimeClass(**realtime_kwargs), tools=tools)

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Gemini TTS, voice=%s)", gemini_voice)
    if _deepgram_stt is None:
        raise RuntimeError("Deepgram plugin is not installed. Run: pip install livekit-plugins-deepgram")
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise RuntimeError("DEEPGRAM_API_KEY is not set. Deepgram mode needs it in .env")
    # 400ms of silence before a turn ends. The plugin default of 25ms cuts people off on a phone line.
    stt = _deepgram_stt(
        model="nova-3",
        language="multi",
        endpointing_ms=400,
        interim_results=True,
        punctuate=True,
        smart_format=True,
        api_key=os.getenv("DEEPGRAM_API_KEY"),
    )
    try:
        from livekit.plugins.google.beta.gemini_tts import TTS as GeminiTTS
        tts = GeminiTTS(model="gemini-2.5-flash-preview-tts", voice_name=gemini_voice or "Sulafat")
    except Exception as exc:
        raise RuntimeError(f"Gemini TTS could not start for Deepgram mode: {exc}") from exc
    # gemini-3.8-live is a speech model. The pipeline needs a text model.
    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.5-flash"),
        tts=tts,
        vad=silero.VAD.load(),
        tools=tools,
    )


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


# ── Background S3 Recording Helper ──────────────────────────────────────────
async def _start_s3_recording(ctx: agents.JobContext, tool_ctx: AppointmentTools) -> None:
    """Start S3 recording in the background without blocking the opening line or session start."""
    aws_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
    s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
    s3_region = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
    if aws_key and aws_secret and aws_bucket:
        try:
            recording_path = f"recordings/{ctx.room.name}.ogg"
            egress_req = api.RoomCompositeEgressRequest(
                room_name=ctx.room.name,
                audio_only=True,
                file_outputs=[
                    api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG,
                        filepath=recording_path,
                        s3=api.S3Upload(
                            access_key=aws_key,
                            secret=aws_secret,
                            bucket=aws_bucket,
                            region=s3_region,
                            endpoint=s3_endpoint,
                        ),
                    )
                ],
            )
            egress = await ctx.api.egress.start_room_composite_egress(egress_req)
            ep = s3_endpoint.rstrip("/")
            tool_ctx.recording_url = (
                f"{ep}/{aws_bucket}/{recording_path}"
                if ep
                else f"s3://{aws_bucket}/{recording_path}"
            )
            await _log("info", f"Recording started: egress={egress.egress_id}")
        except Exception as exc:
            await _log("warning", f"Recording start failed (non-fatal): {exc}")


async def entrypoint(ctx: agents.JobContext) -> None:
    """
    Main entrypoint. Called per job. Reads metadata JSON from ctx.job.metadata.

    DIAL-FIRST PATTERN — CRITICAL:
    Start Gemini Live ONLY after create_sip_participant(wait_until_answered=True) completes.
    If you start the session during ring time (~20-30s), the Gemini idle timeout fires
    and the session dies silently before the call is even answered.

    NO close_on_disconnect — SIP legs have brief audio dropouts that look like disconnects.
    Instead, watch participant_disconnected event for the specific SIP identity.
    """
    await _log("info", f"Job started — room: {ctx.room.name}")

    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "site visit"
    agent_name_var = "Priya"
    project_name = ""
    project_type = "property"
    project_location = ""
    project_status = "abhi available hai"
    key_benefit_1 = ""
    key_benefit_2 = ""
    key_benefit_3 = ""
    site_visit_day_1 = "is Saturday"
    site_visit_day_2 = "is Sunday"
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None
    audio_mode: Optional[str] = None
    persona_id: Optional[str] = None
    sip_provider = "twilio"
    is_inbound = False

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            phone_number    = data.get("phone_number")
            lead_name       = data.get("lead_name", lead_name)
            business_name   = data.get("business_name", business_name)
            service_type    = data.get("service_type", service_type)
            agent_name_var  = data.get("agent_name", agent_name_var)
            project_name    = data.get("project_name", project_name)
            project_type    = data.get("project_type", project_type)
            project_location = data.get("project_location", project_location)
            project_status  = data.get("project_status", project_status)
            key_benefit_1   = data.get("key_benefit_1", key_benefit_1)
            key_benefit_2   = data.get("key_benefit_2", key_benefit_2)
            key_benefit_3   = data.get("key_benefit_3", key_benefit_3)
            site_visit_day_1 = data.get("site_visit_day_1", site_visit_day_1)
            site_visit_day_2 = data.get("site_visit_day_2", site_visit_day_2)
            custom_prompt   = data.get("system_prompt")
            voice_override  = data.get("voice_override")
            model_override  = data.get("model_override")
            tools_override  = data.get("tools_override")
            audio_mode      = data.get("audio_mode") or audio_mode
            persona_id      = data.get("persona_id") or persona_id
            sip_provider    = os.getenv("SIP_PROVIDER") or data.get("sip_provider", sip_provider)
            is_inbound      = data.get("inbound", False)
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    # Inbound: no phone_number in metadata — detect from room name or flag
    if not phone_number and not is_inbound:
        if ctx.room.name.startswith("inbound-"):
            is_inbound = True

    await _log("info", f"Call job received — phone={phone_number} lead={lead_name} biz={business_name} inbound={is_inbound}")

    # ── Persona: local database, then the old built-in inbound list ───────────
    _inbound_persona_data = None

    def _apply_saved_persona(saved: dict) -> None:
        nonlocal custom_prompt, voice_override, model_override, tools_override, audio_mode, agent_name_var, business_name, _inbound_persona_data
        if saved.get("system_prompt") and not custom_prompt:
            custom_prompt = saved["system_prompt"]
        if not voice_override and saved.get("voice"):
            voice_override = saved["voice"]
        if not model_override and saved.get("model"):
            model_override = saved["model"]
        if not tools_override and saved.get("enabled_tools"):
            tools_override = saved["enabled_tools"]
        if not audio_mode and saved.get("audio_mode"):
            audio_mode = saved["audio_mode"]
        if saved.get("agent_name"):
            agent_name_var = saved["agent_name"]
        if business_name in ("our company", "", None):
            business_name = saved.get("name") or business_name
        _inbound_persona_data = {
            "name": saved.get("name"),
            "agent_name": saved.get("agent_name"),
            "voice": saved.get("voice"),
            "prompt": saved.get("system_prompt"),
        }

    try:
        from local_store import get_active_persona, get_local_setting, get_persona
        saved_persona = get_persona(persona_id) if persona_id else None
        if saved_persona is None:
            saved_persona = get_active_persona("inbound" if is_inbound else "outbound")
        if saved_persona:
            _apply_saved_persona(saved_persona)
            await _log(
                "info",
                f"Persona loaded: {saved_persona.get('name')} "
                f"({'inbound' if is_inbound else 'outbound'}, audio={audio_mode or 'default'})",
            )
        elif not custom_prompt and not is_inbound:
            local_prompt = get_local_setting("system_prompt", "")
            if local_prompt:
                custom_prompt = local_prompt
                await _log("info", "Using saved AI prompt — no outbound persona selected")
    except Exception as _pe:
        await _log("warning", f"Could not load saved persona: {_pe}")

    if is_inbound and not custom_prompt:
        try:
            from personas import PERSONAS
            _persona_id = await get_setting("INBOUND_ACTIVE_PERSONA", "raj_dental")
            _inbound_persona_data = PERSONAS.get(_persona_id) or PERSONAS.get("raj_dental", {})
            if _inbound_persona_data.get("prompt"):
                custom_prompt = _inbound_persona_data["prompt"]
            if not voice_override and _inbound_persona_data.get("voice"):
                voice_override = _inbound_persona_data["voice"]
            if business_name in ("our company", ""):
                business_name = _inbound_persona_data.get("name", business_name)
            if _inbound_persona_data.get("agent_name"):
                agent_name_var = _inbound_persona_data["agent_name"]
            await _log("info", f"Built-in inbound persona loaded: {_persona_id}")
        except Exception as _pe:
            await _log("warning", f"Could not load built-in inbound persona: {_pe}")

    system_prompt = build_prompt(
        lead_name=lead_name, lead_phone=phone_number or "",
        business_name=business_name, service_type=service_type,
        agent_name=agent_name_var, project_name=project_name,
        project_type=project_type, project_location=project_location,
        project_status=project_status, key_benefit_1=key_benefit_1,
        key_benefit_2=key_benefit_2, key_benefit_3=key_benefit_3,
        site_visit_day_1=site_visit_day_1, site_visit_day_2=site_visit_day_2,
        custom_prompt=custom_prompt, inbound=is_inbound,
    )
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name, is_inbound=is_inbound, persona_data=_inbound_persona_data)

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    if audio_mode == "deepgram":
        if _deepgram_stt is None or not os.getenv("DEEPGRAM_API_KEY"):
            await _log("error", "This persona uses Deepgram, but DEEPGRAM_API_KEY is missing or the plugin is not installed")
            ctx.shutdown()
            return
        if not os.getenv("GOOGLE_API_KEY"):
            await _log("error", "Deepgram mode still needs GOOGLE_API_KEY so Gemini can think and speak")
            ctx.shutdown()
            return

    # ── Connect ──────────────────────────────────────────────────────────────
    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Inbound: extract caller number from SIP participant identity ──────────
    if is_inbound and not phone_number:
        await asyncio.sleep(1)  # brief wait for SIP participant to join
        for p in ctx.room.remote_participants.values():
            if p.identity.startswith("sip_") or p.identity.startswith("+"):
                phone_number = p.identity.replace("sip_", "")
                tool_ctx.phone_number = phone_number
                await _log("info", f"Inbound call from {phone_number}")
                break
        if not phone_number:
            await _log("info", "Inbound call — caller number not available")

    # ── Dial — MUST come before session.start() ──────────────────────────────
    if phone_number and not is_inbound:
        if sip_provider == "voicelink":
            trunk_id = await get_setting("VOICELINK_TRUNK_ID", "") or os.getenv("VOICELINK_TRUNK_ID") or os.getenv("OUTBOUND_TRUNK_ID")
            tool_ctx._sip_domain = os.getenv("VOICELINK_SIP_DOMAIN", "")
        elif sip_provider == "telnyx":
            trunk_id = await get_setting("TELNYX_TRUNK_ID", "") or os.getenv("TELNYX_TRUNK_ID") or os.getenv("OUTBOUND_TRUNK_ID")
            tool_ctx._sip_domain = "sip.telnyx.com"
        elif sip_provider == "twilio":
            trunk_id = await get_setting("TWILIO_TRUNK_ID", "") or os.getenv("TWILIO_TRUNK_ID") or os.getenv("OUTBOUND_TRUNK_ID")
            tool_ctx._sip_domain = await get_setting("TWILIO_SIP_TRUNK_DOMAIN", "") or os.getenv("TWILIO_SIP_TRUNK_DOMAIN", "")
        else:
            trunk_id = await get_setting("VOBIZ_TRUNK_ID", "") or os.getenv("VOBIZ_TRUNK_ID") or os.getenv("OUTBOUND_TRUNK_ID")
            tool_ctx._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")

        if not trunk_id:
            await _log("error", f"OUTBOUND_TRUNK_ID not set for provider '{sip_provider}' — cannot place outbound call")
            ctx.shutdown()
            return
        # VoiceLink requires tech prefix prepended to the number (strip leading +)
        if sip_provider == "voicelink":
            tech_prefix = os.getenv("VOICELINK_TECH_PREFIX", "")
            dial_number = tech_prefix + phone_number.lstrip("+") if tech_prefix else phone_number
        else:
            dial_number = phone_number  # Telnyx, Vobiz, and Twilio all use plain E.164
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id} (provider={sip_provider}, dial={dial_number})")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=dial_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            try:
                await _db_log_call(
                    phone_number=phone_number,
                    lead_name=tool_ctx.lead_name,
                    outcome="no_answer",
                    reason=f"SIP dial failed: {exc}",
                    duration_seconds=0,
                    ended_by="system",
                )
            except Exception as _le:
                _db_log_call_sync(
                    phone_number, tool_ctx.lead_name,
                    "no_answer", f"SIP dial failed: {exc}",
                    0, None, "system",
                )
            ctx.shutdown()
            return
        await _log("info", f"Call ANSWERED — {phone_number} picked up, starting AI session now")
        tool_ctx._call_start_time = time.time()
    elif is_inbound:
        tool_ctx._call_start_time = time.time()

    gemini_model = model_override or os.getenv("GEMINI_MODEL", "gemini-3.8-live")
    opening_voice = voice_override or os.getenv("GEMINI_TTS_VOICE", "Aoede")

    # ── Build and start the session ──────────────────────────────────────────
    if audio_mode == "deepgram":
        await _log("info", f"Building AI session — mode=deepgram+gemini model=gemini-2.5-flash voice={opening_voice}")
    else:
        await _log("info", f"Building AI session — mode={audio_mode or 'gemini-live'} model={gemini_model} voice={opening_voice}")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    session = _build_session(
        tools=active_tools,
        system_prompt=system_prompt,
        model=model_override,
        voice=voice_override,
        audio_mode=audio_mode,
    )

    # Use RoomOptions if available (non-deprecated), else fall back
    # NEVER use close_on_disconnect=True with SIP — drops on any audio blip
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_options=_RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        )
    else:
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        )

    await session.start(**_session_kwargs)
    await _log("info", "Agent session started — AI ready, speaking the opening line")

    # ── Fallback logger — runs if model never calls end_call() ───────────────
    _sip_identity = f"sip_{phone_number}" if phone_number else None
    _disconnect_event = asyncio.Event()
    _fallback_logged = False

    async def _do_fallback_log():
        """Primary fallback: async, runs in the event loop after disconnect fires.
        Only called once — blocks the sync emergency path via _fallback_logged flag."""
        nonlocal _fallback_logged
        if tool_ctx._call_logged or _fallback_logged:
            return
        _fallback_logged = True
        duration = int(time.time() - tool_ctx._call_start_time)
        fallback_outcome = "booked" if getattr(tool_ctx, "_booking_completed", False) else "dropped"
        fallback_reason = (
            f"caller hung up after {getattr(tool_ctx, '_last_booking_summary', 'appointment booking')}"
            if fallback_outcome == "booked"
            else "caller hung up"
        )
        await _log("info", f"Logging cut call — duration={duration}s phone={tool_ctx.phone_number}")
        try:
            await _db_log_call(
                phone_number=tool_ctx.phone_number or "unknown",
                lead_name=tool_ctx.lead_name,
                outcome=fallback_outcome,
                reason=fallback_reason,
                duration_seconds=duration,
                recording_url=tool_ctx.recording_url,
                ended_by="caller_hungup",
            )
            await _log("info", f"Cut call logged — duration={duration}s ended_by=caller_hungup")
        except Exception as _le:
            await _log("warning", f"Async fallback log failed: {_le} — using sync fallback")
            _db_log_call_sync(
                tool_ctx.phone_number or "unknown", tool_ctx.lead_name,
                fallback_outcome, f"{fallback_reason} (sync retry)",
                duration, tool_ctx.recording_url, "caller_hungup",
            )

    # ── Register disconnect listeners — ONLY set the event, no blocking DB calls ──
    # The actual logging happens async in _do_fallback_log() after the event fires.
    # _sync_log_in_thread is kept ONLY as last-resort for process shutdown.
    import threading as _threading

    def _sync_log_in_thread():
        """Emergency sync fallback — only runs at process shutdown if async path didn't fire."""
        nonlocal _fallback_logged
        if tool_ctx._call_logged or _fallback_logged:
            return
        _fallback_logged = True
        duration = int(time.time() - tool_ctx._call_start_time)
        fallback_outcome = "booked" if getattr(tool_ctx, "_booking_completed", False) else "dropped"
        fallback_reason = (
            f"caller hung up after {getattr(tool_ctx, '_last_booking_summary', 'appointment booking')}"
            if fallback_outcome == "booked"
            else "caller hung up"
        )
        t = _threading.Thread(
            target=_db_log_call_sync,
            args=(tool_ctx.phone_number or "unknown", tool_ctx.lead_name,
                  fallback_outcome, f"{fallback_reason} (shutdown fallback)",
                  duration, tool_ctx.recording_url, "caller_hungup"),
            daemon=True,
        )
        t.start()
        t.join(timeout=12)

    def _on_participant_disconnected(participant: rtc.RemoteParticipant):
        is_sip = (_sip_identity and participant.identity == _sip_identity) \
                 or participant.identity.startswith("sip_")
        if is_sip:
            _disconnect_event.set()  # just signal — no blocking DB call here

    def _on_disconnected():
        _disconnect_event.set()  # just signal — no blocking DB call here

    ctx.room.on("participant_disconnected", _on_participant_disconnected)
    ctx.room.on("disconnected", _on_disconnected)

    # ── Shutdown callback — last resort if process exits before async path fires ──
    async def _shutdown_log(_reason: str = "") -> None:
        _sync_log_in_thread()
    ctx.add_shutdown_callback(_shutdown_log)

    # Catch the race where the caller hung up between answer and listener
    # registration — the disconnect event already fired and we'd otherwise
    # wait out the full 1-hour timeout. If no SIP participant is present, mark
    # the disconnect now so the wait below returns immediately and logs.
    if phone_number and not is_inbound:
        _sip_present = any(
            p.identity == _sip_identity or p.identity.startswith("sip_")
            for p in ctx.room.remote_participants.values()
        )
        if not _sip_present:
            await _log("info", "SIP participant already gone before listener setup — flagging disconnect")
            _disconnect_event.set()

    # ── Optional S3 recording (runs in background so the opening line is not blocked) ────
    if phone_number:
        asyncio.create_task(_start_s3_recording(ctx, tool_ctx))

    # No separate greeting. Gemini Live will not speak until a reply is requested,
    # so this turn is the first real line — not "Hi, this is …".
    opening = (
        "The call just connected. Do not greet: no hi, hello, good morning, "
        "and do not ask if you are speaking with them. "
        "Say the first real line of your instructions now, in one short sentence. "
        "Do not wait for the caller, and do not call any tool before this sentence."
    )
    try:
        await session.generate_reply(instructions=opening)
        await _log("info", "Opening line triggered via generate_reply — no greeting")
    except Exception as _gr_exc:
        await _log("warning", f"generate_reply failed: {_gr_exc}")

    # ── Wait for SIP participant to leave, then fallback-log if needed ────────
    if phone_number or is_inbound:
        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")

        await _log("info", f"SIP participant disconnected — ending session for {phone_number}")
        await _do_fallback_log()  # no-op if already logged by callback or end_call()
        await session.aclose()
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
