"""Build a caller persona from a website, or from Google Maps when the site is missing.

Uses Apify. Set APIFY_TOKEN (or APIFY_API_KEY) in the environment.
Prefer a website. If the target is a Maps link, a business name, or the site
comes back empty, fall back to the Google Maps place.
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("persona-builder")

WEBSITE_ACTOR = "apify~website-content-crawler"
MAPS_ACTOR = "compass~crawler-google-places"


def apify_token() -> str:
    return os.getenv("APIFY_TOKEN", "").strip() or os.getenv("APIFY_API_KEY", "").strip()


def _is_maps(target: str) -> bool:
    low = target.lower()
    return any(part in low for part in ("google.com/maps", "maps.app.goo.gl", "goo.gl/maps", "g.page/"))


def _as_website(target: str) -> Optional[str]:
    text = target.strip()
    if _is_maps(text) or " " in text:
        return None
    if not text.startswith(("http://", "https://")):
        if "." not in text:
            return None
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return text


async def _run_actor(actor: str, payload: dict, timeout_s: int = 150, memory: int = 4096) -> list:
    token = apify_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set. Add it to .env and restart the server.")
    async with httpx.AsyncClient(timeout=60) as client:
        started = await client.post(
            f"https://api.apify.com/v2/acts/{actor}/runs",
            params={"token": token, "memory": memory, "timeout": timeout_s},
            json=payload,
        )
        if started.status_code >= 400:
            detail = started.text[:400]
            raise RuntimeError(f"Apify rejected {actor}: {started.status_code} {detail}")
        run = started.json()["data"]
        run_id = run["id"]
        dataset_id = run.get("defaultDatasetId")
        status = run.get("status")
        logger.info("Apify %s started — run_id=%s", actor, run_id)
        # waitForFinish on Apify caps at 60s, so poll instead.
        deadline = time.time() + timeout_s
        while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT") and time.time() < deadline:
            await asyncio.sleep(3)
            check = await client.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}",
                params={"token": token},
            )
            check.raise_for_status()
            body = check.json()["data"]
            status = body.get("status")
            dataset_id = body.get("defaultDatasetId") or dataset_id
        if status != "SUCCEEDED":
            raise RuntimeError(f"Apify {actor} finished as {status or 'timeout'}")
        if not dataset_id:
            raise RuntimeError(f"Apify {actor} succeeded but returned no dataset ID — cannot fetch results")
        items = await client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": token, "clean": "true", "limit": 12},
        )
        items.raise_for_status()
        data = items.json()
        logger.info("Apify %s returned %d items", actor, len(data) if isinstance(data, list) else 0)
        return data if isinstance(data, list) else []


def _website_text(items: list) -> tuple[str, str]:
    chunks = []
    title = ""
    for item in items:
        page = (item.get("metadata") or {}).get("title") or item.get("title") or ""
        if page and not title:
            title = page.split("|")[0].strip()
        body = (item.get("markdown") or item.get("text") or "").strip()
        if len(body) < 40:
            continue
        url = item.get("url") or ""
        chunks.append(f"PAGE: {page or url}\n{body[:5000]}")
        if sum(len(c) for c in chunks) > 18000:
            break
    return title, "\n\n".join(chunks)[:18000]


def _maps_text(items: list) -> tuple[str, str]:
    if not items:
        return "", ""
    place = items[0]
    title = (place.get("title") or place.get("name") or "").strip()
    lines = [f"Name: {title}"]
    for label, key in (
        ("Category", "categoryName"),
        ("Address", "address"),
        ("Phone", "phone"),
        ("Website", "website"),
        ("Rating", "totalScore"),
        ("Reviews", "reviewsCount"),
    ):
        value = place.get(key)
        if value:
            lines.append(f"{label}: {value}")
    hours = place.get("openingHours") or place.get("openingHoursText")
    if isinstance(hours, list):
        lines.append("Hours: " + "; ".join(str(h) for h in hours[:7]))
    elif hours:
        lines.append(f"Hours: {hours}")
    reviews = place.get("reviews") or []
    snippets = []
    for review in reviews[:5]:
        text = (review.get("text") or "").strip()
        if text:
            snippets.append(text[:400])
    if snippets:
        lines.append("What customers say:\n- " + "\n- ".join(snippets))
    return title, "\n".join(lines)[:14000]


async def _scrape(target: str) -> tuple[str, str, str]:
    """Return (source, title, text). source is 'website' or 'gmaps'."""
    website = _as_website(target)
    if website and not _is_maps(target):
        logger.info("Scraping website: %s", website)
        items = await _run_actor(
            WEBSITE_ACTOR,
            {
                "startUrls": [{"url": website}],
                "maxCrawlPages": 6,
                "maxCrawlDepth": 1,
                "crawlerType": "playwright:adaptive",
                "saveMarkdown": True,
                "useSitemaps": False,
                "maxConcurrency": 2,
                "proxyConfiguration": {"useApifyProxy": True},
            },
            timeout_s=180,
            memory=4096,
        )
        title, text = _website_text(items)
        logger.info("Website scrape returned %d chars (title=%s)", len(text), title)
        if len(text) >= 200:
            return "website", title or urlparse(website).netloc, text
        logger.info("Website content too short (%d chars) — falling back to Google Maps", len(text))
        query = urlparse(website).netloc.replace("www.", "")
    else:
        query = target.strip()

    logger.info("Scraping Google Maps: %s", query)
    payload: dict = {"maxCrawledPlacesPerSearch": 1, "language": "en", "maxReviews": 5}
    if _is_maps(target):
        payload["startUrls"] = [{"url": target.strip()}]
    else:
        payload["searchStringsArray"] = [query]
    items = await _run_actor(MAPS_ACTOR, payload, timeout_s=120, memory=4096)
    title, text = _maps_text(items)
    logger.info("Google Maps scrape returned %d chars (title=%s)", len(text), title)
    if not text:
        raise RuntimeError(f"Nothing useful came back from the website or Google Maps for '{target}'. "
                           "Check that the URL is accessible and the business is listed on Google Maps.")
    return "gmaps", title or query, text


def _spoken_lines(text: str) -> int:
    straight = re.findall(r'"[^"\n]{18,}"', text or "")
    curly = re.findall(r"“[^”\n]{18,}”", text or "")
    return len(straight) + len(curly)


def _prompt_is_thin(text: str) -> bool:
    """A usable phone prompt has a real script, not a short summary."""
    body = (text or "").strip()
    if len(body) < 800:
        return True
    if "end_call" not in body and "end the call" not in body.lower():
        return True
    return _spoken_lines(body) < 3


def _fallback_prompt(title: str, direction: str, source_text: str) -> str:
    business = title or "this business"
    side = direction if direction in ("inbound", "outbound", "both") else "both"
    knowledge = (source_text or "").strip()[:7000] or "No extra facts were provided. Do not invent prices, hours, or offers."
    outbound_open = (
        f'"Ji, main {business} se bol raha hoon. Aapke liye ek short update tha — do minute milenge?"'
    )
    inbound_open = (
        f'"Ji, {business} se bol raha hoon. Bataiye, kya kaam hai?"'
    )
    if side == "inbound":
        first = (
            "THIS CALL IS INBOUND. They called you and are waiting. "
            f"The first sentence you speak, out loud, is exactly: {inbound_open} "
            "Do not stay silent. Do not start with hi, hello, or good morning."
        )
    elif side == "outbound":
        first = (
            "THIS CALL IS OUTBOUND. You placed the call. Do not greet and do not wait for them to speak. "
            f"The first sentence you speak is exactly: {outbound_open}"
        )
    else:
        first = (
            "If you placed the call, do not wait. The first sentence is exactly: "
            f"{outbound_open}\n"
            "If they called you, the first sentence is exactly: "
            f"{inbound_open}"
        )
    return f"""You are the phone agent for {business}. You are not a narrator and you do not read these headings aloud.
{first}
Never start with hi, hello, good morning, or "am I speaking with…". One short spoken sentence, then stop and listen.

