"""Weather Agent — outside temperature (Foundry hosted, LangGraph).

Answers questions about the **outside** temperature. It reads the value from a
weather API (mocked here by :func:`_query_weather_api` — the real call is
provided later) and keeps the last known value in an in-memory cache together
with the timestamp it was refreshed. If the API call fails the agent falls back
to the last cached value, or to a hard-coded default so it always returns
something.

Built with LangGraph and served over the Azure AI Agent Server (Responses +
Invocations protocols) so Foundry / VoiceLive can stream turns to it.

Environment variables:
  AZURE_AI_PROJECT_ENDPOINT / FOUNDRY_PROJECT_ENDPOINT   Foundry project endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME / MODEL_DEPLOYMENT_NAME  chat model deployment
  APPLICATIONINSIGHTS_CONNECTION_STRING                  optional telemetry

Run locally from the project root:

    python -m src.weather_agent.agent
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from typing_extensions import Literal

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.websockets import WebSocket, WebSocketDisconnect

load_dotenv()

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(enable_live_metrics=True, logger_name="__main__")
    logging.getLogger("azure").setLevel(logging.WARNING)

deployment_name = os.environ.get("MODEL_DEPLOYMENT_NAME") or os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
project_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ["AZURE_AI_PROJECT_ENDPOINT"]

_token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://ai.azure.com/.default"
)


class _AzureTokenAuth(httpx.Auth):
    """Inject a fresh Entra token on every request to the Foundry OpenAI endpoint."""

    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer " + _token_provider()
        yield request


llm = ChatOpenAI(
    base_url=f"{project_endpoint}/openai/v1",
    api_key="placeholder",  # overridden by _AzureTokenAuth
    model=deployment_name,
    use_responses_api=True,
    http_client=httpx.Client(auth=_AzureTokenAuth()),
)


# ---------------------------------------------------------------------------
# Last-known-value cache
# ---------------------------------------------------------------------------

# Default returned when the weather API has never been reached successfully.
DEFAULT_OUTSIDE_TEMPERATURE_C = 29.0

_cache: dict[str, object] = {
    "temperature_c": DEFAULT_OUTSIDE_TEMPERATURE_C,
    "refreshed_at": "never",
    "stale": True,
}


def _query_weather_api() -> float:
    """Return the current outside temperature in °C.

    MOCK IMPLEMENTATION — replace the body with the real weather API call
    (LangChain tool / HTTP request) when it is available. It only needs to
    return a float in degrees Celsius.
    """
    return round(random.uniform(-5.0, 35.0), 1)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def get_outside_temperature() -> dict:
    """Get the current outside (outdoor) temperature in degrees Celsius.

    Reads the live value from the weather API. If the call fails, the last
    known value is returned instead (or a default if none was ever read).
    """
    try:
        value = _query_weather_api()
        _cache["temperature_c"] = value
        _cache["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        _cache["stale"] = False
    except Exception:  # noqa: BLE001 - keep serving on any API failure
        logger.exception("Weather API call failed; serving cached value")
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
        "location": "outside",
        "refreshed_at": refreshed_at,
        "measured_ago": measured_ago,
        "stale": _cache["stale"],
    }


tools = [get_outside_temperature]
tools_by_name = {t.name: t for t in tools}
llm_with_tools = llm.bind_tools(tools)

SYSTEM_MESSAGE = SystemMessage(
    content="""\
You are the Weather Agent. You report the current OUTSIDE (outdoor) temperature.

Guidelines:
- Always call get_outside_temperature before answering a temperature question;
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
)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def llm_call(state: MessagesState):
    return {"messages": [llm_with_tools.invoke([SYSTEM_MESSAGE] + state["messages"])]}


def tool_node(state: dict):
    result = []
    for tool_call in state["messages"][-1].tool_calls:
        t = tools_by_name[tool_call["name"]]
        observation = t.invoke(tool_call["args"])
        result.append(ToolMessage(content=observation, tool_call_id=tool_call["id"]))
    return {"messages": result}


def should_continue(state: MessagesState) -> Literal["environment", "__end__"]:
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "Action"
    return END


def build_agent() -> "StateGraph":
    agent_builder = StateGraph(MessagesState)
    agent_builder.add_node("llm_call", llm_call)
    agent_builder.add_node("environment", tool_node)
    agent_builder.add_edge(START, "llm_call")
    agent_builder.add_conditional_edges(
        "llm_call",
        should_continue,
        {"Action": "environment", END: END},
    )
    agent_builder.add_edge("environment", "llm_call")
    return agent_builder.compile()


graph = build_agent()


# ---------------------------------------------------------------------------
# invocations_ws server
# ---------------------------------------------------------------------------
# Wire format (JSON text frames):
#   client -> server : {"type": "message", "text": "<question>"}
#   server -> client : {"type": "done",    "text": "<answer>"}
#                      {"type": "error",   "message": "<detail>"}
# One reply per incoming message; the connection stays open for further turns.

app = InvocationAgentServerHost()


async def _answer(user_input: str) -> str:
    result = await graph.ainvoke({"messages": [HumanMessage(content=user_input)]})
    raw = result["messages"][-1].content
    if isinstance(raw, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in raw
        )
    return raw or ""


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
                answer = await _answer(user_input)
                await websocket.send_text(
                    json.dumps({"type": "done", "text": answer})
                )
            except Exception as exc:  # noqa: BLE001 - report per-turn failures
                logger.exception("Failed to answer weather question")
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)})
                )
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--query", type=str, default=None, help="Run a single query and exit")
    args = parser.parse_args()

    if args.query:
        result = graph.invoke({"messages": [HumanMessage(content=args.query)]})
        for msg in result["messages"]:
            print(f"{msg.type}: {msg.content}")
    else:
        app.run()
