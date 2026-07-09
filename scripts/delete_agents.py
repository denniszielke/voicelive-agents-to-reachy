"""Delete the two Foundry hosted agents (weather-agent, homeassistant-agent).

Usage::

    python -m scripts.delete_agents
"""

from __future__ import annotations

from scripts._helpers import AGENTS, get_client


def _delete_agent(client, name: str) -> None:
    for attempt in (
        lambda: client.agents.delete(agent_name=name),
        lambda: client.agents.delete_agent(agent_name=name),
    ):
        try:
            attempt()
            print(f"Deleted hosted agent '{name}'.")
            return
        except AttributeError:
            continue
        except Exception as exc:  # pragma: no cover - tolerate not-found / API drift
            print(f"  WARN: could not delete agent '{name}': {exc}")
            return
    print(f"  WARN: no delete method available for agent '{name}'.")


def delete_all() -> None:
    client = get_client()
    for agent in AGENTS:
        print(f"==> Deleting hosted agent '{agent.name}'")
        _delete_agent(client, agent.name)
    print("\nDone.")


if __name__ == "__main__":
    delete_all()
