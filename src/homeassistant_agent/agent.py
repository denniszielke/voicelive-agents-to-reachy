"""Home Assistant Agent — inside temperature (Foundry hosted).

Answers questions about the **inside** (indoor) temperature. It reads the value
from the Home Assistant API (mocked here by :func:`_query_home_assistant_api` —
the real call is provided later) and keeps the last known value in an in-memory
cache together with the timestamp it was refreshed. If the call fails the agent
falls back to the last cached value, or to a hard-coded default so it always
returns something.

Built with the Microsoft Agent Framework and served over Foundry hosting
(Responses + Invocations protocols) so Foundry / VoiceLive can stream turns
to it. Model calls are routed through Azure AI Foundry using Entra ID.

Environment variables:
  AZURE_AI_PROJECT_ENDPOINT                              Foundry project endpoint
  AZURE_OPENAI_CHAT_DEPLOYMENT_NAME /
  AZURE_AI_MODEL_DEPLOYMENT_NAME                         chat model deployment

Run locally from the project root:

    python -m src.homeassistant_agent.agent
"""
from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone

from agent_framework import tool
from agent_framework.foundry import FoundryChatClient
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from starlette.websockets import WebSocket, WebSocketDisconnect

load_dotenv()

logging.basicConfig(level=logging.WARNING)
logging.getLogger("azure").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_PROJECT_ENDPOINT = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
_MODEL_DEPLOYMENT = (
    os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
    or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    or "gpt-4.1-mini"
)


# ---------------------------------------------------------------------------
# Last-known-value cache
# ---------------------------------------------------------------------------

# Default returned when Home Assistant has never been reached successfully.
DEFAULT_INSIDE_TEMPERATURE_C = 21.0

_cache: dict[str, object] = {
    "temperature_c": DEFAULT_INSIDE_TEMPERATURE_C,
    "refreshed_at": "never",
    "stale": True,
}


def _query_home_assistant_api() -> float:
    """Return the current inside temperature in °C.

    MOCK IMPLEMENTATION — replace the body with the real Home Assistant API
    call when it is available. It only needs to return a float in degrees
    Celsius.
    """
    return round(random.uniform(18.0, 26.0), 1)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def get_inside_temperature() -> dict:
    """Get the current inside (indoor) temperature in degrees Celsius.

    Reads the live value from Home Assistant. If the call fails, the last known
    value is returned instead (or a default if none was ever read).
    """
    try:
        value = _query_home_assistant_api()
        _cache["temperature_c"] = value
        _cache["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        _cache["stale"] = False
    except Exception:  # noqa: BLE001 - keep serving on any API failure
        logger.exception("Home Assistant API call failed; serving cached value")
        _cache["stale"] = True

    refreshed_at = _cache["refreshed_at"]
    if refreshed_at == "never":
        measured_ago = "never measured"
    else:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(str(refreshed_at))).total_seconds()
        if elapsed < 60:
            measured_ago = f"{int(elapsed)} seconds ago"
        elif elapsed < 3600:
            measured_ago = f"{int(elapsed // 60)} minutes ago"
        else:
            measured_ago = f"{elapsed / 3600:.1f} hours ago"

    return {
        "temperature_c": _cache["temperature_c"],
        "unit": "celsius",
        "location": "inside",
        "refreshed_at": refreshed_at,
        "measured_ago": measured_ago,
        "stale": _cache["stale"],
    }


SYSTEM_PROMPT = """\
You are the Home Assistant Agent. You report the current INSIDE (indoor)
temperature of the home.

Guidelines:
- Always call get_inside_temperature before answering a temperature question;
  never guess the value.
- Report the temperature in degrees Celsius, rounded sensibly for speech.
- Always mention the measured_ago field — say something like "measured 3 minutes
  ago" or "this reading is from 2 hours ago".
- If the returned value is marked stale, say that the reading could not be
  refreshed and you are reporting the last known value.
- If measured_ago is "never measured", say the sensor has not been read yet and
  you are using the default value.
- Keep answers short and natural — they are spoken aloud.
"""


# ---------------------------------------------------------------------------
# Agent assembly
# ---------------------------------------------------------------------------

_credential = DefaultAzureCredential()

_chat_client = FoundryChatClient(
    project_endpoint=_PROJECT_ENDPOINT,
    model=_MODEL_DEPLOYMENT,
    credential=_credential,
)

agent = _chat_client.as_agent(
    name="homeassistant-agent",
    instructions=SYSTEM_PROMPT,
    tools=[get_inside_temperature],
)


# ---------------------------------------------------------------------------
# invocations_ws server
# ---------------------------------------------------------------------------
# Wire format (JSON text frames):
#   client -> server : {"type": "message", "text": "<question>"}
#   server -> client : {"type": "done",    "text": "<answer>"}
#                      {"type": "error",   "message": "<detail>"}
# One reply per incoming message; the connection stays open for further turns.

app = InvocationAgentServerHost()


@app.ws_handler
async def handle_ws(websocket: WebSocket) -> None:
    try:
        async for raw_frame in websocket.iter_text():
            try:
                evt = json.loads(raw_frame)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "invalid JSON frame"})
                )
                continue
            if evt.get("type") != "message":
                continue
            user_input = evt.get("text", "") or ""
            try:
                response = await agent.run(user_input)
                await websocket.send_text(
                    json.dumps({"type": "done", "text": response.text or ""})
                )
            except Exception as exc:  # noqa: BLE001 - report per-turn failures
                logger.exception("Failed to answer inside-temperature question")
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)})
                )
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    app.run()