GOAL
Get the caller to the right next step for {business}: answer their question from the knowledge below, and book a time only after they agree. If you do not know a fact, say so. Never invent a price, offer, address, hour, or doctor name.

LANGUAGE
Speak the way this business's customers speak. If the knowledge is Hindi or the business is in India, speak natural Hinglish: short sentences, "ji" where it fits, English only for names and numbers. If they switch to English, switch with them. No "Certainly", "Of course", or "Absolutely".

WHAT YOU SAY — follow this order. Each quoted line is something you may actually speak. One line, then wait.

STEP 1 — FIRST LINE
Outbound, you called them: {outbound_open}
Inbound, they called you: {inbound_open}
After that line, call lookup_contact once. Never call a tool before this first sentence.

STEP 2 — WHY THIS CALL, OR WHAT THEY NEED
Outbound: "Main isliye call kar raha hoon kyunki {business} ke baare mein ek cheez aapke kaam ki hai. Main seedha bata deta hoon."
Then say the single most relevant fact from KNOWLEDGE, in one sentence. Do not list the whole website.
Inbound: listen. Then repeat their need in one short line: "Samajh gaya — aapko [their need] chahiye."

STEP 3 — ONE USEFUL FACT, THEN A QUESTION
Pick one real service, offer, or detail from KNOWLEDGE. Say it in one sentence, then ask one question.
"Isme sabse useful cheez yeh hai — [one fact from KNOWLEDGE]. Aap yeh apne liye dekh rahe hain, ya kisi aur ke liye?"
Use their answer. Do not recite three benefits in one turn.

