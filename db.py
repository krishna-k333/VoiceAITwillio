import os
import uuid
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

# ---------------------------------------------------------------------------
# DEFAULTS — all loaded from environment variables only.
# Never hardcode real credentials here. Use Coolify env vars or .env file.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "LIVEKIT_URL":             os.getenv("LIVEKIT_URL", ""),
    "LIVEKIT_API_KEY":         os.getenv("LIVEKIT_API_KEY", ""),
    "LIVEKIT_API_SECRET":      os.getenv("LIVEKIT_API_SECRET", ""),
    "GOOGLE_API_KEY":          os.getenv("GOOGLE_API_KEY", ""),
    "GEMINI_MODEL":            os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
    "GEMINI_TTS_VOICE":        os.getenv("GEMINI_TTS_VOICE", "Aoede"),
    "USE_GEMINI_REALTIME":     os.getenv("USE_GEMINI_REALTIME", "true"),
    "SIP_PROVIDER":             os.getenv("SIP_PROVIDER", "twilio"),
    "VOBIZ_SIP_DOMAIN":        os.getenv("VOBIZ_SIP_DOMAIN", ""),
    "VOBIZ_USERNAME":          os.getenv("VOBIZ_USERNAME", ""),
    "VOBIZ_PASSWORD":          os.getenv("VOBIZ_PASSWORD", ""),
    "VOBIZ_OUTBOUND_NUMBER":   os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
    "VOBIZ_TRUNK_ID":          os.getenv("VOBIZ_TRUNK_ID", ""),
    "VOICELINK_SIP_DOMAIN":    os.getenv("VOICELINK_SIP_DOMAIN", ""),
    "VOICELINK_USERNAME":      os.getenv("VOICELINK_USERNAME", ""),
    "VOICELINK_PASSWORD":      os.getenv("VOICELINK_PASSWORD", ""),
    "VOICELINK_OUTBOUND_NUMBER": os.getenv("VOICELINK_OUTBOUND_NUMBER", ""),
    "VOICELINK_TRUNK_ID":      os.getenv("VOICELINK_TRUNK_ID", ""),
    "OUTBOUND_TRUNK_ID":       os.getenv("OUTBOUND_TRUNK_ID", ""),
    "DEFAULT_TRANSFER_NUMBER": os.getenv("DEFAULT_TRANSFER_NUMBER", ""),
    "SUPABASE_URL":            os.getenv("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY":    os.getenv("SUPABASE_SERVICE_KEY", ""),
    "DEEPGRAM_API_KEY":        os.getenv("DEEPGRAM_API_KEY", ""),
}


def _default(key: str) -> str:
    return os.getenv(key, DEFAULTS.get(key, ""))


SUPABASE_URL = _default("SUPABASE_URL")
SUPABASE_KEY = _default("SUPABASE_SERVICE_KEY")

SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY",
    "VOBIZ_PASSWORD", "VOICELINK_PASSWORD", "TWILIO_AUTH_TOKEN",
    "SUPABASE_SERVICE_KEY", "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY",
    "CALCOM_API_KEY", "DEEPGRAM_API_KEY",
}


def _sdb():
    from supabase import create_client
    return create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))


async def _adb():
    from supabase._async.client import create_client
    return await create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))


def init_db() -> None:
    url = os.getenv("SUPABASE_URL", SUPABASE_URL)
    key = os.getenv("SUPABASE_SERVICE_KEY", SUPABASE_KEY)
    if not url or not key:
        print("⚠️  SUPABASE_URL or SUPABASE_SERVICE_KEY not set.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        print("✅ Supabase connected")
    except Exception as exc:
        print(f"⚠️  Supabase connection failed: {exc}")
        print("   Run supabase_schema.sql in your Supabase Dashboard → SQL Editor")


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_all_settings() -> dict:
    db = await _adb()
    result = await db.table("settings").select("key, value").execute()
    KNOWN_KEYS = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
        "SIP_PROVIDER",
        "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
        "VOBIZ_OUTBOUND_NUMBER", "VOBIZ_TRUNK_ID",
        "VOICELINK_SIP_DOMAIN", "VOICELINK_USERNAME", "VOICELINK_PASSWORD",
        "VOICELINK_OUTBOUND_NUMBER", "VOICELINK_TRUNK_ID",
        "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
        "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
        "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
        "ENABLED_TOOLS",
    ]
    out: dict = {}
    for k in KNOWN_KEYS:
        env_val = _default(k)
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(env_val)}
        else:
            out[k] = {"value": env_val, "configured": bool(env_val)}
    for row in (result.data or []):
        k, v = row["key"], row["value"]
        if k == "TEST_KEY":
            continue
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(v)}
        else:
            out[k] = {"value": v, "configured": bool(v)}
    return out


