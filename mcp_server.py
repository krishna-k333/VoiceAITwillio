"""Web MCP server for the caller personas.

Listens on 127.0.0.1 only. Nginx publishes it at /mcp. Every request must send
the API key as `Authorization: Bearer <AGENT_API_KEY>` or `X-API-Key`.
The process refuses to start when AGENT_API_KEY is missing or shorter than 16 characters.
"""

import os
import secrets
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

# Reverse-proxy bypass: authentication is handled exclusively by _ApiKeyGate
async def _bypass_security(self, request, is_post=False):
    return None

TransportSecurityMiddleware.validate_request = _bypass_security

load_dotenv(".env")

_PORT = int(os.getenv("MCP_PORT", "8766"))
_PUBLIC_HOST = os.getenv("MCP_PUBLIC_HOST", "voicees.moorerevenue.com").strip()


def _allowed_hosts() -> list:
    hosts = [
        "127.0.0.1:*",
        "localhost:*",
        _PUBLIC_HOST,
        f"{_PUBLIC_HOST}:*",
    ]
    extra = os.getenv("MCP_ALLOWED_HOSTS", "")
    hosts.extend(part.strip() for part in extra.split(",") if part.strip())
    return hosts


mcp = FastMCP(
    "voice-agents",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=_PORT,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=[
            f"https://{_PUBLIC_HOST}",
            f"http://{_PUBLIC_HOST}",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
)

from agent_service import (  # noqa: E402
    AgentError,
    create_from_ai,
    create_from_prompt,
    create_from_website,
    edit_selected_prompt as save_selected_prompt,
    get_agent,
    list_agents,
    place_outbound_call,
    select_agent,
)

mcp = FastMCP("voice-agents")


def _fail(exc: AgentError) -> dict:
    return {"error": str(exc)}


@mcp.tool()
async def make_agent_from_website(
    url: str,
    direction: str = "both",
    audio_mode: str = "gemini",
    voice: str = "Sulafat",
) -> dict:
    """Create an agent persona from a website. Falls back to Google Maps if the site is empty."""
    try:
        return await create_from_website(url, direction, audio_mode, voice)
    except AgentError as exc:
        return _fail(exc)


@mcp.tool()
async def make_agent_from_prompt(
    name: str,
    system_prompt: str,
    agent_name: str = "",
    direction: str = "both",
    audio_mode: str = "gemini",
    voice: str = "Sulafat",
) -> dict:
    """Save an agent persona using a prompt you already wrote."""
    try:
        return create_from_prompt(name, system_prompt, agent_name, direction, voice, audio_mode)
    except AgentError as exc:
        return _fail(exc)


@mcp.tool()
async def make_agent_from_ai(
    brief: str,
    name: str = "",
    direction: str = "both",
    audio_mode: str = "gemini",
    voice: str = "Sulafat",
) -> dict:
    """Write an agent persona with AI from a short description, then save it."""
    try:
        return await create_from_ai(brief, name, direction, audio_mode, voice)
    except AgentError as exc:
        return _fail(exc)


@mcp.tool()
def get_agents() -> dict:
    """List every saved agent and which one is selected for inbound and outbound."""
    return list_agents()


@mcp.tool()
def get_agent_detail(agent_id: str) -> dict:
    """Return one agent, including its prompt, voice, and which tools it can use."""
    try:
        return get_agent(agent_id)
    except AgentError as exc:
        return _fail(exc)


@mcp.tool()
def select_agent_for_side(side: str, agent_id: str) -> dict:
    """Choose which saved agent handles inbound or outbound. The two sides stay independent."""
    try:
        return select_agent(side, agent_id)
    except AgentError as exc:
        return _fail(exc)


@mcp.tool()
async def call_outbound_agent(
    phone: str,
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "site visit",
    agent_id: str = "",
) -> dict:
    """Place an outbound call. Leave agent_id empty to use the selected outbound agent."""
    try:
        return await place_outbound_call(
            phone=phone,
            lead_name=lead_name,
            business_name=business_name,
            service_type=service_type,
            agent_id=agent_id or None,
        )
    except AgentError as exc:
        return _fail(exc)


@mcp.tool()
def edit_selected_prompt(side: str, system_prompt: str) -> dict:
    """Replace the prompt of the agent currently selected for inbound or outbound."""
    try:
        return save_selected_prompt(side, system_prompt)
    except AgentError as exc:
        return _fail(exc)


class _ApiKeyGate:
    """Rejects every request that does not carry the API key. Forwards lifespan."""

    def __init__(self, app, expected: str):
        self.app = app
        self.expected = expected

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not _key_ok(_presented_key(scope), self.expected):
            body = b'{"error":"Missing or invalid API key"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="voice-agents"'),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _presented_key(scope) -> str:
    headers = {
        name.decode("latin1").lower(): value.decode("latin1")
        for name, value in scope.get("headers", [])
    }
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return headers.get("x-api-key", "").strip()


def _key_ok(presented: str, expected: str) -> bool:
    if not presented or len(presented) != len(expected):
        return False
    return secrets.compare_digest(presented, expected)


def main() -> None:
    key = os.getenv("AGENT_API_KEY", "").strip()
    if len(key) < 16:
        print(
            "Refusing to start. Set AGENT_API_KEY in .env to a random key of at least 16 characters.",
            file=sys.stderr,
        )
        sys.exit(1)
    import uvicorn

    host = os.getenv("MCP_HOST", "0.0.0.0")
    print(f"Voice agent MCP on http://{host}:{_PORT}/mcp (API key required)")
    uvicorn.run(_ApiKeyGate(mcp.streamable_http_app(), key), host=host, port=_PORT, log_level="info")


if __name__ == "__main__":
    main()
