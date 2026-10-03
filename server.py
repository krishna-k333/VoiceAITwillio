"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import json
import logging
import os
import random
import secrets
import ssl
import sys
import time
import certifi
import aiohttp
from pathlib import Path
from typing import Optional

# Force UTF-8 stdout/stderr so emoji in print() don't crash on Windows cp1252 consoles
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from db import (
    SENSITIVE_KEYS, cancel_appointment, clear_errors, create_campaign, delete_campaign,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_all_agent_profiles, get_agent_profile, create_agent_profile, update_agent_profile,
    delete_agent_profile, set_default_agent_profile, get_billing_summary, get_calls_by_phone,
    get_campaign, get_contacts, get_errors, get_logs, get_setting, get_stats, init_db, log_error,
    save_settings, set_setting, update_call_notes, update_campaign_run_stats, update_campaign_status,
)
from prompts import DEFAULT_SYSTEM_PROMPT
from local_store import (
    active_persona_id, call_fields, delete_persona, get_active_persona, get_local_setting,
    get_persona, list_personas, save_persona, set_active_persona, set_local_setting,
)
from persona_builder import apify_token, build_from_target
from agent_service import (
    AgentError,
    create_from_ai,
    create_from_prompt,
    create_from_website,
    edit_selected_prompt,
    get_agent,
    list_agents,
    place_outbound_call,
    select_agent,
)

load_dotenv(".env", override=True)
ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

init_db()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")

# ── Auth ──────────────────────────────────────────────────────────────────────
_SESSIONS: dict = {}   # token → expiry timestamp
_SESSION_TTL = 86400   # 24 hours

_LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>MooreRevenue AI — Sign In</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',sans-serif;background:linear-gradient(145deg,#EAE6D8 0%,#F2EDD4 35%,#F8F3C6 65%,#F5E878 100%);min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1A1A1A;border-radius:24px;padding:48px 40px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.2)}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:32px}
.logo-mark{width:44px;height:44px;background:#F0C132;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px}
.logo-name{font-family:'Sora',sans-serif;font-weight:700;font-size:18px;color:#fff}
.logo-sub{font-size:12px;color:rgba(255,255,255,.35)}
h2{font-family:'Sora',sans-serif;font-size:22px;font-weight:700;color:#fff;margin-bottom:6px}
.sub{color:rgba(255,255,255,.4);font-size:14px;margin-bottom:28px}
label{display:block;font-size:11px;font-weight:600;color:rgba(255,255,255,.5);margin-bottom:6px;letter-spacing:.06em;text-transform:uppercase}
input{width:100%;background:rgba(255,255,255,.07);border:1.5px solid rgba(255,255,255,.12);border-radius:10px;color:#fff;font-family:'DM Sans',sans-serif;font-size:15px;padding:12px 14px;outline:none;transition:border-color .2s}
input:focus{border-color:#F0C132}
input::placeholder{color:rgba(255,255,255,.2)}
.field{margin-bottom:16px}
.err{color:#E8453C;font-size:13px;margin-bottom:14px;min-height:18px}
button{width:100%;background:#F0C132;color:#1A1A1A;border:none;border-radius:10px;font-family:'Sora',sans-serif;font-weight:700;font-size:15px;padding:14px;cursor:pointer;transition:background .15s,transform .1s;margin-top:4px}
button:hover{background:#F7DB6E}
button:active{transform:scale(.98)}
button:disabled{opacity:.6;cursor:not-allowed;transform:none}
</style></head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-mark">&#128222;</div>
    <div><div class="logo-name">MooreRevenue AI</div><div class="logo-sub">Admin Dashboard</div></div>
  </div>
  <h2>Welcome back</h2>
  <p class="sub">Enter your admin credentials to continue.</p>
  <form id="f">
    <div class="field"><label>Email</label><input type="email" id="e" placeholder="admin@example.com" autocomplete="username" required/></div>
    <div class="field"><label>Password</label><input type="password" id="p" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="current-password" required/></div>
    <div id="err" class="err"></div>
    <button type="submit" id="btn">Sign In</button>
  </form>
</div>
<script>
document.getElementById('f').addEventListener('submit',async ev=>{
  ev.preventDefault();
  const btn=document.getElementById('btn'),err=document.getElementById('err');
  btn.disabled=true;btn.textContent='Signing in…';err.textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('e').value,password:document.getElementById('p').value})});
    if(r.ok){window.location.href='/';return;}
    const d=await r.json().catch(()=>({}));
    err.textContent=d.detail||'Invalid credentials';
  }catch(ex){err.textContent='Network error. Please try again.';}
  btn.disabled=false;btn.textContent='Sign In';
});
</script>
</body></html>"""


def _agent_api_key_ok(request: Request) -> bool:
    expected = os.getenv("AGENT_API_KEY", "").strip()
    given = request.headers.get("x-api-key", "").strip()
    if not expected or not given or len(given) != len(expected):
        return False
    return secrets.compare_digest(given, expected)


def _check_session(token: str) -> bool:
    if not token:
        return False
    exp = _SESSIONS.get(token)
    if exp is None or time.time() > exp:
        _SESSIONS.pop(token, None)
        return False
    return True


class _AuthMiddleware(BaseHTTPMiddleware):
    _PUBLIC = {
        "/api/login", "/api/logout", "/api/auth/check",
        "/api/vobiz/answer", "/api/vobiz/hangup", "/api/vobiz/dial-status",
        "/webhook/vobiz/answer", "/webhook/vobiz/hangup", "/webhook/vobiz/dial-status",
    }

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in self._PUBLIC:
            return await call_next(request)
        if path.startswith("/api/agents") and _agent_api_key_ok(request):
            return await call_next(request)
        token = request.cookies.get("dashboard_session", "")
        if not _check_session(token):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)


app.add_middleware(_AuthMiddleware)


@app.on_event("startup")
async def _startup():
    # Ensure SIP_PROVIDER env var always wins over stale DB value
    env_provider = os.getenv("SIP_PROVIDER")
    if env_provider:
        try:
            await set_setting("SIP_PROVIDER", env_provider)
        except Exception as exc:
            logger.warning("Could not sync SIP_PROVIDER to Supabase: %s", exc)
    if _scheduler:
        try:
            _scheduler.start()
            await _reschedule_all_campaigns()
        except Exception as exc:
            logger.warning("Could not reschedule campaigns: %s", exc)


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    val = await get_setting(key, "")
    return val if val else os.getenv(key, "")


# ── Request models ────────────────────────────────────────────────────────────

class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "site visit"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None
    persona_id: Optional[str] = None
    sip_provider: Optional[str] = None
    # Real estate / project fields
    agent_name: Optional[str] = None
    project_name: Optional[str] = None
    project_type: Optional[str] = None
    project_location: Optional[str] = None
    project_status: Optional[str] = None
    key_benefit_1: Optional[str] = None
    key_benefit_2: Optional[str] = None
    key_benefit_3: Optional[str] = None
    site_visit_day_1: Optional[str] = None
    site_visit_day_2: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None
    sip_provider: Optional[str] = None


class StatusRequest(BaseModel):
    status: str


class LoginRequest(BaseModel):
    email: str
    password: str


# ── Dashboard & Health ────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    token = request.cookies.get("dashboard_session", "")
    if not _check_session(token):
        return HTMLResponse(_LOGIN_HTML)
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)


@app.post("/api/login")
async def api_login(req: LoginRequest):
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        raise HTTPException(500, "ADMIN_EMAIL / ADMIN_PASSWORD not set in .env")
    email_ok = secrets.compare_digest(req.email.strip().lower(), ADMIN_EMAIL.lower())
    pass_ok  = secrets.compare_digest(req.password, ADMIN_PASSWORD)
    if not (email_ok and pass_ok):
        raise HTTPException(401, "Invalid email or password")
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    resp = JSONResponse({"status": "ok"})
    resp.set_cookie("dashboard_session", token, httponly=True, samesite="lax", max_age=_SESSION_TTL)
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get("dashboard_session", "")
    _SESSIONS.pop(token, None)
    resp = JSONResponse({"status": "logged out"})
    resp.delete_cookie("dashboard_session")
    return resp


@app.get("/api/auth/check")
async def api_auth_check(request: Request):
    token = request.cookies.get("dashboard_session", "")
    if not _check_session(token):
        raise HTTPException(401, "Not authenticated")
    return {"status": "authenticated"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Call dispatch ─────────────────────────────────────────────────────────────

@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    try:
        return await place_outbound_call(
            phone=req.phone,
            lead_name=req.lead_name,
            business_name=req.business_name,
            service_type=req.service_type,
            system_prompt=req.system_prompt,
            agent_id=req.persona_id,
            agent_profile_id=req.agent_profile_id,
            sip_provider=req.sip_provider,
            agent_name=req.agent_name,
            project_name=req.project_name,
            project_type=req.project_type,
            project_location=req.project_location,
            project_status=req.project_status,
            key_benefit_1=req.key_benefit_1,
            key_benefit_2=req.key_benefit_2,
            key_benefit_3=req.key_benefit_3,
            site_visit_day_1=req.site_visit_day_1,
            site_visit_day_2=req.site_visit_day_2,
        )
    except AgentError as exc:
        logger.error("Dispatch error: %s", exc)
        raise HTTPException(exc.status, str(exc))


# ── Calls ─────────────────────────────────────────────────────────────────────

@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# ── Billing ───────────────────────────────────────────────────────────────────

@app.get("/api/billing")
async def api_get_billing():
    return await get_billing_summary()


# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Prompt ────────────────────────────────────────────────────────────────────

@app.get("/api/prompt")
async def api_get_prompt():
    saved = get_local_setting("system_prompt", "")
    if not saved:
        saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    set_local_setting("system_prompt", req.prompt)
    try:
        await set_setting("system_prompt", req.prompt)
    except Exception:
        pass
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    set_local_setting("system_prompt", "")
    try:
        await set_setting("system_prompt", "")
    except Exception:
        pass
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


class PersonaRequest(BaseModel):
    name: str
    agent_name: str = ""
    direction: str = "both"
    voice: str = "Sulafat"
    model: str = "gemini-3.1-flash-live-preview"
    audio_mode: str = "gemini"
    system_prompt: str = ""
    enabled_tools: str = "[]"
    source: str = "manual"
    source_ref: str = ""


class PersonaUseRequest(BaseModel):
    side: str


class PersonaBuildRequest(BaseModel):
    target: str
    direction: str = "both"
    audio_mode: str = "gemini"
    voice: str = "Sulafat"
    save: bool = True


def _persona_payload() -> dict:
    return {
        "personas": list_personas(),
        "active_inbound_id": active_persona_id("inbound"),
        "active_outbound_id": active_persona_id("outbound"),
        "apify_configured": bool(apify_token()),
    }


@app.get("/api/personas")
async def api_list_personas():
    return _persona_payload()


@app.post("/api/personas")
async def api_create_persona(req: PersonaRequest):
    if not req.name.strip():
        raise HTTPException(400, "Name is required")
    if req.audio_mode not in ("gemini", "deepgram"):
        raise HTTPException(400, "audio_mode must be gemini or deepgram")
    persona = save_persona(
        name=req.name, agent_name=req.agent_name, direction=req.direction,
        voice=req.voice, model=req.model, audio_mode=req.audio_mode,
        system_prompt=req.system_prompt, enabled_tools=req.enabled_tools,
        source=req.source, source_ref=req.source_ref,
    )
    return persona


@app.put("/api/personas/{persona_id}")
async def api_update_persona(persona_id: str, req: PersonaRequest):
    if req.audio_mode not in ("gemini", "deepgram"):
        raise HTTPException(400, "audio_mode must be gemini or deepgram")
    try:
        return save_persona(
            persona_id=persona_id, name=req.name, agent_name=req.agent_name,
            direction=req.direction, voice=req.voice, model=req.model,
            audio_mode=req.audio_mode, system_prompt=req.system_prompt,
            enabled_tools=req.enabled_tools, source=req.source, source_ref=req.source_ref,
        )
    except KeyError:
        raise HTTPException(404, "Persona not found")


@app.delete("/api/personas/{persona_id}")
async def api_delete_persona(persona_id: str):
    if not delete_persona(persona_id):
        raise HTTPException(404, "Persona not found")
    return {"status": "deleted"}


@app.post("/api/personas/{persona_id}/use")
async def api_use_persona(persona_id: str, req: PersonaUseRequest):
    if req.side not in ("inbound", "outbound"):
        raise HTTPException(400, "side must be inbound or outbound")
    try:
        set_active_persona(req.side, persona_id)
    except KeyError:
        raise HTTPException(404, "Persona not found")
    return {"status": "active", "side": req.side, "persona_id": persona_id}


@app.delete("/api/personas/active/{side}")
async def api_clear_active_persona(side: str):
    if side not in ("inbound", "outbound"):
        raise HTTPException(400, "side must be inbound or outbound")
    set_active_persona(side, None)
    return {"status": "cleared", "side": side}


@app.post("/api/personas/build")
async def api_build_persona(req: PersonaBuildRequest):
    if req.audio_mode not in ("gemini", "deepgram"):
        raise HTTPException(400, "audio_mode must be gemini or deepgram")
    try:
        built = await build_from_target(req.target, req.direction)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    built["audio_mode"] = req.audio_mode
    if req.voice:
        built["voice"] = req.voice
    if not req.save:
        return built
    persona = save_persona(
        name=built["name"], agent_name=built["agent_name"], direction=built["direction"],
        voice=built["voice"], model=built["model"], audio_mode=built["audio_mode"],
        system_prompt=built["system_prompt"], source=built["source"], source_ref=built["source_ref"],
    )
    return persona


class AgentPromptCreate(BaseModel):
    name: str
    system_prompt: str
    agent_name: str = ""
    direction: str = "both"
    voice: str = "Sulafat"
    audio_mode: str = "gemini"
    enabled_tools: Optional[list] = None


class AgentAiCreate(BaseModel):
    brief: str
    name: str = ""
    direction: str = "both"
    voice: str = "Sulafat"
    audio_mode: str = "gemini"


class AgentWebsiteCreate(BaseModel):
    url: str
    direction: str = "both"
    voice: str = "Sulafat"
    audio_mode: str = "gemini"


class AgentSelectRequest(BaseModel):
    side: str
    agent_id: Optional[str] = None


class AgentPromptEdit(BaseModel):
    side: str
    system_prompt: str


class AgentCallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "site visit"
    agent_id: Optional[str] = None


def _agent_http(exc: AgentError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@app.post("/api/agents/from-website")
async def api_agent_from_website(req: AgentWebsiteCreate):
    try:
        return await create_from_website(req.url, req.direction, req.audio_mode, req.voice)
    except AgentError as exc:
        raise _agent_http(exc)


@app.post("/api/agents/from-prompt")
async def api_agent_from_prompt(req: AgentPromptCreate):
    try:
        return create_from_prompt(
            req.name, req.system_prompt, req.agent_name, req.direction,
            req.voice, req.audio_mode, req.enabled_tools,
        )
    except AgentError as exc:
        raise _agent_http(exc)


@app.post("/api/agents/from-ai")
async def api_agent_from_ai(req: AgentAiCreate):
    try:
        return await create_from_ai(req.brief, req.name, req.direction, req.audio_mode, req.voice)
    except AgentError as exc:
        raise _agent_http(exc)


@app.get("/api/agents")
async def api_agent_list():
    return list_agents()


@app.post("/api/agents/select")
async def api_agent_select(req: AgentSelectRequest):
    try:
        return select_agent(req.side, req.agent_id)
    except AgentError as exc:
        raise _agent_http(exc)


@app.post("/api/agents/call")
async def api_agent_call(req: AgentCallRequest):
    try:
        return await place_outbound_call(
            phone=req.phone,
            lead_name=req.lead_name,
            business_name=req.business_name,
            service_type=req.service_type,
            agent_id=req.agent_id,
        )
    except AgentError as exc:
        raise _agent_http(exc)


@app.post("/api/agents/selected/prompt")
async def api_agent_edit_selected_prompt(req: AgentPromptEdit):
    try:
        return edit_selected_prompt(req.side, req.system_prompt)
    except AgentError as exc:
        raise _agent_http(exc)


@app.get("/api/agents/{agent_id}")
async def api_agent_detail(agent_id: str):
    try:
        return get_agent(agent_id)
    except AgentError as exc:
        raise _agent_http(exc)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    for k, v in filtered.items():
        os.environ[k] = str(v)
    return {"status": "saved", "count": len(filtered)}


# ── SIP trunk setup ───────────────────────────────────────────────────────────

@app.post("/api/setup/trunk")
async def api_setup_trunk(provider: str = "twilio"):
    url = key = secret = sip_domain = username = password = phone = ""
    trunk_id_setting = ""

    if provider == "voicelink":
        url      = await eff("LIVEKIT_URL")
        key      = await eff("LIVEKIT_API_KEY")
        secret   = await eff("LIVEKIT_API_SECRET")
        sip_domain = await eff("VOICELINK_SIP_DOMAIN")
        username   = await eff("VOICELINK_USERNAME")
        password   = await eff("VOICELINK_PASSWORD")
        phone      = await eff("VOICELINK_OUTBOUND_NUMBER")
        trunk_id_setting = "VOICELINK_TRUNK_ID"
        trunk_name = "VoiceLink Outbound Trunk"
    elif provider == "telnyx":
        url      = await eff("LIVEKIT_URL")
        key      = await eff("LIVEKIT_API_KEY")
        secret   = await eff("LIVEKIT_API_SECRET")
        sip_domain = "sip.telnyx.com"
        username   = await eff("TELNYX_USERNAME")
        password   = await eff("TELNYX_PASSWORD")
        phone      = await eff("TELNYX_OUTBOUND_NUMBER")
        trunk_id_setting = "TELNYX_TRUNK_ID"
        trunk_name = "Telnyx Outbound Trunk"
    elif provider == "twilio":
        url      = await eff("LIVEKIT_URL")
        key      = await eff("LIVEKIT_API_KEY")
        secret   = await eff("LIVEKIT_API_SECRET")
        sip_domain = await eff("TWILIO_SIP_TRUNK_DOMAIN")
        username   = await eff("TWILIO_SIP_USERNAME")
        password   = await eff("TWILIO_SIP_PASSWORD")
        phone      = await eff("TWILIO_OUTBOUND_NUMBER")
        trunk_id_setting = "TWILIO_TRUNK_ID"
        trunk_name = "Twilio Outbound Trunk"
    else:
        url      = await eff("LIVEKIT_URL")
        key      = await eff("LIVEKIT_API_KEY")
        secret   = await eff("LIVEKIT_API_SECRET")
        sip_domain = await eff("VOBIZ_SIP_DOMAIN")
        username   = await eff("VOBIZ_USERNAME")
        password   = await eff("VOBIZ_PASSWORD")
        phone      = await eff("VOBIZ_OUTBOUND_NUMBER")
        trunk_id_setting = "VOBIZ_TRUNK_ID"
        trunk_name = "Vobiz Outbound Trunk"

    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, f"Configure LiveKit and {provider.title()} credentials in Settings first.")

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk_info = lk_api.SIPOutboundTrunkInfo(
            name=trunk_name,
            address=sip_domain,
            auth_username=username,
            auth_password=password,
            numbers=[phone],
        )
        # Telnyx requires username in the first INVITE to force credential auth
        # (prevents accidental IP-based routing to a different account)
        if provider == "telnyx":
            trunk_info.headers["X-Telnyx-Username"] = username
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(trunk=trunk_info)
        )
        trunk_id = trunk.sip_trunk_id
        await set_setting(trunk_id_setting, trunk_id)
        os.environ[trunk_id_setting] = trunk_id
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await set_setting("SIP_PROVIDER", provider)
        os.environ["SIP_PROVIDER"] = provider
        await lk.aclose()
        await session.close()
        return {"status": "created", "trunk_id": trunk_id, "provider": provider}
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


# ── Inbound trunk setup ───────────────────────────────────────────────────────

@app.post("/api/setup/inbound-trunk")
async def api_setup_inbound_trunk(provider: str = "voicelink"):
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")

    if provider == "voicelink":
        did_number   = await eff("VOICELINK_OUTBOUND_NUMBER")
        allowed_ip   = "160.30.71.89"
        trunk_name   = "VoiceLink Inbound Trunk"
        setting_key  = "VOICELINK_INBOUND_TRUNK_ID"
    elif provider == "telnyx":
        did_number   = await eff("TELNYX_OUTBOUND_NUMBER")
        allowed_ip   = ""
        trunk_name   = "Telnyx Inbound Trunk"
        setting_key  = "TELNYX_INBOUND_TRUNK_ID"
    elif provider == "twilio":
        did_number   = await eff("TWILIO_INBOUND_NUMBER") or await eff("TWILIO_OUTBOUND_NUMBER")
        allowed_ip   = ""
        trunk_name   = "Twilio Inbound Trunk"
        setting_key  = "TWILIO_INBOUND_TRUNK_ID"
    else:
        did_number   = await eff("VOBIZ_OUTBOUND_NUMBER")
        allowed_ip   = ""
        trunk_name   = "Vobiz Inbound Trunk"
        setting_key  = "VOBIZ_INBOUND_TRUNK_ID"

    if not all([url, key, secret, did_number]):
        raise HTTPException(400, f"Configure LiveKit and {provider} credentials first.")

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)

        # ── Clean up any existing inbound trunks for this DID + their dispatch rules ──
        try:
            existing_trunks = await lk.sip.list_sip_inbound_trunk(lk_api.ListSIPInboundTrunkRequest())
            stale_trunk_ids = [t.sip_trunk_id for t in existing_trunks.items if did_number in (t.numbers or [])]
            if stale_trunk_ids:
                existing_rules = await lk.sip.list_sip_dispatch_rule(lk_api.ListSIPDispatchRuleRequest())
                for r in existing_rules.items:
                    if any(tid in (r.trunk_ids or []) for tid in stale_trunk_ids) or not r.trunk_ids:
                        try:
                            await lk.sip.delete_sip_dispatch_rule(
                                lk_api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=r.sip_dispatch_rule_id)
                            )
                        except Exception:
                            pass
                for tid in stale_trunk_ids:
                    try:
                        await lk.sip.delete_sip_trunk(lk_api.DeleteSIPTrunkRequest(sip_trunk_id=tid))
                    except Exception:
                        pass
        except Exception as _ce:
            logger.warning(f"Inbound cleanup skipped: {_ce}")

        trunk = await lk.sip.create_sip_inbound_trunk(
            lk_api.CreateSIPInboundTrunkRequest(
                trunk=lk_api.SIPInboundTrunkInfo(
                    name=trunk_name,
                    numbers=[did_number],
                    allowed_addresses=[allowed_ip] if allowed_ip else [],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id

        dispatch_rule = await lk.sip.create_sip_dispatch_rule(
            lk_api.CreateSIPDispatchRuleRequest(
                rule=lk_api.SIPDispatchRule(
                    dispatch_rule_individual=lk_api.SIPDispatchRuleIndividual(
                        room_prefix="inbound-",
                    )
                ),
                trunk_ids=[trunk_id],
                name=f"{trunk_name} Dispatch",
                room_config=lk_api.RoomConfiguration(
                    agents=[lk_api.RoomAgentDispatch(
                        agent_name="outbound-caller",
                        metadata=json.dumps({"inbound": True}),
                    )],
                ),
            )
        )

        await set_setting(setting_key, trunk_id)
        os.environ[setting_key] = trunk_id
        await set_setting("INBOUND_TRUNK_ID", trunk_id)
        os.environ["INBOUND_TRUNK_ID"] = trunk_id

        await lk.aclose()
        await session.close()
        return {
            "status": "created", "trunk_id": trunk_id,
            "dispatch_rule_id": dispatch_rule.sip_dispatch_rule_id,
            "did": did_number, "provider": provider,
        }
    except Exception as exc:
        raise HTTPException(500, f"Inbound trunk creation failed: {exc}")


# ── Vobiz Voice Application Webhook ──────────────────────────────────────────

@app.api_route("/api/vobiz/answer", methods=["GET", "POST"])
@app.api_route("/webhook/vobiz/answer", methods=["GET", "POST"])
async def vobiz_answer_webhook(request: Request):
    body = await request.body()
    logger.info(f"Vobiz answer webhook: query={dict(request.query_params)} body={body.decode('utf-8', errors='ignore')}")
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true" audioTrack="inbound" contentType="audio/x-l16;rate=16000">wss://voicees.moorerevenue.com/ws/vobiz</Stream>
</Response>"""
    return Response(content=xml_content, media_type="application/xml")


@app.websocket("/ws/vobiz")
async def websocket_vobiz_endpoint(websocket: WebSocket):
    from vobiz_bridge import handle_vobiz_websocket
    await handle_vobiz_websocket(websocket)


@app.api_route("/api/vobiz/dial-status", methods=["GET", "POST"])
@app.api_route("/webhook/vobiz/dial-status", methods=["GET", "POST"])
async def vobiz_dial_status_webhook(request: Request):
    body = await request.body()
    logger.info(f"Vobiz dial-status webhook: query={dict(request.query_params)} body={body.decode('utf-8', errors='ignore')}")
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response/>", media_type="application/xml")


@app.api_route("/api/vobiz/hangup", methods=["GET", "POST"])
@app.api_route("/webhook/vobiz/hangup", methods=["GET", "POST"])
async def vobiz_hangup_webhook(request: Request):
    body = await request.body()
    logger.info(f"Vobiz hangup webhook: query={dict(request.query_params)} body={body.decode('utf-8', errors='ignore')}")
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response/>", media_type="application/xml")


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


# ── CRM ───────────────────────────────────────────────────────────────────────

@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


# ── Agent Profiles ────────────────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, model=req.model,
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools, is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {
        "name": req.name, "voice": req.voice, "model": req.model,
        "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
        "is_default": 1 if req.is_default else 0,
    })
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Inbound Personas ─────────────────────────────────────────────────────────

INBOUND_PERSONAS = {
    "raj_dental": {
        "id": "raj_dental",
        "name": "Raj Dental Care",
        "agent_name": "Priya",
        "voice": "Sulafat",
        "language": "Hinglish",
        "use_case": "Dental Clinic Receptionist",
        "description": "AI receptionist for Raj Dental Care in Faridabad. Books dental appointments and answers clinic queries in warm Hinglish.",
        "color": "#1a73e8",
    },
    "edu_agent": {
        "id": "edu_agent",
        "name": "Tech Academy",
        "agent_name": "Dennis",
        "voice": "Puck",
        "language": "English",
        "use_case": "Education Enrollment Counselor",
        "description": "AI counselor for Tech Academy. Helps students enroll in coding bootcamps and schedules free consultations.",
        "color": "#4285F4",
    },
    "customer_support": {
        "id": "customer_support",
        "name": "Global Support Center",
        "agent_name": "Sarah",
        "voice": "Kore",
        "language": "English",
        "use_case": "L1 Customer Support",
        "description": "AI support rep for Global Retail. Handles refund requests, technical issues, and schedules callbacks for complex cases.",
        "color": "#d93025",
    },
    "edu_outbound_demo": {
        "id": "edu_outbound_demo",
        "name": "Tech Academy (Outbound)",
        "agent_name": "Priya",
        "voice": "Sulafat",
        "language": "Hinglish",
        "use_case": "Education Outbound Persuasion",
        "description": "Proactively calls leads who showed interest in coding bootcamps. Uses FOMO and warm persuasion in Hinglish.",
        "color": "#0f9d58",
    },
    "holi_wishes": {
        "id": "holi_wishes",
        "name": "Festival Greetings",
        "agent_name": "AI Assistant",
        "voice": "Puck",
        "language": "Hindi / Hinglish",
        "use_case": "Festival Greeting Calls",
        "description": "Sends personalized Holi greetings on behalf of Krishna Aggarwal. Short 15-20 second warm and festive wish calls.",
        "color": "#e91e63",
    },
    "hvac_demo": {
        "id": "hvac_demo",
        "name": "CoolBreeze HVAC",
        "agent_name": "Alex",
        "voice": "Aoede",
        "language": "English",
        "use_case": "HVAC Inbound Receptionist",
        "description": "24/7 AI dispatcher for CoolBreeze HVAC. Triages emergencies, books service visits, and handles the full inbound call flow with natural HVAC terminology.",
        "color": "#0077b6",
    },
    "real_estate": {
        "id": "real_estate",
        "name": "Prestige Realty",
        "agent_name": "Ananya",
        "voice": "Aoede",
        "language": "English / Hinglish",
        "use_case": "Real Estate Consultant",
        "description": "AI property consultant for Prestige Realty. Books site visits and consultations for buyers, sellers, and renters across Delhi NCR, Mumbai, Bengaluru, and more.",
        "color": "#b8860b",
    },
}


@app.get("/api/inbound/personas")
async def api_get_inbound_personas():
    active = await get_setting("INBOUND_ACTIVE_PERSONA", "raj_dental")
    result = []
    for persona in INBOUND_PERSONAS.values():
        p = dict(persona)
        p["is_active"] = (p["id"] == active)
        result.append(p)
    return {"data": result, "active_persona_id": active}


@app.post("/api/inbound/personas/{persona_id}/activate")
async def api_activate_inbound_persona(persona_id: str):
    if persona_id not in INBOUND_PERSONAS:
        raise HTTPException(404, f"Persona '{persona_id}' not found")
    await set_setting("INBOUND_ACTIVE_PERSONA", persona_id)
    return {"status": "activated", "persona_id": persona_id}


@app.get("/api/inbound/status")
async def api_get_inbound_status():
    trunk_id = await get_setting("INBOUND_TRUNK_ID", "")
    did = (await get_setting("VOBIZ_INBOUND_NUMBER", "")) or (await get_setting("VOICELINK_INBOUND_NUMBER", ""))
    local = get_active_persona("inbound")
    if local:
        persona = {
            "id": local["id"],
            "name": local["name"],
            "agent_name": local.get("agent_name") or local["name"],
            "voice": local.get("voice") or "",
            "audio_mode": local.get("audio_mode") or "gemini",
            "is_active": True,
        }
    else:
        active_id = await get_setting("INBOUND_ACTIVE_PERSONA", "raj_dental")
        persona = {**INBOUND_PERSONAS.get(active_id, INBOUND_PERSONAS["raj_dental"]), "is_active": True}
    return {
        "active_persona": persona,
        "trunk_id": trunk_id,
        "did_number": did,
        "is_configured": bool(trunk_id),
    }


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def _dispatch_one(lk, lk_api, contact: dict, room_name: str,
                         prompt: Optional[str], profile: Optional[dict] = None,
                         sip_provider: str = "twilio") -> bool:
    try:
        persona = get_active_persona("outbound")
        fields = call_fields(persona)
        saved_prompt = (
            prompt
            or fields.get("system_prompt")
            or get_local_setting("system_prompt", "")
            or (await get_setting("system_prompt", ""))
            or None
        )
        metadata: dict = {
            "phone_number": contact["phone"],
            "lead_name": contact.get("lead_name", "there"),
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "system_prompt": saved_prompt,
            "sip_provider": sip_provider,
        }
        if fields.get("voice_override"):
            metadata["voice_override"] = fields["voice_override"]
        if fields.get("model_override"):
            metadata["model_override"] = fields["model_override"]
        if fields.get("tools_override"):
            metadata["tools_override"] = fields["tools_override"]
        if fields.get("audio_mode"):
            metadata["audio_mode"] = fields["audio_mode"]
        if fields.get("persona_id"):
            metadata["persona_id"] = fields["persona_id"]
        if fields.get("agent_name"):
            metadata["agent_name"] = fields["agent_name"]
        # An active outbound persona wins over an old agent profile.
        # A prompt typed on the campaign itself still replaces the persona's words.
        if profile and not persona:
            if profile.get("system_prompt") and not prompt:
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):
                metadata["voice_override"] = profile["voice"]
            if profile.get("model"):
                metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"):
                metadata["tools_override"] = profile["enabled_tools"]
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata))
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    delay = int(campaign.get("call_delay_seconds") or 3)
    prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    sip_provider = os.getenv("SIP_PROVIDER") or await get_setting("SIP_PROVIDER", "twilio")
    profile = None
    if agent_profile_id:
        profile = await get_agent_profile(agent_profile_id)

    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    from livekit import api as lk_api_module
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))

    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            phone = contact.get("phone", "")
            if not phone.startswith("+"):
                fail_count += 1
                continue
            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            success = await _dispatch_one(lk, lk_api_module, contact, room_name, prompt, profile, sip_provider)
            if success:
                ok_count += 1
            else:
                fail_count += 1
            if i < len(contacts) - 1:
                await asyncio.sleep(delay)
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc)
    finally:
        await session.close()

    await update_campaign_run_stats(campaign_id, ok_count, fail_count)
    logger.info("Campaign %s done — %d dispatched, %d failed", campaign_id, ok_count, fail_count)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds, system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}
