"""Build a caller persona from a website, or from Google Maps when the site is missing.

Uses Apify. Set APIFY_TOKEN (or APIFY_API_KEY) in the environment.
Prefer a website. If the target is a Maps link, a business name, or the site
comes back empty, fall back to the Google Maps place.
"""

import asyncio
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

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


async def _run_actor(actor: str, payload: dict, timeout_s: int = 110, memory: int = 4096) -> list:
    token = apify_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set. Add it to .env and restart the server.")
    async with httpx.AsyncClient(timeout=40) as client:
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
        items = await client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": token, "clean": "true", "limit": 12},
        )
        items.raise_for_status()
        data = items.json()
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
        chunks.append(f"PAGE: {page or url}\n{body[:3500]}")
        if sum(len(c) for c in chunks) > 14000:
            break
    return title, "\n\n".join(chunks)[:14000]


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
            timeout_s=150,
            memory=4096,
        )
        title, text = _website_text(items)
        if len(text) >= 200:
            return "website", title or urlparse(website).netloc, text
        # Site was empty or blocked. Search Maps with the domain name.
        query = urlparse(website).netloc.replace("www.", "")
    else:
        query = target.strip()

    payload: dict = {"maxCrawledPlacesPerSearch": 1, "language": "en", "maxReviews": 5}
    if _is_maps(target):
        payload["startUrls"] = [{"url": target.strip()}]
    else:
        payload["searchStringsArray"] = [query]
    items = await _run_actor(MAPS_ACTOR, payload, timeout_s=90, memory=4096)
    title, text = _maps_text(items)
    if not text:
        raise RuntimeError("Nothing useful came back from the website or Google Maps.")
    return "gmaps", title or query, text


def _fallback_prompt(title: str, direction: str, source_text: str) -> str:
    role = {
        "inbound": "You answer incoming phone calls for this business.",
        "outbound": "You place outbound phone calls for this business and try to book the next step.",
        "both": "You handle both incoming calls and outbound calls for this business.",
    }[direction if direction in ("inbound", "outbound", "both") else "both"]
    opening = (
        "As soon as the call connects, ask what they need in one short sentence. Do not greet."
        if direction == "inbound"
        else "As soon as the call connects, say why you are calling in one short sentence. Do not greet and do not wait for them to speak first."
    )
    return (
        f"You are the phone agent for {title or 'this business'}.\n"
        f"{role}\n\n"
        f"{opening}\n"
        "Speak in short spoken sentences. Use only the facts below. If you do not know, say so and offer a callback.\n"
        "Call lookup_contact only after your first sentence. Use book_appointment only after they agree to a time.\n"
        "Always call end_call before hanging up.\n\n"
        "KNOWLEDGE:\n"
        f"{source_text[:6000]}\n"
    )


async def _write_prompt(title: str, direction: str, source_text: str) -> str:
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return _fallback_prompt(title, direction, source_text)
    instruction = (
        "Write a complete system prompt for a live phone agent. "
        f"The business is {title or 'the business below'}. Direction: {direction}.\n"
        "Rules:\n"
        "- Use only facts from the source. Never invent prices, hours, addresses, or offers.\n"
        "- Short spoken sentences, one or two per turn. No stage directions. No markdown headings in the spoken lines.\n"
        "- Do not open with hello, hi, or 'how can I help you'.\n"
        "- Inbound: the first sentence asks what they need.\n"
        "- Outbound: the first sentence says why you are calling.\n"
        "- If the source is Indian or Hindi, write the agent's speaking rules in Hinglish. Otherwise match the source language.\n"
        "- Include a KNOWLEDGE section: services, location, hours, phone, and real FAQs from the source.\n"
        "- Tools the agent may use after the first sentence: lookup_contact, check_availability, "
        "book_appointment, send_sms_confirmation, remember_details, transfer_to_human, end_call.\n"
        "- lookup_contact must not run before the first sentence.\n"
        "Return only the prompt.\n\n"
        f"SOURCE:\n{source_text}"
    )
    async with httpx.AsyncClient(timeout=60) as client:
        for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": instruction}]}],
                    "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2500},
                },
            )
            if resp.status_code >= 400:
                continue
            data = resp.json()
            parts = (
                ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            )
            text = "\n".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
    return _fallback_prompt(title, direction, source_text)


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
    prompt = await _write_prompt(title, direction, brief)
    return {
        "name": title,
        "agent_name": _agent_name(name or title),
        "direction": direction,
        "system_prompt": prompt,
        "source": "ai",
        "source_ref": brief[:500],
        "voice": os.getenv("GEMINI_TTS_VOICE", "Sulafat") or "Sulafat",
        "model": "gemini-3.8-live",
        "audio_mode": "gemini",
    }


async def build_from_target(target: str, direction: str = "both") -> dict:
    """Scrape a website or Google Maps listing and return persona fields. Does not save."""
    target = (target or "").strip()
    if not target:
        raise RuntimeError("Paste a website, a Google Maps link, or a business name.")
    source, title, text = await _scrape(target)
    prompt = await _write_prompt(title, direction, text)
    return {
        "name": title or target[:80],
        "agent_name": _agent_name(title),
        "direction": direction if direction in ("inbound", "outbound", "both") else "both",
        "system_prompt": prompt,
        "source": source,
        "source_ref": target,
        "voice": os.getenv("GEMINI_TTS_VOICE", "Sulafat") or "Sulafat",
        "model": "gemini-3.8-live",
        "audio_mode": "gemini",
    }
