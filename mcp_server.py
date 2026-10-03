"""MCP server for the caller personas.

Tools match the /api/agents routes. Run it from this folder:

    python mcp_server.py

It reads the same local database and .env as the dashboard. Set AGENT_API_KEY
only if another program will call the HTTP routes. This process does not need it.
"""

import os

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(".env")

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


if __name__ == "__main__":
    os.environ.setdefault("FASTMCP_LOG_LEVEL", "WARNING")
    mcp.run()