STEP 4 — THE NEXT STEP
Only after they show some interest, offer a time.
"Aapko jo chahiye, woh phone pe poora clear nahi hota. Main ek time rakh deta hoon — kaun sa din aur time theek rahega?"
If they name a day and time, call check_availability before you confirm.
If that slot is taken: "Woh time fill ho gaya. [other time] chalega?"
If they are unsure: "Koi baat nahi. Ek tentative time rakh deta hoon, baad mein badal sakte hain. Kaun sa din loose hai?"

STEP 5 — BOOK, THEN CLOSE
Only after they clearly agree:
1. book_appointment with their name, phone, date, time, and the service they asked for.
2. send_sms_confirmation with the date, time, and {business}.
3. Say: "Ho gaya. [date] ko [time] pe aap set hain. Koi aur sawaal ho toh abhi bata dijiye."
4. remember_details with anything useful they said (budget, who decides, what they want).
5. hangup with reason "booking confirmed".

OBJECTIONS — say the line, do not argue
"Abhi busy hoon" → "Bilkul. Sirf ek line: [one fact from KNOWLEDGE]. Baaki aap decide kariye — baad mein call karun, ya ek time rakh dun?"
"Interest nahi hai" → "Koi baat nahi ji. Agar baad mein chahiye ho toh {business} yahin hai. Aapka din achha rahe." Then hangup with reason "not interested".
"WhatsApp pe bhejo" → "Bhej deta hoon. Saath mein ek tentative time bhi rakhun, taaki slot chala na jaaye?"
"Number kahan se mila?" → "Aap {business} ke enquiry list mein the, isliye call kiya. Timing kharab ho toh maaf kijiyega."
"Baar baar call mat karo" → "Note kar liya. Dobara call nahi aayega. Maaf kijiyega." Then remember_details "Do not call again" and hangup with reason "requested removal".
"Insaan se baat karni hai" → transfer_to_human and tell them you are connecting them.
"Bot ho kya?" → "Main {business} ka phone assistant hoon. Sawal ka jawab de sakta hoon, aur time bhi rakh sakta hoon. Kya chahiye?"
"Baad mein call karo" → "Theek hai. Kaun sa time likh lun?" Then remember_details with that time and hangup with reason "callback requested".
Wrong person → "Sorry, galat number lag gaya. Disturb kiya." Then end_call with outcome wrong_number.
Voicemail → "{business} se call tha, ek short update ke liye. Jab time ho, call back kar lijiyega." Then end_call with outcome voicemail.
Silence for several seconds → end_call with outcome no_answer. Do not fill silence with extra talk.

STYLE
One or two short sentences, then stop. No speeches. No greeting at the start. Do not say you are an AI unless they ask. Match their language. If they say wait, wait.

TOOLS
lookup_contact — once, only after the first sentence.
check_availability — before you agree to any time.
book_appointment — only after a clear yes. Auto-creates Google Calendar event + sends branded email when email is provided.
send_sms_confirmation — right after a booking.
send_email — branded confirmation email. Use after booking when email is available.
create_google_calendar_event — Google Calendar event. Use after book_appointment succeeds.
remember_details — budget, timeline, who decides, objections, callback time.
transfer_to_human — when they ask for a person or the problem is urgent.
hangup — whenever the call has naturally ended. No outcome needed, it auto-detects.
end_call — only when you need a specific outcome (wrong_number, voicemail, no_answer).

