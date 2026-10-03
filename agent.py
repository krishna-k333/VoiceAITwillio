import asyncio
import json
import logging
import os
import re
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

# Patch aiohttp.StreamReader.readline: google-genai SDK incorrectly passes max_line_length
# which aiohttp.StreamReader does not accept.
try:
    import aiohttp
    _orig_readline = aiohttp.StreamReader.readline
    async def _patched_readline(self, *args, **kwargs):
        kwargs.pop("max_line_length", None)
        return await _orig_readline(self, *args, **kwargs)
    aiohttp.StreamReader.readline = _patched_readline
except Exception:
    pass

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_call as _db_log_call, log_call_sync as _db_log_call_sync, log_error, get_enabled_tools, get_setting
from prompts import build_prompt, render_prompt, INBOUND_SYSTEM_PROMPT
from tools import AppointmentTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")
# Official Gemini Live id. gemini-3.8-live is not used for calls.
LIVE_MODEL = "gemini-3.1-flash-live-preview"


def _live_model(model: Optional[str]) -> str:
    value = (model or os.getenv("GEMINI_MODEL") or LIVE_MODEL).strip()
    if not value or "3.8" in value:
        return LIVE_MODEL
    return value

# A quoted line the agent may say. The first quote in a whole prompt is often an
# example ("Ji haan, aapka appointment book ho jayega"), not the opener.
_SPOKEN_QUOTE = re.compile(r'"([^"\n]{12,180})"|“([^”\n]{12,180})”')
_SECTION_END = re.compile(
    r"\n(?:#{1,3} |STEP\s+\d+\b|[A-Z][A-Z0-9][A-Z0-9 /&\-]{5,}\s*$)",
    re.MULTILINE,
)


def _spoken_quotes(text: str) -> list[str]:
    found = []
    for match in _SPOKEN_QUOTE.finditer(text or ""):
        line = (match.group(1) or match.group(2) or "").strip()
        if line:
            found.append(line)
    return found


def _section_body(text: str, label: str) -> str:
    match = re.search(re.escape(label), text or "", flags=re.IGNORECASE)
    if not match:
        return ""
    rest = text[match.end():]
    stop = _SECTION_END.search(rest)
    body = rest[: stop.start()] if stop else rest[:1200]
    return body[:1200]


def _fallback_inbound_opening(agent_name: str, business_name: str) -> str:
    agent = (agent_name or "").strip()
    business = (business_name or "").strip()
    if business.lower() in {"", "our company", "the business"}:
        business = ""
    if agent.lower() in {"", "the receptionist"}:
        agent = ""
    if business and agent:
        return f"Ji, {business} se {agent} is taraf se. Bataiye, kis cheez mein madad chahiye?"
    if business:
        return f"Ji, {business} se bol rahe hain. Bataiye, kis cheez mein madad chahiye?"
    if agent:
        return f"Ji, {agent} is taraf se. Bataiye, kis cheez mein madad chahiye?"
    return "Ji, bataiye, kis cheez mein madad chahiye?"


def inbound_opening_line(
    prompt: str,
    *,
    greeting: str = "",
    agent_name: str = "",
    business_name: str = "",
) -> str:
    """Sentence an inbound agent says the moment the call is answered.

    Only a labeled inbound opening, or a quote inside the greeting field, is used.
    """
    text = prompt or ""
    for label in ("INBOUND FIRST LINE", "INBOUND OPENING", "INBOUND FIRST SENTENCE"):
        quotes = _spoken_quotes(_section_body(text, label))
        if quotes:
            return quotes[0]

    lowered = text.lower()
    for marker in ("this call is inbound", "they called you"):
        idx = lowered.find(marker)
        if idx >= 0:
            quotes = _spoken_quotes(text[idx:idx + 700])
            if quotes:
                return quotes[0]

    first_line = _section_body(text, "FIRST LINE")
    if first_line:
        inbound_at = first_line.lower().find("inbound")
        if inbound_at >= 0:
            quotes = _spoken_quotes(first_line[inbound_at:])
            if quotes:
                return quotes[0]
        elif "outbound" not in first_line.lower():
            quotes = _spoken_quotes(first_line)
            if quotes:
                return quotes[0]

    greeting_quotes = _spoken_quotes(greeting or "")
    if greeting_quotes:
        return greeting_quotes[0]
    return _fallback_inbound_opening(agent_name, business_name)