async def save_settings(data: dict) -> None:
    db = await _adb()
    updated_at = datetime.now().isoformat()
    rows = [
        {"key": k, "value": str(v), "updated_at": updated_at}
        for k, v in data.items()
        if v is not None and v != ""
    ]
    if rows:
        await db.table("settings").upsert(rows, on_conflict="key").execute()


async def get_setting(key: str, default: str = "") -> str:
    db = await _adb()
    result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
    if result and result.data:
        return result.data["value"]
    return _default(key) or default


async def set_setting(key: str, value: str) -> None:
    db = await _adb()
    await db.table("settings").upsert(
        {"key": key, "value": value, "updated_at": datetime.now().isoformat()},
        on_conflict="key",
    ).execute()


async def get_enabled_tools() -> list:
    raw = await get_setting("ENABLED_TOOLS", "")
    if not raw:
        return []
    try:
        import json
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Error logs ────────────────────────────────────────────────────────────────

async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({
            "id": str(uuid.uuid4()),
            "source": source,
            "level": level,
            "message": message[:500],
            "detail": detail[:2000],
            "timestamp": datetime.now().isoformat(),
        }).execute()
    except Exception:
        pass


async def get_errors(limit: int = 100) -> list:
    db = await _adb()
    result = await db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    db = await _adb()
    query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level:
        query = query.eq("level", level)
    if source:
        query = query.eq("source", source)
    result = await query.execute()
    return result.data or []


async def clear_errors() -> None:
    db = await _adb()
    await db.table("error_logs").delete().neq("id", "").execute()


# ── Appointments ──────────────────────────────────────────────────────────────

async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    db = await _adb()
    await db.table("appointments").insert({
        "id": full_id, "name": name, "phone": phone,
        "date": date, "time": time, "service": service,
        "status": "booked", "created_at": datetime.now().isoformat(),
    }).execute()
    return booking_id


async def check_slot(date: str, time: str) -> bool:
    """Returns True if slot is available (no existing booking)."""
    db = await _adb()
    result = await (
        db.table("appointments").select("id")
        .eq("date", date).eq("time", time).eq("status", "booked")
        .maybe_single().execute()
    )
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    result = await query.execute()
    return result.data or []


async def cancel_appointment(appointment_id: str) -> bool:
    db = await _adb()
    result = await (
        db.table("appointments").update({"status": "cancelled"})
        .eq("id", appointment_id).eq("status", "booked").execute()
    )
    return len(result.data or []) > 0


async def get_appointments_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()
    return result.data or []


# ── Call logs ─────────────────────────────────────────────────────────────────

async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    ended_by: str = "unknown",
) -> None:
    db = await _adb()
    row: dict = {
        "id": str(uuid.uuid4()), "phone_number": phone_number, "lead_name": lead_name,
        "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds,
        "timestamp": datetime.now().isoformat(), "ended_by": ended_by,
    }
    if recording_url:
        row["recording_url"] = recording_url
    if notes:
        row["notes"] = notes
    try:
        await db.table("call_logs").insert(row).execute()
    except Exception as _e:
        # If ended_by column doesn't exist yet (migration pending), retry without it
        if "ended_by" in str(_e):
            row.pop("ended_by", None)
            await db.table("call_logs").insert(row).execute()
        else:
            raise


