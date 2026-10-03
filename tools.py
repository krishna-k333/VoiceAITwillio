import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import llm

from db import (
    check_slot, get_next_available, insert_appointment, log_call, log_error,
    get_calls_by_phone, get_appointments_by_phone,
    add_contact_memory, get_contact_memory, compress_contact_memory,
    get_http_tools,
)

logger = logging.getLogger("appointment-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def _substitute(text: str, values: dict) -> str:
    """Replace {{key}} placeholders in a string with values from a dict."""
    import re as _re
    pattern = _re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
    def _repl(m: _re.Match) -> str:
        key = m.group(1)
        return str(values[key]) if key in values and values[key] is not None else m.group(0)
    return pattern.sub(_repl, text)


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_name: Optional[str] = None,
        is_inbound: bool = False,
        persona_data: Optional[dict] = None,
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.is_inbound = is_inbound
        self.persona_data = persona_data  # used for Google Calendar + Gmail on inbound calls
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self._call_logged = False  # set True when end_call() fires so agent.py won't double-log
        self._booking_completed = False
        self._last_booking_summary = ""
        self.http_tools: list = []  # user-configured HTTP request tools
        super().__init__(tools=[])

    async def load_http_tools(self) -> None:
        """Load enabled user-defined HTTP request tools from the database."""
        try:
            self.http_tools = await get_http_tools(enabled_only=True)
        except Exception as exc:
            logger.warning("Could not load HTTP tools: %s", exc)
            self.http_tools = []

    def build_tool_list(self, enabled: list) -> list:
        """Return tool methods filtered by the enabled list. Empty list = all enabled."""
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call, self.hangup,
            self.transfer_to_human, self.send_sms_confirmation, self.send_email,
            self.create_google_calendar_event, self.http_request,
            self.lookup_contact, self.remember_details, self.book_calcom, self.cancel_calcom,
        ]
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """
        Check whether a date/time slot is available for booking.
        Call this BEFORE attempting to book whenever the lead proposes a date/time.
        date format: YYYY-MM-DD  |  time format: HH:MM (24-hour)
        Returns 'available' or 'unavailable: next available slot is <slot>'.
        """
        try:
            if await check_slot(date, time):
                return "available"
            next_slot = await get_next_available(date, time)
            return f"unavailable: next available slot is {next_slot}"
        except Exception as exc:
            return "Unable to check availability right now — please suggest a date and I will confirm."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str, email: str = "") -> str:
        """
        Book an appointment after the lead has verbally confirmed date, time, and service.
        Call ONLY after the lead confirms all details.
        name: lead's full name | phone: with country code | date: YYYY-MM-DD | time: HH:MM | service: type
        email: caller's email address — if provided, sends confirmation email and creates Google Calendar event
        """
        try:
            booking_id = await insert_appointment(name, phone, date, time, service)
        except Exception as exc:
            logger.error("DB appointment insert failed: %s", exc)
            booking_id = "pending"

        result = f"Confirmed! Booking ID: {booking_id}. See you on {date} at {time} for {service}."
        self._booking_completed = True
        self._last_booking_summary = f"appointment booked: {booking_id}"

        # Create Google Calendar event + send confirmation email when email is provided
        if email and self.persona_data:
            google_note = await self._book_google(name, phone, email, service, date, time)
            if google_note:
                result += f" {google_note}"

        return result

    async def _book_google(self, name: str, phone: str, email: str, service: str, date: str, time: str) -> str:
        """Attempt Google Calendar + Gmail booking. Returns status string, never raises."""
        try:
            from inbound_google import is_configured, create_calendar_event, send_confirmation_email
            if not is_configured():
                return ""
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: create_calendar_event(self.persona_data, name, service, date, time, phone, email),
            )
            await loop.run_in_executor(
                None,
                lambda: send_confirmation_email(self.persona_data, name, email, service, date, time),
            )
            return f"Confirmation email sent to {email}."
        except Exception as exc:
            logger.warning("Google booking failed (non-fatal): %s", exc)
            return ""

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """
        End the call and log the outcome. ALWAYS call this before the call ends.
        outcome: 'booked' | 'not_interested' | 'wrong_number' | 'voicemail' | 'no_answer' | 'callback_requested'
        reason: brief description
        """
        duration = int(time.time() - self._call_start_time)
        try:
            await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name, outcome=outcome, reason=reason,
                duration_seconds=duration, recording_url=self.recording_url,
                ended_by="agent",
            )
            self._call_logged = True  # only mark logged after confirmed DB insert
        except Exception as exc:
            logger.error("Failed to log call in end_call(): %s — fallback will retry", exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    @llm.function_tool
    async def hangup(self, reason: str = "") -> str:
        """
        Say goodbye and end the call. Use this whenever the conversation has
        reached a natural ending — the caller got what they needed, they said
        goodbye, they are not interested, or they asked to hang up.
        This is simpler than end_call: you do not need to pick an outcome.
        reason: a short note about how the call ended (optional).
        """
        if self._booking_completed:
            outcome = "booked"
            reason = reason or self._last_booking_summary or "call completed after booking"
        else:
            outcome = "completed"
            reason = reason or "call completed"
        duration = int(time.time() - self._call_start_time)
        try:
            await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name, outcome=outcome, reason=reason,
                duration_seconds=duration, recording_url=self.recording_url,
                ended_by="agent",
            )
            self._call_logged = True
        except Exception as exc:
            logger.error("Failed to log call in hangup(): %s — fallback will retry", exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Transfer the call to a human agent via SIP REFER.
        Call when lead requests a human, is angry, or has a complex issue.
        reason: why you're transferring
        """
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination, play_dialtone=False,
                )
            )
            return "Transferring you to a human agent now. Please hold."
        except Exception as exc:
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """
        Send SMS confirmation after a successful booking. Skips silently if Twilio not configured.
        phone: lead's phone | message: text to send
        """
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception as exc:
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def send_email(self, to_email: str, subject: str, headline: str, message: str) -> str:
        """
        Send a branded confirmation email. Uses the persona's theme colors automatically.
        to_email: recipient email | subject: email subject line
        headline: big text at the top (e.g. "Appointment Confirmed!")
        message: the main body text
        """
        if not self.persona_data:
            return "Email skipped: no persona data available."
        try:
            from inbound_google import is_configured, send_generic_email
            if not is_configured():
                return "Email skipped: Google OAuth not configured."
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: send_generic_email(
                    persona_data=self.persona_data,
                    to_email=to_email,
                    subject=subject,
                    headline=headline,
                    greeting=message,
                    details=[],
                    note="",
                    closing=f"Thank you! — {self.persona_data.get('name', 'Our Office')}",
                ),
            )
            return f"Email sent to {to_email}."
        except Exception as exc:
            logger.warning("Email send failed: %s", exc)
            return "Email delivery failed, but the appointment is confirmed."

    @llm.function_tool
    async def create_google_calendar_event(self, name: str, email: str, date: str, time: str, service: str, phone: str = "") -> str:
        """
        Create a Google Calendar event for an appointment. Call after book_appointment succeeds.
        name: person's name | email: their email | date: YYYY-MM-DD | time: HH:MM | service: service type
        phone: optional phone number
        """
        if not self.persona_data:
            return "Calendar skipped: no persona data available."
        try:
            from inbound_google import is_configured, create_calendar_event
            if not is_configured():
                return "Calendar skipped: Google OAuth not configured. Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN."
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: create_calendar_event(self.persona_data, name, service, date, time, phone, email),
            )
            return f"Google Calendar event created for {name} on {date} at {time}."
        except Exception as exc:
            logger.warning("Google Calendar creation failed: %s", exc)
            return "Calendar event creation failed, but the appointment is booked."

    @llm.function_tool
    async def http_request(self, tool_name: str, params: str = "{}") -> str:
        """
        Call a user-configured HTTP integration tool (URL/API endpoint) by name.
        tool_name: the name of the configured HTTP tool (e.g. "lookup_customer", "book_slot")
        params: JSON object of field values to substitute into the tool's URL/body (optional)
        Only call tools listed in your instructions. Returns the API response or an error.
        """
        tool = next((t for t in self.http_tools if t.get("name") == tool_name), None)
        if not tool:
            available = ", ".join(t.get("name", "?") for t in self.http_tools) or "none"
            return f"HTTP tool '{tool_name}' not found. Available: {available}"
        import json as _json
        try:
            params_dict = _json.loads(params or "{}") if isinstance(params, str) else (params or {})
        except Exception:
            params_dict = {}
        url = tool.get("url", "")
        method = (tool.get("method") or "GET").upper()
        headers_raw = tool.get("headers_json") or "{}"
        try:
            headers = _json.loads(headers_raw) if isinstance(headers_raw, str) else headers_raw
        except Exception:
            headers = {}
        body_template = tool.get("body_template") or ""
        timeout = int(tool.get("timeout") or 10)

        # Substitute {{field}} placeholders in URL and body with param values
        url = _substitute(url, params_dict)
        body_text = _substitute(body_template, params_dict) if body_template else None

        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method in ("POST", "PUT", "PATCH"):
                    resp = await client.request(method, url, headers=headers,
                                                json=_json.loads(body_text) if body_text else None)
                else:
                    resp = await client.request(method, url, headers=headers, params=params_dict)
                if resp.status_code >= 400:
                    return f"{tool_name} returned HTTP {resp.status_code}: {resp.text[:300]}"
                return resp.text[:800]
        except Exception as exc:
            return f"{tool_name} failed: {exc}"

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """
        Look up a contact's full history. Call at the START of every call before engaging.
        phone: the lead's phone number with country code
        Returns call history, appointments, and remembered details.
        """
        try:
            calls = await get_calls_by_phone(phone)
            appointments = await get_appointments_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not appointments and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"Contact history for {phone}:"]
            if memories:
                lines.append(f"\nREMEMBERED ({len(memories)} notes):")
                for m in memories[:10]:
                    lines.append(f"  • {m['insight']}")
            if calls:
                lines.append(f"\nCALL HISTORY ({len(calls)} calls):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  • {ts} — {c.get('outcome','?')}: {c.get('reason','')}")
            if appointments:
                lines.append(f"\nAPPOINTMENTS ({len(appointments)}):")
                for a in appointments[:3]:
                    lines.append(f"  • {a.get('date')} {a.get('time')} — {a.get('service')} [{a.get('status')}]")
            return "\n".join(lines)
        except Exception as exc:
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn something useful: preferences, objections, timing, family info.
        Examples: "Prefers morning calls", "Has 2 kids, interested in family plan", "Callback in 2 weeks"
        insight: the detail to remember
        """
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = f"Compress these notes about a sales contact into 3-5 concise bullets. Keep all key facts.\n\n{bullet_list}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            if response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str = "") -> str:
        """
        Book in Cal.com calendar after book_appointment succeeds.
        name: full name | email: lead's email | date: YYYY-MM-DD | start_time: HH:MM | notes: optional
        """
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — skipping. Add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"eventTypeId": int(event_type_id), "start": start_iso, "timeZone": timezone,
                          "responses": {"name": name, "email": email, "notes": notes},
                          "metadata": {"source": "OutboundAI"}, "language": "en"},
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            uid = data.get("uid", "")
            return f"Cal.com booked. UID: {uid}"
        except Exception as exc:
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        """
        Cancel a Cal.com booking by UID.
        booking_uid: from book_calcom | reason: optional
        """
        api_key = os.getenv("CALCOM_API_KEY", "")
        if not api_key:
            return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"https://api.cal.com/v1/bookings/{booking_uid}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"reason": reason} if reason else {},
                )
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            return f"Cancellation failed: {exc}"