def outbound_opening_line(
    prompt: str,
    *,
    agent_name: str = "",
    business_name: str = "",
) -> str:
    """Sentence an outbound agent says immediately when the call connects."""
    text = prompt or ""
    for label in (
        "OUTBOUND FIRST LINE",
        "OUTBOUND OPENING",
        "OUTBOUND FIRST SENTENCE",
        "STEP 2 — APNA INTRODUCTION + PERMISSION LO",
        "STEP 2 — APNA INTRODUCTION",
        "STEP 2",
    ):
        quotes = _spoken_quotes(_section_body(text, label))
        if quotes:
            return quotes[0]

    first_line = _section_body(text, "FIRST LINE")
    if first_line and "inbound" not in first_line.lower():
        quotes = _spoken_quotes(first_line)
        if quotes:
            return quotes[0]

    lowered = text.lower()
    for marker in ("call connect hote hi", "connect hote hi", "call start"):
        idx = lowered.find(marker)
        if idx >= 0:
            quotes = _spoken_quotes(text[idx : idx + 800])
            if quotes:
                return quotes[0]

    idx = text.find("STEP 2")
    if idx >= 0:
        quotes = _spoken_quotes(text[idx : idx + 800])
        if quotes:
            return quotes[0]

    agent = (agent_name or "").strip()
    business = (business_name or "").strip()
    if business.lower() in {"", "our company", "the business"}:
        business = ""
    if agent.lower() in {"", "the receptionist"}:
        agent = ""

    if business and agent:
        return f"Ji! Main {agent} bol rahi hoon, {business} se. Kya do minute baat ho sakti hai?"
    if business:
        return f"Ji! Main {business} se bol rahi hoon. Kya do minute baat ho sakti hai?"
    if agent:
        return f"Ji! Main {agent} bol rahi hoon. Kya do minute baat ho sakti hai?"
    return "Ji namaste! Kya do minute baat ho sakti hai?"


