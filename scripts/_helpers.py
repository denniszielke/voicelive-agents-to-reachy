"""Shared helpers for building and deploying the two Foundry hosted agents.

Configuration is sourced from ``./.env`` (written by ``azd up`` via the
``postdeploy`` hook in ``azure.yaml``). The two agents are:

* ``weather-agent``        — outside temperature (LangGraph).
* ``homeassistant-agent``  — inside temperature (Agent Framework).

Both are built from a Dockerfile, pushed to Azure Container Registry (ACR) and
registered as a Foundry hosted agent version with the Responses, A2A and
Invocations endpoint protocols enabled (Invocations is what VoiceLive routes
each turn through).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentCard, AgentCardSkill
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

# Load the repository-root .env explicitly so the scripts work regardless of the
# current working directory (azd writes it there via the postdeploy hook).
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
load_dotenv(override=False)


@dataclass(frozen=True)
class AgentConfig:
    """Everything needed to build and register one hosted agent."""

    name: str
    description: str
    dockerfile: str  # relative to REPO_ROOT
    card: AgentCard
    cpu: str = "1"
    memory: str = "2Gi"
    env_vars: dict[str, str] = field(default_factory=dict)
    # Endpoint protocol the agent exposes:
    #   "responses"       — OpenAI-compatible HTTP (VoiceLive front-end / orchestrator)
    #   "invocations_ws"  — duplex WebSocket (specialist sub-agents)
    protocol: str = "responses"
    # Protocol version expected by the SDK inside the container:
    #   "1.0.0" for azure-ai-agentserver-invocations (weather / homeassistant, invocations_ws)
    #   "2.0.0" for agent-framework-foundry-hosting >= 1.0.0a260630 (orchestrator, responses)
    protocol_version: str = "1.0.0"


AGENTS: list[AgentConfig] = [
    AgentConfig(
        name="weather-agent",
        description="Weather agent — reports the current outside (outdoor) temperature.",
        dockerfile="src/weather_agent/Dockerfile",
        protocol="invocations_ws",
        protocol_version="1.0.0",
        card=AgentCard(
            description="Reports the current outside (outdoor) temperature.",
            version="1.0",
            skills=[
                AgentCardSkill(
                    id="outside-temperature",
                    name="Outside Temperature",
                    description="Return the current outdoor temperature in degrees Celsius.",
                ),
            ],
        ),
    ),
    AgentConfig(
        name="homeassistant-agent",
        description="Home Assistant agent — reports the current inside (indoor) temperature.",
        dockerfile="src/homeassistant_agent/Dockerfile",
        protocol="invocations_ws",
        protocol_version="1.0.0",
        card=AgentCard(
            description="Reports the current inside (indoor) temperature.",
            version="1.0",
            skills=[
                AgentCardSkill(
                    id="inside-temperature",
                    name="Inside Temperature",
                    description="Return the current indoor temperature in degrees Celsius.",
                ),
            ],
        ),
    ),
    AgentConfig(
        name="orchestrator-agent",
        description="Orchestrator agent — routes temperature questions to weather-agent or homeassistant-agent.",
        dockerfile="src/orchestrator_agent/Dockerfile",
        protocol="responses",
        protocol_version="2.0.0",
        card=AgentCard(
            description="Routes temperature questions to the appropriate specialist agent.",
            version="1.0",
            skills=[
                AgentCardSkill(
                    id="temperature-routing",
                    name="Temperature Routing",
                    description=(
                        "Route temperature questions to weather-agent (outside) or "
                        "homeassistant-agent (inside) and return their answers."
                    ),
                ),
            ],
        ),
    ),
]


def get_env(name: str, required: bool = True, default: str | None = None) -> str:
    """Read an environment variable, raising if a required one is missing."""
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def get_client() -> AIProjectClient:
    """Return a Foundry project client authenticated with the local identity."""
    return AIProjectClient(
        endpoint=get_env("AZURE_AI_PROJECT_ENDPOINT"),
        credential=DefaultAzureCredential(),
    )


def _registry_name(login_server: str) -> str:
    return login_server.removesuffix(".azurecr.io")


def resolve_registry() -> str:
    """Resolve the ACR login server (e.g. ``myacr.azurecr.io``).

    Precedence: ``AZURE_CONTAINER_REGISTRY_ENDPOINT`` > ``AZURE_REGISTRY`` >
    discovery from the resource group via ``az acr list``.
    """
    registry = os.getenv("AZURE_CONTAINER_REGISTRY_ENDPOINT") or os.getenv("AZURE_REGISTRY")
    if registry:
        return registry
    resource_group = get_env("AZURE_RESOURCE_GROUP")
    result = subprocess.run(
        ["az", "acr", "list", "-g", resource_group, "--query", "[0].loginServer", "-o", "tsv"],
        check=False,
        capture_output=True,
        text=True,
    )
    registry = result.stdout.strip()
    if not registry:
        raise RuntimeError(
            "Could not resolve a container registry. Set "
            "AZURE_CONTAINER_REGISTRY_ENDPOINT in ./.env or ensure an Azure "
            f"Container Registry exists in {resource_group}."
        )
    print(f"==> Resolved container registry: {registry}")
    return registry


def build_image(registry: str, name: str, dockerfile: str, tag: str | None = None) -> str:
    """Build ``name:tag`` and ``name:latest`` in ACR from the repo-root context.

    Returns the fully-qualified timestamped image reference.
    """
    build_tag = tag or datetime.now().strftime("%Y%m%d%H%M%S")
    image_tag = f"{registry}/{name}:{build_tag}"
    latest_tag = f"{registry}/{name}:latest"
    print(f"==> Building {image_tag} (and :latest) from {dockerfile}")
    subprocess.run(
        [
            "az", "acr", "build",
            "--registry", _registry_name(registry),
            "--image", image_tag,
            "--image", latest_tag,
            "--platform", "linux/amd64",
            "--file", dockerfile,
            str(REPO_ROOT),
        ],
        check=True,
    )
    return image_tag
