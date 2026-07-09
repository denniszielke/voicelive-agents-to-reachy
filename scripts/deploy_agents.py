"""Build and register the Foundry hosted agents.

For each agent this:
  1. Builds the container image in ACR.
  2. Registers a hosted agent version in the Foundry project.
  3. Enables the endpoint protocol(s) the agent speaks:
       * orchestrator-agent  — Responses + A2A + Invocations (VoiceLive front-end).
       * weather-agent /
         homeassistant-agent — invocations_ws (duplex WebSocket the
                                orchestrator connects to).

Configuration is read from ``./.env`` (written by ``azd up``).

Usage::

    python -m scripts.deploy_agents
"""

from __future__ import annotations

from azure.ai.projects.models import (
    A2AProtocolConfiguration,
    AgentEndpointConfig,
    AgentEndpointProtocol,
    ContainerConfiguration,
    HostedAgentDefinition,
    InvocationsProtocolConfiguration,
    InvocationsWsProtocolConfiguration,
    ProtocolConfiguration,
    ProtocolVersionRecord,
    ResponsesProtocolConfiguration,
)

from scripts._helpers import AGENTS, build_image, get_client, get_env, resolve_registry


def _shared_env() -> dict[str, str]:
    """Environment variables passed to every hosted agent container."""
    project_endpoint = get_env("AZURE_AI_PROJECT_ENDPOINT")
    model = get_env("AZURE_AI_MODEL_DEPLOYMENT_NAME", default="gpt-5.4-mini")
    return {
        "AZURE_AI_PROJECT_ENDPOINT": project_endpoint,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": model,
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": model,
        "MODEL_DEPLOYMENT_NAME": model,
    }


def _endpoint_and_protocol(protocol: str, version: str):
    """Return (AgentEndpointConfig, [ProtocolVersionRecord]) for a protocol."""
    if protocol == "invocations_ws":
        endpoint_config = AgentEndpointConfig(
            protocol_configuration=ProtocolConfiguration(
                invocations_ws=InvocationsWsProtocolConfiguration(),
            ),
        )
        versions = [
            ProtocolVersionRecord(
                protocol=AgentEndpointProtocol.INVOCATIONS_WS,
                version=version,
            ),
        ]
        return endpoint_config, versions

    # Default: Responses front-end (also enable A2A + Invocations for VoiceLive).
    endpoint_config = AgentEndpointConfig(
        protocol_configuration=ProtocolConfiguration(
            responses=ResponsesProtocolConfiguration(),
            a2a=A2AProtocolConfiguration(),
            invocations=InvocationsProtocolConfiguration(),
        ),
    )
    versions = [
        ProtocolVersionRecord(
            protocol=AgentEndpointProtocol.RESPONSES,
            version=version,
        ),
    ]
    return endpoint_config, versions


def deploy() -> None:
    client = get_client()
    registry = resolve_registry()
    shared_env = _shared_env()

    for agent in AGENTS:
        image_tag = build_image(registry, agent.name, agent.dockerfile)
        env_vars = {**shared_env, **agent.env_vars}

        endpoint_config, protocol_versions = _endpoint_and_protocol(
            agent.protocol, agent.protocol_version
        )

        client.agents.create_version(
            agent_name=agent.name,
            description=agent.description,
            definition=HostedAgentDefinition(
                cpu=agent.cpu,
                memory=agent.memory,
                container_configuration=ContainerConfiguration(image=image_tag),
                protocol_versions=protocol_versions,
                environment_variables=env_vars,
            ),
            metadata={"voiceLiveCompatible": "true"},
        )
        print(f"Hosted agent '{agent.name}' version created.")

        client.agents.update_details(
            agent_name=agent.name,
            agent_endpoint=endpoint_config,
            agent_card=agent.card,
        )
        print(f"  '{agent.protocol}' protocol enabled for '{agent.name}'.")

    print("\nAll agents deployed.")


if __name__ == "__main__":
    deploy()