def log_call_sync(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, ended_by: str = "unknown",
) -> None:
    """Synchronous call logger — safe to call from threads or during event-loop teardown."""
    import requests as _req
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return
    row = {
        "id": str(uuid.uuid4()), "phone_number": phone_number,
        "lead_name": lead_name, "outcome": outcome, "reason": reason,
        "duration_seconds": duration_seconds,
        "timestamp": datetime.now().isoformat(), "ended_by": ended_by,
    }
    if recording_url:
        row["recording_url"] = recording_url
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    endpoint = f"{url.rstrip('/')}/rest/v1/call_logs"
    try:
        resp = _req.post(endpoint, json=row, headers=headers, timeout=10)
        if resp.status_code >= 300:
            import logging as _logging
            # If ended_by column missing (migration not run yet), retry without it
            if resp.status_code == 400 and "ended_by" in resp.text:
                row.pop("ended_by", None)
                resp2 = _req.post(endpoint, json=row, headers=headers, timeout=10)
                if resp2.status_code >= 300:
                    _logging.getLogger("outbound-agent").error(
                        "log_call_sync retry HTTP %s: %s", resp2.status_code, resp2.text[:200]
                    )
            else:
                _logging.getLogger("outbound-agent").error(
                    "log_call_sync HTTP %s: %s", resp.status_code, resp.text[:200]
                )
    except Exception as _e:
        import logging as _logging
        _logging.getLogger("outbound-agent").error("log_call_sync failed: %s", _e)


def _effective_call_duration(row: dict) -> int:
    if row.get("outcome") == "no_answer":
        return 0
    return int(row.get("duration_seconds") or 0)


def _normalize_call_durations(rows: list) -> list:
    for row in rows:
        row["duration_seconds"] = _effective_call_duration(row)
    return rows


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + limit - 1).execute()
    return _normalize_call_durations(result.data or [])


async def get_calls_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()
    return _normalize_call_durations(result.data or [])


