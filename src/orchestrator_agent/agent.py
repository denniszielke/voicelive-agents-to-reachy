"""Orchestrator Agent — routes voice requests to the appropriate specialist agent.

Knows about all registered agents and routes incoming questions to the correct
one based on intent:

  - ``weather-agent``         : outside (outdoor) temperature questions
  - ``homeassistant-agent``   : inside (indoor) temperature questions

Sub-agents are reached over the Foundry ``invocations_ws`` protocol (a duplex
WebSocket).  Each sub-agent container serves a small JSON wire format:

  client -> server : {"type": "message", "text": "<question>"}
  server -> client : {"type": "done",    "text": "<answer>"}
                     {"type": "error",   "message": "<detail>"}

The WebSocket URL is derived from the shared project endpoint, so no extra URLs
or secrets are required.  The orchestrator itself is hosted in Foundry via the
Responses protocol so VoiceLive can stream turns to it.

Environment variables:
  AZURE_AI_PROJECT_ENDPOINT              Foundry project endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME /
  AZURE_OPENAI_CHAT_DEPLOYMENT_NAME      Chat model deployment for orchestration

Run locally from the project root::

    python -m src.orchestrator_agent.agent
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from urllib.parse import quote, urlparse

import websockets
from agent_framework import tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.WARNING)
logging.getLogger("azure").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_PROJECT_ENDPOINT = os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/")
_MODEL_DEPLOYMENT = (
    os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
    or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    or "gpt-4.1-mini"
)

# Preview flag required by the invocations_ws protocol.
_FOUNDRY_FEATURES = "HostedAgents=V1Preview"
# The AzureML gateway rejects the WebSocket upgrade (empty-body HTTP 400) unless
# an api-version is supplied. Overridable if the service bumps the version.
_INVOCATIONS_WS_API_VERSION = os.getenv(
    "AZURE_AI_INVOCATIONS_WS_API_VERSION", "2025-11-15-preview"
)

_credential = DefaultAzureCredential()
_token_provider = get_bearer_token_provider(_credential, "https://ai.azure.com/.default")


# ---------------------------------------------------------------------------
# Sub-agent caller — Foundry invocations_ws (duplex WebSocket)
# ---------------------------------------------------------------------------

def _ws_url(agent_name: str, session_id: str) -> str:
    """Build the Foundry-side invocations_ws URL for a hosted agent.

    ``AZURE_AI_PROJECT_ENDPOINT`` looks like
    ``https://{account}.services.ai.azure.com/api/projects/{project}``; the
    account host and project name are extracted from it.
    """
    parsed = urlparse(_PROJECT_ENDPOINT)
    host = parsed.netloc
    project_name = parsed.path.rstrip("/").split("/")[-1]
    query = (
        f"project_name={quote(project_name)}"
        f"&agent_name={quote(agent_name)}"
        f"&agent_session_id={quote(session_id)}"
        f"&foundry_features={_FOUNDRY_FEATURES}"
        f"&api-version={quote(_INVOCATIONS_WS_API_VERSION)}"
    )
    return (
        f"wss://{host}/api/projects/agents/endpoint/protocols/invocations_ws?{query}"
    )


async def _call_agent_ws(agent_name: str, question: str) -> str:
    """Ask a Foundry hosted agent a question over its invocations_ws endpoint."""
    session_id = f"orch-{uuid.uuid4().hex[:8]}"
    url = _ws_url(agent_name, session_id)
    headers = {"Authorization": f"Bearer {_token_provider()}"}
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "message", "text": question}))
            async for frame in ws:
                if isinstance(frame, (bytes, bytearray)):
                    frame = frame.decode("utf-8", "ignore")
                try:
                    evt = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "done":
                    return evt.get("text", "") or ""
                if etype == "error":
                    return (
                        f"Agent '{agent_name}' error: "
                        f"{evt.get('message', 'unknown error')}"
                    )
        return "No answer received from agent."
    except Exception as exc:  # noqa: BLE001 - surface failures to the model
        logger.exception("Failed to call agent '%s' over invocations_ws", agent_name)
        return f"Agent '{agent_name}' is unavailable: {exc}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
async def ask_weather_agent(question: str) -> str:
    """Ask the weather agent about the current outside (outdoor) temperature.

    Use this tool when the user asks about outdoor temperature, outside weather,
    or what the temperature is outside.
    """
    return await _call_agent_ws("weather-agent", question)


@tool
async def ask_homeassistant_agent(question: str) -> str:
    """Ask the home assistant agent about the current inside (indoor) temperature.

    Use this tool when the user asks about indoor temperature, inside temperature,
    or what the temperature is inside the house / home.
    """
    return await _call_agent_ws("homeassistant-agent", question)


# ---------------------------------------------------------------------------
# Agent assembly
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the Orchestrator Agent. Your job is to route temperature questions to
the correct specialist agent and relay the answer naturally.

Available specialist agents:
- ask_weather_agent        : knows the current OUTSIDE (outdoor) temperature
- ask_homeassistant_agent  : knows the current INSIDE (indoor) temperature

Guidelines:
- Always call the appropriate tool — never guess a temperature value.
- Outside / outdoor / exterior question  → use ask_weather_agent.
- Inside / indoor / interior / home question → use ask_homeassistant_agent.
- If the user asks about both temperatures, call both tools and combine the answers.
- Relay the specialist agent's answer verbatim or rephrase it naturally.
- Keep answers short and natural — they are spoken aloud via VoiceLive.
"""

_chat_client = FoundryChatClient(
    project_endpoint=_PROJECT_ENDPOINT,
    model=_MODEL_DEPLOYMENT,
    credential=_credential,
)

agent = _chat_client.as_agent(
    name="orchestrator-agent",
    instructions=SYSTEM_PROMPT,
    tools=[ask_weather_agent, ask_homeassistant_agent],
)


def _run_devui() -> None:
    """Serve the orchestrator agent in the Agent Framework DevUI.

    Opens a local web interface to interactively test the agent and inspect
    OpenTelemetry traces of tool calls to the sub-agents. Not for production —
    see https://learn.microsoft.com/en-us/agent-framework/devui/.
    """
    from agent_framework.devui import serve

    host = os.getenv("DEVUI_HOST", "127.0.0.1")
    port = int(os.getenv("DEVUI_PORT", "8080"))
    serve(
        entities=[agent],
        host=host,
        port=port,
        auto_open=True,
        instrumentation_enabled=True,
        auth_enabled=False,
    )


if __name__ == "__main__":
    # Set DEVUI=1 to observe the orchestrator locally in the Agent Framework
    # DevUI instead of hosting it via the Foundry Responses protocol.
    if os.getenv("DEVUI", "").strip().lower() in {"1", "true", "yes"}:
        _run_devui()
    else:
        ResponsesHostServer(agent).run()
