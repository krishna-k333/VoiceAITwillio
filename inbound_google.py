"""
google_services.py — Google Calendar + Gmail with persona-branded email templates.

Works for both inbound and outbound calls. Each persona gets a permanently
assigned color theme (10 pre-defined palettes). The email template is a single
HTML skeleton that swaps CSS variables — no per-persona template duplication.

Required env vars:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
Optional:
  CLINIC_CALENDAR_ID  (default: "primary")
  CLINIC_EMAIL        (Gmail sender address)
  CLINIC_PHONE        (shown in email template)
"""

import base64
import hashlib
import logging
import os
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("google-services")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10 PRE-DEFINED COLOR THEMES — assigned permanently to each persona at creation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COLOR_THEMES = [
    {"name": "Ocean Blue",      "primary": "#1a73e8", "secondary": "#e8f0fe", "accent": "#174ea6", "header_bg": "linear-gradient(135deg, #1a73e8, #4285f4)"},
    {"name": "Forest Green",    "primary": "#0d7c3f", "secondary": "#e6f4ea", "accent": "#137333", "header_bg": "linear-gradient(135deg, #0d7c3f, #34a853)"},
    {"name": "Royal Purple",    "primary": "#7c3aed", "secondary": "#ede9fe", "accent": "#5b21b6", "header_bg": "linear-gradient(135deg, #7c3aed, #a78bfa)"},
    {"name": "Sunset Orange",   "primary": "#ea580c", "secondary": "#fff7ed", "accent": "#c2410c", "header_bg": "linear-gradient(135deg, #ea580c, #fb923c)"},
    {"name": "Ruby Red",        "primary": "#dc2626", "secondary": "#fef2f2", "accent": "#991b1b", "header_bg": "linear-gradient(135deg, #dc2626, #f87171)"},
    {"name": "Teal",            "primary": "#0d9488", "secondary": "#f0fdfa", "accent": "#0f766e", "header_bg": "linear-gradient(135deg, #0d9488, #2dd4bf)"},
    {"name": "Rose Pink",       "primary": "#be185d", "secondary": "#fdf2f8", "accent": "#9d174d", "header_bg": "linear-gradient(135deg, #be185d, #f472b6)"},
    {"name": "Charcoal Gold",   "primary": "#1a1a2e", "secondary": "#f0ead6", "accent": "#e8c96a", "header_bg": "linear-gradient(135deg, #1a1a2e, #16213e)"},
    {"name": "Sky Cyan",        "primary": "#0891b2", "secondary": "#ecfeff", "accent": "#0e7490", "header_bg": "linear-gradient(135deg, #0891b2, #22d3ee)"},
    {"name": "Warm Brown",      "primary": "#92400e", "secondary": "#fffbeb", "accent": "#78350f", "header_bg": "linear-gradient(135deg, #92400e, #d97706)"},
]


def assign_theme(persona_name: str) -> dict:
    """Deterministically pick a theme for a persona name. Same name always gets the same theme."""
    idx = int(hashlib.md5(persona_name.encode()).hexdigest(), 16) % len(COLOR_THEMES)
    return COLOR_THEMES[idx]