async def _speak_opening_31(session, instructions: str) -> None:
    """Gemini 3.1 Live dynamic opening. Sends trigger event so Gemini generates the greeting turn from its prompt."""
    from google.genai import types as gt

    for attempt in (1, 2, 3):
        try:
            session.clear_user_turn()
        except Exception as exc:
            await _log("warning", f"Could not clear buffered caller audio: {exc}")
        activity = getattr(session, "_activity", None)
        rt = getattr(activity, "_rt_session", None) if activity else None
        send = getattr(rt, "_send_client_event", None) if rt else None
        if send is None:
            if attempt < 3:
                await _log("info", f"Gemini 3.1 session not ready yet (attempt {attempt}/3), waiting 2s...")
                await asyncio.sleep(2)
                continue
            await _log("warning", "Gemini 3.1 session never became ready — falling back to generate_reply")
            await _speak_opening(session, instructions)
            return
        started = asyncio.Event()
        finished = asyncio.Event()

        def _on_state(ev) -> None:
            state = getattr(ev, "new_state", None)
            if state == "speaking":
                started.set()
            elif started.is_set() and state != "speaking":
                finished.set()

        session.on("agent_state_changed", _on_state)
        try:
            send(
                gt.LiveClientContent(
                    turns=[
                        gt.Content(parts=[gt.Part(text=instructions)], role="user"),
                    ],
                    turn_complete=True,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=10)
            try:
                await asyncio.wait_for(finished.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
            await _log("info", "Opening greeting played (3.1 direct)")
            return
        except asyncio.TimeoutError:
            await _log("warning", f"Opening greeting did not play (3.1 attempt {attempt})")
        except Exception as exc:
            await _log("warning", f"Opening greeting send failed (3.1 attempt {attempt}): {exc}")
        finally:
            session.off("agent_state_changed", _on_state)
    await _log("warning", "3.1 direct send exhausted — falling back to generate_reply")
    await _speak_opening(session, instructions)


async def _speak_opening(session, instructions: str) -> None:
    """Say the opening greeting dynamically according to the prompt at the first second."""
    for attempt in (1, 2):
        try:
            session.clear_user_turn()
        except Exception as exc:
            await _log("warning", f"Could not clear buffered caller audio: {exc}")
        handle = None
        try:
            handle = session.generate_reply(instructions=instructions)
            await asyncio.wait_for(handle.wait_for_playout(), timeout=15)
        except asyncio.TimeoutError:
            await _log("warning", f"Opening greeting timed out (attempt {attempt})")
            try:
                await session.interrupt()
            except Exception:
                pass
            continue
        except Exception as exc:
            await _log("warning", f"generate_reply failed (attempt {attempt}): {exc}")
            continue
        interrupted = bool(getattr(handle, "interrupted", False))
        said = _spoken_from_handle(handle)
        if not interrupted:
            if said:
                await _log("info", f"Opening greeting played: {said[:160]}")
            else:
                await _log("info", "Opening greeting playout completed successfully")
            return
        await _log(
            "warning",
            f"Opening greeting did not play (attempt {attempt}, interrupted={interrupted})",
        )
        try:
            await session.interrupt()
        except Exception:
            pass
    await _log("warning", "Opening greeting did not play")


def _spoken_from_handle(handle) -> str:
    parts: list[str] = []
    for item in getattr(handle, "chat_items", None) or []:
        if getattr(item, "role", None) != "assistant":
            continue
        content = getattr(item, "content", "") or ""
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, str):
                    parts.append(piece)
                else:
                    text = getattr(piece, "text", None)
                    if text:
                        parts.append(str(text))
    return " ".join(part.strip() for part in parts if part and str(part).strip()).strip()


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
    gemini_model = _live_model(model)
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
                    silence_duration_ms=850,
                    prefix_padding_ms=250,
                ),
            )
            logger.info("Telephony VAD config applied (sensitivity=LOW, silence=850ms)")
        except Exception as _cfg_err:
            logger.warning("Could not build VAD config: %s", _cfg_err)
            _realtime_input_cfg = None

        realtime_kwargs: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"] = _realtime_input_cfg

        return AgentSession(llm=RealtimeClass(**realtime_kwargs), tools=tools)

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Gemini TTS, voice=%s)", gemini_voice)
    if _deepgram_stt is None:
        raise RuntimeError("Deepgram plugin is not installed. Run: pip install livekit-plugins-deepgram")
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise RuntimeError("DEEPGRAM_API_KEY is not set. Deepgram mode needs it in .env")
    # 350ms silence endpointing for responsive conversational turns without cutoffs
    stt = _deepgram_stt(
        model="nova-3",
        language="multi",
        endpointing_ms=350,
        interim_results=True,
        punctuate=True,
        smart_format=True,
        api_key=os.getenv("DEEPGRAM_API_KEY"),
    )
    valid_gemini_voices = {
        'Zephyr', 'Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede',
        'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba',
        'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar',
        'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi',
        'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat'
    }
    chosen_voice = gemini_voice if gemini_voice in valid_gemini_voices else "Sulafat"
    try:
        from livekit.plugins.google.beta.gemini_tts import TTS as GeminiTTS
        tts = GeminiTTS(
            model="gemini-2.5-flash-preview-tts",
            voice_name=chosen_voice,
            api_key=os.getenv("GOOGLE_API_KEY"),
        )
    except Exception as exc:
        logger.warning("Gemini TTS could not start, checking Deepgram TTS fallback: %s", exc)
        try:
            from livekit.plugins import deepgram as _dg
            tts = _dg.TTS(model="aura-asteria-en", api_key=os.getenv("DEEPGRAM_API_KEY"))
            logger.info("Using Deepgram TTS (aura-asteria-en) as fallback")
        except Exception as dg_exc:
            raise RuntimeError(f"TTS could not start for Deepgram mode: {exc} | fallback error: {dg_exc}") from exc
    # Responsive pipeline session: low endpointing delay, silero VAD tuned for telephony
    vad = silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=0.45,
        prefix_padding_duration=0.3,
    )
    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.5-flash"),
        tts=tts,
        vad=vad,
        tools=tools,
        min_endpointing_delay=0.1,
        max_endpointing_delay=0.5,
        allow_interruptions=True,
        min_consecutive_speech_delay=0.0,
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
    meta: dict = {}

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            meta = data if isinstance(data, dict) else {}
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

    # SIP dispatch rules put the caller in inbound-* with no job metadata.
    # A phone number in metadata must not turn that room into an outbound dial.
    if ctx.room.name.startswith("inbound-"):
        is_inbound = True

    await _log("info", f"Call job received — phone={phone_number} lead={lead_name} biz={business_name} inbound={is_inbound}")

    # ── Persona: local database, then the old built-in inbound list ───────────
    _inbound_persona_data = None
    prompt_vars: dict = {}

    def _apply_saved_persona(saved: dict) -> None:
        nonlocal custom_prompt, voice_override, model_override, tools_override, audio_mode, agent_name_var, business_name, prompt_vars, _inbound_persona_data
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
        if isinstance(saved.get("prompt_vars"), dict):
            prompt_vars.update(saved["prompt_vars"])
        _inbound_persona_data = {
            "name": saved.get("name"),
            "agent_name": saved.get("agent_name"),
            "voice": saved.get("voice"),
            "prompt": saved.get("system_prompt"),
            "email_theme": saved.get("email_theme"),
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

    if not audio_mode:
        use_rt = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"
        audio_mode = "gemini" if use_rt else "deepgram"

    if custom_prompt and "THIS CALL:" not in custom_prompt:
        if is_inbound:
            custom_prompt += (
                "\n\nTHIS CALL: inbound. They called you and are already listening. "
                "Speak your opening sentence immediately. Do not stay silent and do not wait for them."
            )
        else:
            custom_prompt += (
                "\n\nTHIS CALL: outbound. You placed this call. "
                "Speak the outbound first line now, in one sentence. Do not greet, and do not wait for them."
            )

    def _from_job(key: str, fallback: str) -> str:
        # Persona scripts must not inherit the real-estate form. A field counts
        # only when this call actually sent it.
        if custom_prompt and key not in meta:
            return ""
        value = meta.get(key) if key in meta else fallback
        return value if isinstance(value, str) else ("" if value is None else str(value))

    tool_ctx = AppointmentTools(ctx, phone_number, lead_name, is_inbound=is_inbound, persona_data=_inbound_persona_data)

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    # ── Build variables for {{dynamic}} prompt template ─────────────────────
    # persona prompt_vars (niche, goal, tone, etc.) merge with call metadata;
    # call-time fields (lead name, phone) always win.
    variables: dict = dict(prompt_vars)
    variables.update({
        "lead_name": lead_name,
        "lead_phone": phone_number or "",
        "caller_phone": phone_number or "",
        "business_name": business_name or "our company",
        "agent_name": agent_name_var,
        "service_type": _from_job("service_type", service_type) or "our service",
        "project_name": _from_job("project_name", project_name),
        "project_type": _from_job("project_type", project_type),
        "project_location": _from_job("project_location", project_location),
        "project_status": _from_job("project_status", project_status),
        "key_benefit_1": _from_job("key_benefit_1", key_benefit_1),
        "key_benefit_2": _from_job("key_benefit_2", key_benefit_2),
        "key_benefit_3": _from_job("key_benefit_3", key_benefit_3),
        "site_visit_day_1": _from_job("site_visit_day_1", site_visit_day_1),
        "site_visit_day_2": _from_job("site_visit_day_2", site_visit_day_2),
    })

    # ── Render the prompt: dynamic {{template}} or legacy {build_prompt} ─────
    if custom_prompt and "{{" in custom_prompt:
        system_prompt = render_prompt(custom_prompt, variables)
        await _log("info", "Prompt rendered from dynamic {{template}}")
    elif not custom_prompt and not is_inbound:
        from prompts import build_default_prompt
        system_prompt = build_default_prompt(variables)
        await _log("info", "Prompt rendered from generic {{default}} template")
    else:
        system_prompt = build_prompt(
            lead_name=lead_name, lead_phone=phone_number or "",
            business_name=business_name or "our company",
            service_type=_from_job("service_type", service_type),
            agent_name=agent_name_var,
            project_name=_from_job("project_name", project_name),
            project_type=_from_job("project_type", project_type),
            project_location=_from_job("project_location", project_location),
            project_status=_from_job("project_status", project_status),
            key_benefit_1=_from_job("key_benefit_1", key_benefit_1),
            key_benefit_2=_from_job("key_benefit_2", key_benefit_2),
            key_benefit_3=_from_job("key_benefit_3", key_benefit_3),
            site_visit_day_1=_from_job("site_visit_day_1", site_visit_day_1),
            site_visit_day_2=_from_job("site_visit_day_2", site_visit_day_2),
            custom_prompt=custom_prompt, inbound=is_inbound,
        )
    opening_line = ""
    if is_inbound:
        # Outbound skips hi/hello and jumps to the script. That ban makes an
        # inbound agent silent: the caller is already waiting, and the built-in
        # scripts have no sentence they are allowed to say.
        greeting = ""
        if isinstance(_inbound_persona_data, dict):
            greeting = str(_inbound_persona_data.get("greeting") or "")
        opening_line = inbound_opening_line(
            system_prompt,
            greeting=greeting,
            agent_name=agent_name_var,
            business_name=business_name or "",
        )
        system_prompt += (
            "\n\nOPENING LINE\n"
            "The caller is already on the line and is waiting to hear a voice. "
            "Ignore any earlier line that says not to greet, to wait, or to stay quiet — "
            "those do not apply to this first sentence. "
            "Say this exact sentence out loud now, then stop and listen. "
            "Do not call a tool before it.\n"
            f"\"{opening_line}\""
        )
        if greeting.strip() and not _spoken_quotes(greeting):
            system_prompt += (
                f"\nSay it in that spirit, without changing the words: {greeting.strip()}"
            )
        await _log("info", f"Inbound opening line: {opening_line}")
    else:
        system_prompt += (
            "\n\nOPENING GREETING RULE:\n"
            "The call just connected. Start talking immediately at this very first second! "
            "Greet the caller warmly according to your prompt instructions, introduce yourself and the company, "
            "and politely ask for a quick 2 minutes to talk. "
            "Do not wait for the caller to speak first."
        )
        await _log("info", "Outbound prompt configured with dynamic first-second greeting")

    system_prompt += (
        "\n\nCRITICAL RULES ON TOOL CALLS AND RESPONSES:\n"
        "1. NEVER STAY SILENT AFTER A TOOL RUNS! When any tool completes and returns its result, you MUST IMMEDIATELY speak back to the caller in 1-2 friendly, natural sentences without waiting for them to say anything.\n"
        "2. Communicate the tool result directly to the caller (e.g. confirm the slot is available, confirm the booking is done, confirm the SMS/email is sent).\n"
        "3. Never call lookup_contact at the start of the call. Always greet and talk first!\n"
        "4. Always call check_availability FIRST when a day/time is proposed. Announce that the slot is open and ask the caller to confirm before calling book_appointment. Never call both in the same breath!"
    )

    # ── Pre-load tools & build session BEFORE dialing so answering has zero latency ──
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    try:
        from prompts import build_tools_context, _tool_definition
        tools_context = build_tools_context(active_tools)
        await tool_ctx.load_http_tools()
        if tool_ctx.http_tools:
            await _log("info", f"HTTP tools loaded: {[t.get('name') for t in tool_ctx.http_tools]}")
            http_lines = [_tool_definition(t.get("name", "?"), t.get("description", "")) for t in tool_ctx.http_tools]
            tools_context = (tools_context + "\n" + "\n".join(http_lines)).strip()
        if "{{tools}}" in system_prompt:
            system_prompt = system_prompt.replace("{{tools}}", tools_context or "no custom integrations")
    except Exception as _hte:
        await _log("warning", f"Could not fill {{tools}} context: {_hte}")

    gemini_model = _live_model(model_override)
    model_override = gemini_model
    opening_voice = voice_override or os.getenv("GEMINI_TTS_VOICE", "Aoede")

    if audio_mode == "deepgram":
        if _deepgram_stt is None or not os.getenv("DEEPGRAM_API_KEY"):
            await _log("error", "This persona uses Deepgram, but DEEPGRAM_API_KEY is missing or the plugin is not installed")
            ctx.shutdown()
            return
        if not os.getenv("GOOGLE_API_KEY"):
            await _log("error", "Deepgram mode still needs GOOGLE_API_KEY so Gemini can think and speak")
            ctx.shutdown()
            return
        await _log("info", f"Building AI session — mode=deepgram+gemini model=gemini-2.5-flash voice={opening_voice}")
    else:
        await _log("info", f"Building AI session — mode={audio_mode or 'gemini-live'} model={gemini_model} voice={opening_voice}")

    session = _build_session(
        tools=active_tools,
        system_prompt=system_prompt,
        model=model_override,
        voice=voice_override,
        audio_mode=audio_mode,
    )
    tool_ctx.session = session  # Allow tools to speak natural verbal fillers while running

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
            from google.protobuf import duration_pb2 as _dpb
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=dial_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                    max_call_duration=_dpb.Duration(seconds=3600),
                    ringing_timeout=_dpb.Duration(seconds=60),
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

    # Use RoomOptions if available (non-deprecated), else fall back
    # Explicit close_on_disconnect=False so SIP audio blips/renegotiations don't drop calls at 2m
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_options=_RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony(), close_on_disconnect=False)),
        )
    else:
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony(), close_on_disconnect=False),
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

    # For inbound calls, wait briefly for the caller's audio bridge to connect
    # before speaking. Without this, the agent can speak into the void before
    # the Vobiz/SIP bridge has subscribed to the agent's audio track.
    if is_inbound:
        for _wait in range(10):
            if any(p.identity.startswith("sip_") or p.identity.startswith("+")
                   for p in ctx.room.remote_participants.values()):
                await _log("info", "Caller participant detected — ready to speak")
                break
            await asyncio.sleep(0.5)
        else:
            await _log("warning", "No caller participant detected after 5s — speaking anyway")

    # Gemini Live stays silent until a reply is requested. Outbound still skips
    # hi/hello and speaks the script. Inbound must say a concrete sentence: the
    # caller is already on the line, and "do not greet" with no allowed line
    # produces an empty turn.
    if is_inbound:
        opening = (
            "The caller just connected and is waiting to hear your voice. "
            "Start talking immediately at this very first second. "
            "Greet the caller warmly in your persona, state how you can help, and listen. "
            "Do not stay silent and do not wait for the caller to speak first."
        )
        if "3.1" in gemini_model and audio_mode != "deepgram":
            await _speak_opening_31(session, opening)
        else:
            await _speak_opening(session, opening)
    else:
        opening = (
            "The call just connected. Start talking immediately at this very first second. "
            "Greet the caller warmly according to your prompt, introduce yourself and the company, "
            "and politely ask for a quick 2 minutes to talk in 1-2 natural sentences. "
            "Do not wait for the caller to speak first."
        )
        if "3.1" in gemini_model and audio_mode != "deepgram":
            await _speak_opening_31(session, opening)
        else:
            await _speak_opening(session, opening)

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