The only placeholders you may see filled in later are {{lead_name}} and {{lead_phone}}. Say the person's name only after you have it. Do not read a placeholder aloud.

KNOWLEDGE — these are the only facts you may use. If it is not here, you do not know it.
{knowledge}
"""


def _script_instruction(title: str, direction: str, source_text: str, previous: str = "") -> str:
    business = title or "the business in the source"
    side = direction if direction in ("inbound", "outbound", "both") else "both"
    rewrite = ""
    if previous:
        rewrite = (
            "\nThe draft below is too short to run a live call. Rewrite it as a full script. "
            "Keep its real facts. Add the missing spoken lines.\n\nDRAFT:\n"
            f"{previous[:4000]}\n"
        )
    return f"""Write the system prompt a live phone agent will follow for {business}.
Direction: {side}.
Return only the prompt. No preface.
{rewrite}
This is the script the agent speaks from, not a summary of the website. A short paragraph is a failure.
Length: at least 600 words. Include at least 6 lines the agent can say out loud, each inside double quotes.

The agent must know, without guessing:
- the exact first sentence, in quotes
- why it is calling, or how it handles someone who called in
- the services, location, hours, prices, and offers that are actually in the source
- one question it asks after each fact
- the line it uses to offer a time
- a spoken reply for each objection: busy, not interested, send it on WhatsApp, where did you get my number, stop calling, I want a human, are you a bot, call me later, wrong person, voicemail
- the exact order of tools when booking

Rules:
- Use only facts from the source. If a price, hour, address, or offer is not in the source, do not invent one. Tell the agent to say it does not know.
- Spoken turns are one or two short sentences. Put those lines in double quotes. Do not put stage directions inside the quotes.
- Do not open with hi, hello, good morning, or "am I speaking with".
- Outbound: the agent placed the call and speaks first, saying why it is calling.
- Inbound: the caller is already waiting. The first quoted sentence says who is answering and asks what they need. Do not start with hi, hello, or good morning, and do not stay silent.
- If direction is both, write both openings and label them OUTBOUND FIRST LINE and INBOUND FIRST LINE.
- Indian or Hindi source: the quoted lines are natural Hinglish. Any other source: the quoted lines are in that language. Headings stay in English.
- Section headings the agent does not read aloud: WHO YOU ARE, FIRST LINE, CALL FLOW, OBJECTIONS, BOOKING, STYLE, TOOLS, KNOWLEDGE.
- KNOWLEDGE must copy the real services, area, hours, phone, prices, and common questions from the source, in enough detail that the agent can answer. Do not compress KNOWLEDGE into one sentence.
- Tools, and only after the first sentence: lookup_contact, check_availability, book_appointment, send_sms_confirmation, send_email, create_google_calendar_event, remember_details, transfer_to_human, hangup, end_call.
- lookup_contact must not run before the first sentence. hangup or end_call must run before every hangup. book_appointment only after a clear yes, and only after check_availability.
- Use hangup for natural call endings (goodbye, done talking, not interested). Use end_call only when a specific outcome must be logged (wrong_number, voicemail, no_answer).
- send_email sends a branded confirmation email. create_google_calendar_event creates a calendar event. Both work after book_appointment when Google OAuth is configured.
- The only tokens you may leave for the dialer to fill are {{lead_name}} and {{lead_phone}}. Write the business name in plain text.