def get_theme(persona_data: dict) -> dict:
    """Get the theme for a persona. Falls back to deterministic assignment."""
    stored = persona_data.get("email_theme")
    if stored and isinstance(stored, dict) and stored.get("primary"):
        return stored
    return assign_theme(persona_data.get("name", "default"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SINGLE HTML EMAIL TEMPLATE — color-swapped via CSS variables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:'Segoe UI',Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:32px 16px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:580px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">

  <!-- HEADER -->
  <tr>
    <td style="background:{header_bg};padding:32px 28px;text-align:center">
      <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-.3px">{business_name}</h1>
      <p style="margin:6px 0 0;color:rgba(255,255,255,.8);font-size:13px">{subtitle}</p>
    </td>
  </tr>

  <!-- BODY -->
  <tr>
    <td style="padding:32px 28px">
      <h2 style="margin:0 0 12px;color:{primary};font-size:18px;font-weight:700">{headline}</h2>
      <p style="margin:0 0 20px;color:#374151;font-size:14px;line-height:1.6">
        {greeting}
      </p>

      <!-- DETAILS TABLE -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{secondary};border-radius:10px;border-collapse:collapse;margin:0 0 20px">
        {details_rows}
      </table>

      <!-- NOTE BOX -->
      <div style="background:{secondary};border-left:4px solid {primary};border-radius:6px;padding:14px 16px;margin:0 0 24px">
        <p style="margin:0;color:#374151;font-size:13px;line-height:1.5">{note}</p>
      </div>

      <p style="margin:0;color:#6b7280;font-size:13px;line-height:1.5">{closing}</p>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="background:#f9fafb;padding:20px 28px;border-top:1px solid #e5e7eb;text-align:center">
      <p style="margin:0;color:#9ca3af;font-size:11px">
        {footer_text}
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def _detail_row(label: str, value: str, secondary: str, stripe: bool) -> str:
    bg = f'background:{secondary}' if stripe else ''
    return (
        f'<tr style="{bg}">'
        f'<td style="padding:10px 14px;font-weight:600;color:#374151;font-size:13px;width:35%;border-bottom:1px solid #e5e7eb">{label}</td>'
        f'<td style="padding:10px 14px;color:#1f2937;font-size:13px;border-bottom:1px solid #e5e7eb">{value}</td>'
        f'</tr>'
    )


def render_email_html(
    persona_data: dict,
    headline: str,
    greeting: str,
    details: list,
    note: str,
    closing: str,
) -> str:
    """
    Render the branded email template.

    persona_data: persona dict with name, email_theme, etc.
    headline: e.g. "Appointment Confirmed!" or "Callback Scheduled!"
    greeting: e.g. "Dear <strong>Rahul</strong>, your appointment is confirmed."
    details: list of (label, value) tuples for the details table
    note: text in the left-bordered note box
    closing: closing message
    """
    theme = get_theme(persona_data)
    business = persona_data.get("name", "Our Office")

    detail_rows = ""
    for i, (label, value) in enumerate(details):
        detail_rows += _detail_row(label, value, theme["secondary"], i % 2 == 0)

    subtitle = persona_data.get("email_subtitle", "Appointment Confirmation")
    footer = persona_data.get("email_footer", f"Powered by {business} AI Assistant")

    return _EMAIL_TEMPLATE.format(
        primary=theme["primary"],
        secondary=theme["secondary"],
        accent=theme["accent"],
        header_bg=theme["header_bg"],
        business_name=business,
        subtitle=subtitle,
        headline=headline,
        greeting=greeting,
        details_rows=detail_rows,
        note=note,
        closing=closing,
        footer_text=footer,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GOOGLE AUTH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_configured() -> bool:
    """Return True if all required Google OAuth env vars are present."""
    return all([
        os.getenv("GOOGLE_REFRESH_TOKEN"),
        os.getenv("GOOGLE_CLIENT_ID"),
        os.getenv("GOOGLE_CLIENT_SECRET"),
    ])


def _get_creds():
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CALENDAR — works for both inbound and outbound
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_calendar_event(
    persona_data: dict,
    person_name: str,
    service: str,
    date_str: str,
    time_str: str,
    phone: str,
    email: str,
) -> dict:
    """Create a 1-hour appointment in Google Calendar using the persona's branding."""
    from googleapiclient.discovery import build
    creds = _get_creds()
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

    calendar_id = os.getenv("CLINIC_CALENDAR_ID", "primary")
    tz = os.getenv("CLINIC_TIMEZONE", "Asia/Kolkata")

    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=1)

    business = persona_data.get("name", "Our Office")
    theme = get_theme(persona_data)

    event = {
        "summary": f"{theme['name']} {service} — {person_name} | {business}",
        "description": (
            f"Person: {person_name}\n"
            f"Phone: {phone}\n"
            f"Email: {email}\n"
            f"Service: {service}\n\n"
            f"Booked via {business} AI Assistant"
        ),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 30},
            ],
        },
    }

    created = cal.events().insert(calendarId=calendar_id, body=event).execute()
    logger.info("Calendar event created: %s", created.get("htmlLink"))
    return created


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EMAIL — works for both inbound and outbound
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_confirmation_email(
    persona_data: dict,
    person_name: str,
    person_email: str,
    service: str,
    date_str: str,
    time_str: str,
) -> bool:
    """Send branded HTML confirmation email using the persona's theme."""
    from googleapiclient.discovery import build
    creds = _get_creds()
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    sender = os.getenv("CLINIC_EMAIL", "")
    clinic_phone = os.getenv("CLINIC_PHONE", "")
    business = persona_data.get("name", "Our Office")

    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        display_time = dt.strftime("%d %B %Y, %I:%M %p")
    except ValueError:
        display_time = f"{date_str} at {time_str}"

    theme = get_theme(persona_data)

    html = render_email_html(
        persona_data=persona_data,
        headline="Appointment Confirmed! ✅",
        greeting=(
            f"Dear <strong>{person_name}</strong>,<br><br>"
            f"Your appointment has been confirmed. Here are the details:"
        ),
        details=[
            ("Service", service),
            ("Date & Time", display_time),
            ("Location", business),
            ("Contact", clinic_phone or sender or "See confirmation"),
        ],
        note=f"⚠️ Please arrive 10 minutes before your appointment. To reschedule, please call us.",
        closing=f"Thank you for choosing {business}! We look forward to seeing you.",
    )

    subject = f"✅ Appointment Confirmed — {service} | {business}"
    sender_name = persona_data.get("name", business)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>" if sender else sender_name
    msg["To"] = person_email
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info("Confirmation email sent to %s (theme: %s)", person_email, theme["name"])
    return True


def send_generic_email(
    persona_data: dict,
    to_email: str,
    subject: str,
    headline: str,
    greeting: str,
    details: list,
    note: str,
    closing: str,
) -> bool:
    """Send a branded email with custom content. Works for any purpose."""
    from googleapiclient.discovery import build
    creds = _get_creds()
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    sender = os.getenv("CLINIC_EMAIL", "")
    business = persona_data.get("name", "Our Office")
    theme = get_theme(persona_data)

    html = render_email_html(
        persona_data=persona_data,
        headline=headline,
        greeting=greeting,
        details=details,
        note=note,
        closing=closing,
    )

    sender_name = persona_data.get("name", business)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>" if sender else sender_name
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info("Email sent to %s (theme: %s)", to_email, theme["name"])
    return True