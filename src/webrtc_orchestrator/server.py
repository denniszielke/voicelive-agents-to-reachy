"""FastAPI server for Voice Live API with WebRTC using Agent Invocation.

Architecture:
  Browser <──WebRTC──> Voice Live API (audio + data channel)
  Browser <──WS──> This server <──WS──> Voice Live API (signaling + session control)

The server acts as a signaling proxy:
1. Opens a control WebSocket to Voice Live API (/voice-live/realtime/calls)
2. Forwards SDP offers from the browser to the service
3. Relays SDP answers back to the browser
4. Keeps the control channel open for session.update, tool calls, etc.

Audio flows directly between the browser and Voice Live API over WebRTC
(peer-to-peer RTP). Non-audio events travel over the WebRTC data channel.

Agent Invocation:
  Uses AgentSessionConfig pattern to connect with a Foundry Agent.
  The agent encapsulates model, instructions, and voice config —
  no model deployment name is needed on the client side.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import logging

import uvicorn
import websockets
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Load .env from workspace root
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_WORKSPACE_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# Configuration — Agent Invocation API
# ---------------------------------------------------------------------------

_FOUNDRY_ENDPOINT = os.environ.get("AZURE_AI_PROJECT_ENDPOINT", "").strip().rstrip("/")

# Bind to a Foundry hosted agent by default. Set AZURE_AI_AGENT_NAME in ./.env
# to choose which agent VoiceLive routes turns to. The default is the
# orchestrator-agent, which itself routes each turn to the specialist agents:
#   orchestrator-agent   — routes to weather-agent / homeassistant-agent
#   weather-agent        — outside (outdoor) temperature
#   homeassistant-agent  — inside (indoor) temperature
_DEFAULT_AGENT_NAME = "orchestrator-agent"
_AGENT_NAME = (
    os.environ.get("AZURE_AI_AGENT_NAME", "").strip()
    or os.environ.get("AZURE_AI_WORKFLOW_AGENT_NAME", "").strip()
    or _DEFAULT_AGENT_NAME
)
_AGENT_VERSION = os.environ.get("AZURE_AI_AGENT_VERSION", "").strip()
_PROJECT_NAME = os.environ.get("AZURE_AI_PROJECT_NAME", "").strip()
# Fall back to the project name embedded in the Foundry endpoint path, e.g.
# https://<resource>.services.ai.azure.com/api/projects/<project> -> <project>
if not _PROJECT_NAME and _FOUNDRY_ENDPOINT:
    _PROJECT_NAME = _FOUNDRY_ENDPOINT.rstrip("/").rsplit("/", 1)[-1]
_CONVERSATION_ID = os.environ.get("AZURE_AI_CONVERSATION_ID", "").strip()
_FOUNDRY_RESOURCE_OVERRIDE = os.environ.get("FOUNDRY_RESOURCE_OVERRIDE", "").strip()
_API_VERSION = "2026-01-01-preview"

# Per-agent greeting instructions (injected as a system turn so the hosted
# agent introduces itself in its own voice on session start).
_AGENT_GREETINGS: dict[str, str] = {
    "orchestrator-agent": (
        "Greet the user and introduce yourself as the temperature assistant for Reachy. "
        "Mention you can report both the current indoor and outdoor temperature, and "
        "how long ago each reading was taken, by asking the right specialist. Keep it "
        "brief and natural — it will be spoken aloud."
    ),
    "weather-agent": (
        "Greet the user and introduce yourself as the weather agent for Reachy. "
        "Mention you can report the current outdoor temperature and how long ago "
        "the reading was taken. Keep it brief and natural — it will be spoken aloud."
    ),
    "homeassistant-agent": (
        "Greet the user and introduce yourself as the home assistant agent for Reachy. "
        "Mention you can report the current indoor temperature and how long ago "
        "the reading was taken. Keep it brief and natural — it will be spoken aloud."
    ),
}

# Voice Live WebRTC endpoint
_VOICE_LIVE_HOST = os.environ.get("AZURE_VOICELIVE_ENDPOINT", "").strip().rstrip("/")
# VoiceLive realtime model deployed by the infra (see AZURE_VOICELIVE_MODEL output)
_VOICE_LIVE_MODEL = os.environ.get("AZURE_VOICELIVE_MODEL", "").strip() or "gpt-realtime"

# Which agents the browser can connect to. AZURE_AI_AGENT_NAMES is a
# comma-separated list; falls back to AZURE_AI_AGENT_NAME, then the
# orchestrator-agent (which fans out to the specialists on its own).
_raw_names = os.environ.get("AZURE_AI_AGENT_NAMES", "").strip()
CONFIGURED_AGENTS: list[str] = (
    [n.strip() for n in _raw_names.split(",") if n.strip()]
    if _raw_names
    else ([_AGENT_NAME] if _AGENT_NAME else ["orchestrator-agent"])
)

# Startup diagnostics
logger.info(
    "Config: AGENTS=%r, PROJECT_NAME=%r, HOST=%r",
    CONFIGURED_AGENTS, _PROJECT_NAME, _VOICE_LIVE_HOST,
)


def _build_voicelive_ws_url(agent_name: str) -> str:
    """Build the Voice Live WebSocket URL for a specific agent."""
    if _VOICE_LIVE_HOST:
        host = _VOICE_LIVE_HOST.replace("https://", "").replace("http://", "").split("/")[0]
    elif _FOUNDRY_ENDPOINT:
        # Extract just the hostname from project endpoint
        # e.g. https://<resource>.services.ai.azure.com/api/projects/<project>
        host = _FOUNDRY_ENDPOINT.replace("https://", "").replace("http://", "").split("/")[0]
    else:
        raise RuntimeError(
            "Set AZURE_VOICELIVE_ENDPOINT or AZURE_AI_PROJECT_ENDPOINT"
        )

    # Build query parameters for agent invocation
    # model is required for WebRTC /calls endpoint (Voice Live managed model)
    params: dict[str, str] = {"api-version": _API_VERSION, "model": _VOICE_LIVE_MODEL}
    if agent_name:
        params["agent-name"] = agent_name
    if _PROJECT_NAME:
        params["agent-project-name"] = _PROJECT_NAME
    if _AGENT_VERSION:
        params["agent-version"] = _AGENT_VERSION
    if _CONVERSATION_ID:
        params["conversation_id"] = _CONVERSATION_ID
    if _FOUNDRY_RESOURCE_OVERRIDE:
        params["foundry_resource_override"] = _FOUNDRY_RESOURCE_OVERRIDE

    url = f"wss://{host}/voice-live/realtime/calls?{urlencode(params)}"
    return url


async def _get_auth_token() -> str:
    """Get a bearer token for the Voice Live API (Entra ID required for agent invocation)."""
    credential = DefaultAzureCredential()
    # Agent invocation requires a token scoped to the AI Foundry Agent
    # service audience (https://ai.azure.com), not cognitiveservices.
    token = await credential.get_token(
        "https://ai.azure.com/.default"
    )
    await credential.close()
    return token.token


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Reachy Temperature Agents — WebRTC (Voice Live)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agents": CONFIGURED_AGENTS,
        "project_name": _PROJECT_NAME or None,
    }


@app.get("/config")
async def config():
    """Return the list of configured agents for the browser to auto-connect."""
    return {"agents": CONFIGURED_AGENTS}


@app.get("/")
async def index():
    """Serve the browser client."""
    return FileResponse(Path(__file__).parent / "index.html")


# ---------------------------------------------------------------------------
# WebSocket signaling endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def signaling_ws(ws: WebSocket, agent: str = Query(default="")):
    """Signaling relay between browser and Voice Live API.

    Pass ?agent=<name> to connect to a specific hosted agent.
    Defaults to the first entry in CONFIGURED_AGENTS.
    """
    agent_name = agent.strip() or (CONFIGURED_AGENTS[0] if CONFIGURED_AGENTS else _AGENT_NAME)
    await ws.accept()
    logger.info("[%s] Browser signaling WebSocket connected", agent_name)

    # Get auth token and build Voice Live WebSocket URL (agent invocation)
    try:
        token = await _get_auth_token()
        voicelive_url = _build_voicelive_ws_url(agent_name)
    except Exception as exc:
        logger.error("[%s] Failed to build connection: %s", agent_name, exc)
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close()
        return

    logger.info(
        "[%s] Connecting to Voice Live (project=%s, version=%s)",
        agent_name, _PROJECT_NAME, _AGENT_VERSION or "latest",
    )
    logger.info("[%s] URL: %s", agent_name, voicelive_url)

    # Connect to Voice Live API control channel
    headers = {"Authorization": f"Bearer {token}"}
    try:
        service_ws = await websockets.connect(
            voicelive_url,
            additional_headers=headers,
            max_size=None,
        )
    except websockets.exceptions.InvalidStatus as exc:
        body = exc.response.body.decode() if exc.response.body else ""
        logger.error(
            "[%s] Voice Live rejected connection: HTTP %s\nURL: %s\nResponse: %s",
            agent_name, exc.response.status_code, voicelive_url, body,
        )
        await ws.send_json({
            "type": "error",
            "message": f"[{agent_name}] Service connection failed: HTTP {exc.response.status_code} - {body}",
        })
        await ws.close()
        return
    except Exception as exc:
        logger.error("[%s] Failed to connect to Voice Live API: %s", agent_name, exc)
        await ws.send_json({"type": "error", "message": f"[{agent_name}] Service connection failed: {exc}"})
        await ws.close()
        return

    logger.info("[%s] Connected to Voice Live control channel", agent_name)

    # Relay messages bidirectionally
    async def _relay_service_to_browser():
        """Forward messages from Voice Live API to browser, with turn logging."""
        greeting_sent = False
        try:
            async for raw in service_ws:
                msg = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                msg_type = msg.get("type", "")
                logger.debug("[%s] Service -> Browser: %s", agent_name, msg_type)

                # After session is confirmed ready, fire a proactive greeting once.
                if msg_type == "session.updated" and not greeting_sent:
                    greeting_sent = True
                    instruction = _AGENT_GREETINGS.get(
                        agent_name,
                        "Greet the user and briefly introduce yourself and your capabilities. "
                        "Keep it short and natural \u2014 it will be spoken aloud.",
                    )
                    try:
                        # Inject a system turn with the greeting instruction
                        await service_ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "system",
                                "content": [{"type": "input_text", "text": instruction}],
                            },
                        }))
                        # Trigger the LLM to generate and speak the greeting
                        await service_ws.send(json.dumps({"type": "response.create"}))
                        logger.info("[%s] Proactive greeting triggered", agent_name)
                    except Exception as exc:
                        logger.warning("[%s] Failed to send proactive greeting: %s", agent_name, exc)

                # Console logging for key transcript events
                if msg_type == "conversation.item.input_audio_transcription.completed":
                    text = msg.get("transcript", "").strip()
                    if text:
                        logger.info("[%s] User said: %r", agent_name, text)
                elif msg_type == "response.audio_transcript.done":
                    text = msg.get("transcript", "").strip()
                    if text:
                        logger.info("[%s] Agent replied: %r", agent_name, text)
                await ws.send_json(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.info("[%s] Voice Live API WebSocket closed", agent_name)
        except WebSocketDisconnect:
            logger.info("[%s] Browser disconnected while relaying from service", agent_name)
        except Exception as exc:
            logger.error("[%s] Error relaying service->browser: %s", agent_name, exc)

    async def _relay_browser_to_service():
        """Forward messages from browser to Voice Live API."""
        try:
            while True:
                data = await ws.receive_text()
                msg = json.loads(data)
                msg_type = msg.get("type", "")
                logger.debug("[%s] Browser -> Service: %s", agent_name, msg_type)
                await service_ws.send(json.dumps(msg))
        except WebSocketDisconnect:
            logger.info("[%s] Browser signaling WebSocket disconnected", agent_name)
        except Exception as exc:
            logger.error("[%s] Error relaying browser->service: %s", agent_name, exc)

    # Run both relay tasks concurrently
    relay_task = asyncio.gather(
        _relay_service_to_browser(),
        _relay_browser_to_service(),
        return_exceptions=True,
    )

    try:
        await relay_task
    finally:
        try:
            await service_ws.close()
        except Exception:
            pass
        logger.info("[%s] Signaling session ended", agent_name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("WEBRTCLIVE_PORT", "8090"))
    logger.info("Starting WebRTC Voice Live server on port %d (agents: %s)", port, CONFIGURED_AGENTS)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