SOURCE:
{source_text}
"""


def _candidate_text(data: dict) -> tuple[str, str]:
    candidate = ((data.get("candidates") or [{}])[0]) or {}
    parts = ((candidate.get("content") or {}).get("parts") or [])
    chunks = []
    for part in parts:
        if part.get("thought"):
            continue
        chunks.append(part.get("text") or "")
    return "\n".join(chunks).strip(), str(candidate.get("finishReason") or "")


async def _generate_script(instruction: str) -> str:
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        logger.error("GOOGLE_API_KEY is not set — cannot generate persona script")
        return ""
    best = ""
    async with httpx.AsyncClient(timeout=120) as client:
        for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
            config: dict = {"temperature": 0.5, "maxOutputTokens": 16384}
            # 2.5 Flash spends the output budget on hidden thinking unless this is 0,
            # which was cutting the spoken script down to a short summary.
            if model.startswith("gemini-2.5"):
                config["thinkingConfig"] = {"thinkingBudget": 0}
            try:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": api_key},
                    json={
                        "contents": [{"parts": [{"text": instruction}]}],
                        "generationConfig": config,
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning("Gemini %s request failed: %s", model, exc)
                continue
            if resp.status_code >= 400:
                logger.warning("Gemini %s returned HTTP %d: %s", model, resp.status_code, resp.text[:300])
                continue
            text, finish = _candidate_text(resp.json())
            logger.info("Gemini %s returned %d chars (finish=%s)", model, len(text), finish)
            if len(text) > len(best):
                best = text
            if text and not _prompt_is_thin(text) and finish != "MAX_TOKENS":
                return text
    if not best:
        logger.warning("Both Gemini models returned empty responses")
    else:
        logger.warning("Best Gemini response was %d chars but failed thin check", len(best))
    return best


async def _write_prompt(title: str, direction: str, source_text: str) -> str:
    instruction = _script_instruction(title, direction, source_text)
    logger.info("Generating persona prompt for %s (direction=%s, source_chars=%d)", title, direction, len(source_text))
    draft = await _generate_script(instruction)
    if draft and not _prompt_is_thin(draft):
        logger.info("Prompt generated on first attempt (%d chars)", len(draft))
        return draft
    if draft:
        logger.info("First draft was thin (%d chars) — retrying with draft context", len(draft))
        expanded = await _generate_script(_script_instruction(title, direction, source_text, draft))
        if expanded and not _prompt_is_thin(expanded):
            logger.info("Expanded prompt generated (%d chars)", len(expanded))
            return expanded
        if len(expanded) > len(draft):
            draft = expanded
    fallback = _fallback_prompt(title, direction, source_text)
    logger.info("Using fallback prompt (%d chars) — best Gemini response was %d chars", len(fallback), len(draft))
    return fallback if _prompt_is_thin(draft) else (draft or fallback)


def _agent_name(title: str) -> str:
    clean = re.sub(r"\s+", " ", title or "").strip()
    if not clean:
        return "Alex"
    return clean.split(" ")[0][:24]


async def build_from_brief(brief: str, direction: str = "both", name: str = "") -> dict:
    """Write a persona from a short description. Does not scrape and does not save."""
    brief = (brief or "").strip()
    if not brief:
        raise RuntimeError("Describe the agent you want.")
    direction = direction if direction in ("inbound", "outbound", "both") else "both"
    title = (name or "").strip() or re.sub(r"\s+", " ", brief).strip()[:80]
    logger.info("Building persona from brief: %s (direction=%s)", title[:80], direction)
    prompt = await _write_prompt(title, direction, brief)
    logger.info("Persona built from brief — name=%s prompt=%d chars", title, len(prompt))
    return {
        "name": title,
        "agent_name": _agent_name(name or title),
        "direction": direction,
        "system_prompt": prompt,
        "source": "ai",
        "source_ref": brief[:500],
        "voice": os.getenv("GEMINI_TTS_VOICE", "Sulafat") or "Sulafat",
        "model": "gemini-3.1-flash-live-preview",
        "audio_mode": "gemini",
    }


async def build_from_target(target: str, direction: str = "both") -> dict:
    """Scrape a website or Google Maps listing and return persona fields. Does not save."""
    target = (target or "").strip()
    if not target:
        raise RuntimeError("Paste a website, a Google Maps link, or a business name.")
    logger.info("Building persona from target: %s (direction=%s)", target[:80], direction)
    source, title, text = await _scrape(target)
    prompt = await _write_prompt(title, direction, text)
    logger.info("Persona built from %s — name=%s prompt=%d chars", source, title, len(prompt))
    return {
        "name": title or target[:80],
        "agent_name": _agent_name(title),
        "direction": direction if direction in ("inbound", "outbound", "both") else "both",
        "system_prompt": prompt,
        "source": source,
        "source_ref": target,
        "voice": os.getenv("GEMINI_TTS_VOICE", "Sulafat") or "Sulafat",
        "model": "gemini-3.1-flash-live-preview",
        "audio_mode": "gemini",
    }