async def update_call_notes(call_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_contacts() -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).execute()
    rows = result.data or []
    contacts: dict = {}
    for row in rows:
        phone = row["phone_number"]
        if phone not in contacts:
            contacts[phone] = {
                "phone_number": phone, "lead_name": row.get("lead_name"),
                "total_calls": 0, "booked": 0,
                "last_call": row["timestamp"], "last_outcome": row.get("outcome"),
            }
        contacts[phone]["total_calls"] += 1
        if row.get("outcome") == "booked":
            contacts[phone]["booked"] += 1
    return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    db = await _adb()
    rows = (await db.table("call_logs").select("outcome, duration_seconds, timestamp").execute()).data or []
    total_calls    = len(rows)
    booked         = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations      = [_effective_call_duration(r) for r in rows if _effective_call_duration(r)]
    avg_dur        = sum(durations) / len(durations) if durations else 0
    booking_rate   = round((booked / total_calls * 100) if total_calls else 0, 1)
    # Outcomes breakdown
    outcomes: dict = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1
    # Timeline: calls per day last 14 days
    daily: dict = defaultdict(int)
    for r in rows:
        ts = (r.get("timestamp") or "")[:10]
        if ts:
            daily[ts] += 1
    today = datetime.now().date()
    timeline = [{"date": (today - timedelta(days=i)).isoformat(), "count": daily.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]
    # Avg duration by outcome
    dur_sum: dict = defaultdict(float)
    dur_cnt: dict = defaultdict(int)
    for r in rows:
        o = r.get("outcome") or "unknown"
        sec = _effective_call_duration(r)
        if sec:
            dur_sum[o] += sec
            dur_cnt[o] += 1
    duration_by_outcome = {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}
    return {
        "total_calls": total_calls, "booked": booked, "not_interested": not_interested,
        "avg_duration_seconds": round(avg_dur, 1), "booking_rate_percent": booking_rate,
        "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
    }


async def get_cut_calls_stats() -> dict:
    """Stats for calls that were cut/hung-up. Works with or without the ended_by column."""
    db = await _adb()
    try:
        rows = (
            await db.table("call_logs")
            .select("id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, ended_by")
            .order("timestamp", desc=True)
            .execute()
        ).data or []
        has_ended_by = True
    except Exception:
        # ended_by column not yet migrated — fall back to selecting without it
        rows = (
            await db.table("call_logs")
            .select("id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp")
            .order("timestamp", desc=True)
            .execute()
        ).data or []
        has_ended_by = False

    total_calls = len(rows)

    def _is_cut(r: dict) -> bool:
        """A call is cut if ended_by=caller_hungup, OR (no ended_by column yet and outcome=dropped)."""
        eb = r.get("ended_by")
        if eb == "caller_hungup":
            return True
        # Before migration: dropped = caller hung up before agent finished
        if not has_ended_by or eb in (None, "unknown"):
            return r.get("outcome") == "dropped"
        return False

    def _is_completed(r: dict) -> bool:
        eb = r.get("ended_by")
        if eb == "agent":
            return True
        if not has_ended_by or eb in (None, "unknown"):
            return r.get("outcome") not in ("dropped", "no_answer")
        return False

    cut_rows = [r for r in rows if _is_cut(r)]
    completed_rows = [r for r in rows if _is_completed(r)]

    cut_durations = [r["duration_seconds"] for r in cut_rows if r.get("duration_seconds")]
    avg_cut_dur = sum(cut_durations) / len(cut_durations) if cut_durations else 0
    total_cut_talk_time = sum(cut_durations)

    # Daily timeline for cut calls (last 14 days)
    daily_cuts: dict = defaultdict(int)
    for r in cut_rows:
        ts = (r.get("timestamp") or "")[:10]
        if ts:
            daily_cuts[ts] += 1
    today = datetime.now().date()
    cut_timeline = [
        {"date": (today - timedelta(days=i)).isoformat(),
         "count": daily_cuts.get((today - timedelta(days=i)).isoformat(), 0)}
        for i in range(13, -1, -1)
    ]

    # Recent 50 cut calls
    recent_cut_calls = [
        {
            "id": r.get("id"),
            "phone_number": r.get("phone_number"),
            "lead_name": r.get("lead_name") or "—",
            "outcome": r.get("outcome"),
            "reason": r.get("reason"),
            "duration_seconds": r.get("duration_seconds") or 0,
            "timestamp": r.get("timestamp"),
            "ended_by": r.get("ended_by") or ("caller_hungup" if r.get("outcome") == "dropped" else "unknown"),
        }
        for r in cut_rows[:50]
    ]

    return {
        "total_calls": total_calls,
        "total_cut_calls": len(cut_rows),
        "total_completed_calls": len(completed_rows),
        "total_system_calls": total_calls - len(cut_rows) - len(completed_rows),
        "cut_rate_percent": round((len(cut_rows) / total_calls * 100) if total_calls else 0, 1),
        "avg_cut_duration_seconds": round(avg_cut_dur, 1),
        "total_cut_talk_time_seconds": total_cut_talk_time,
        "cut_timeline": cut_timeline,
        "recent_cut_calls": recent_cut_calls,
    }


# ── Billing ──────────────────────────────────────────────────────────────────

import math as _math

_RATE_PER_MIN = 5.0  # ₹ per billed minute — internal only


def _billed_minutes(seconds: int) -> float:
    """Round up to nearest 0.5 min."""
    if not seconds or seconds <= 0:
        return 0.0
    return _math.ceil((seconds / 60) * 2) / 2


async def get_billing_summary() -> dict:
    db = await _adb()
    rows = (
        await db.table("call_logs")
        .select("id, lead_name, phone_number, outcome, duration_seconds, timestamp")
        .order("timestamp", desc=True)
        .execute()
    ).data or []

    total_billed_mins = 0.0
    monthly: dict = {}
    recent_calls = []

    for r in rows:
        secs = _effective_call_duration(r)
        bm = _billed_minutes(secs)
        cost = round(bm * _RATE_PER_MIN, 2)
        total_billed_mins += bm

        # Monthly grouping (YYYY-MM)
        ts = (r.get("timestamp") or "")[:7]
        if ts:
            if ts not in monthly:
                monthly[ts] = {"month": ts, "calls": 0, "billed_minutes": 0.0, "cost": 0.0}
            monthly[ts]["calls"] += 1
            monthly[ts]["billed_minutes"] += bm
            monthly[ts]["cost"] = round(monthly[ts]["cost"] + cost, 2)

        if len(recent_calls) < 50:
            recent_calls.append({
                "id": r.get("id"),
                "lead_name": r.get("lead_name") or "—",
                "phone_number": r.get("phone_number"),
                "outcome": r.get("outcome"),
                "duration_seconds": secs,
                "billed_minutes": bm,
                "cost": cost,
                "timestamp": r.get("timestamp"),
            })

    total_cost = round(total_billed_mins * _RATE_PER_MIN, 2)

    # This month
    this_month = datetime.now().strftime("%Y-%m")
    this_month_data = monthly.get(this_month, {"calls": 0, "billed_minutes": 0.0, "cost": 0.0})

    monthly_list = sorted(monthly.values(), key=lambda x: x["month"], reverse=True)
    for m in monthly_list:
        m["billed_minutes"] = round(m["billed_minutes"], 1)

    return {
        "total_calls": len(rows),
        "total_billed_minutes": round(total_billed_mins, 1),
        "total_cost": total_cost,
        "this_month_calls": this_month_data["calls"],
        "this_month_billed_minutes": round(this_month_data["billed_minutes"], 1),
        "this_month_cost": this_month_data["cost"],
        "monthly_breakdown": monthly_list,
        "recent_calls": recent_calls,
    }


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def create_campaign(
    name: str, contacts_json: str, schedule_type: str = "once",
    schedule_time: str = "09:00", call_delay_seconds: int = 3,
    system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None,
) -> str:
    campaign_id = str(uuid.uuid4())
    db = await _adb()
    row: dict = {
        "id": campaign_id, "name": name, "status": "active",
        "contacts_json": contacts_json, "schedule_type": schedule_type,
        "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds,
        "created_at": datetime.now().isoformat(), "total_dispatched": 0, "total_failed": 0,
    }
    if system_prompt:
        row["system_prompt"] = system_prompt
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    await db.table("campaigns").insert(row).execute()
    return campaign_id


async def get_all_campaigns() -> list:
    db = await _adb()
    result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    return result.data or []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    return result.data if result else None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    db = await _adb()
    await db.table("campaigns").update({
        "last_run_at": datetime.now().isoformat(),
        "total_dispatched": dispatched, "total_failed": failed, "status": "completed",
    }).eq("id", campaign_id).execute()


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    return len(result.data or []) > 0


# ── Contact Memory ────────────────────────────────────────────────────────────

async def add_contact_memory(phone: str, insight: str) -> None:
    db = await _adb()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone,
        "insight": insight[:1000], "created_at": datetime.now().isoformat(),
    }).execute()


async def get_contact_memory(phone: str) -> list:
    db = await _adb()
    result = await (
        db.table("contact_memory").select("insight, created_at")
        .eq("phone_number", phone).order("created_at", desc=True).limit(20).execute()
    )
    return result.data or []


async def compress_contact_memory(phone: str, compressed: str) -> None:
    db = await _adb()
    await db.table("contact_memory").delete().eq("phone_number", phone).execute()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone,
        "insight": compressed[:2000], "created_at": datetime.now().isoformat(),
    }).execute()


# ── Agent Profiles ────────────────────────────────────────────────────────────

async def get_all_agent_profiles() -> list:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").order("created_at").execute()
    return result.data or []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    return result.data if result else None


async def create_agent_profile(
    name: str, voice: str = "Aoede", model: str = "gemini-3.1-flash-live-preview",
    system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False,
) -> str:
    profile_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").insert({
        "id": profile_id, "name": name, "voice": voice, "model": model,
        "system_prompt": system_prompt, "enabled_tools": enabled_tools,
        "is_default": 1 if is_default else 0, "created_at": datetime.now().isoformat(),
    }).execute()
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def delete_agent_profile(profile_id: str) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def set_default_agent_profile(profile_id: str) -> None:
    db = await _adb()
    await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()
