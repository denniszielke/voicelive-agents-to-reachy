"""Build the two agent container images in Azure Container Registry.

This script **only builds** the images — it does not register the hosted
agents. Use ``python -m scripts.deploy_agents`` to build *and* register.

Configuration is read from ``./.env`` (written by ``azd up``).

Usage::

    python -m scripts.build_containers            # timestamped tag + :latest
    python -m scripts.build_containers 20240608   # explicit tag
"""

from __future__ import annotations

import sys

from scripts._helpers import AGENTS, build_image, resolve_registry


def main(argv: list[str]) -> int:
    tag = argv[0] if argv else None
    registry = resolve_registry()
    for agent in AGENTS:
        build_image(registry, agent.name, agent.dockerfile, tag)
    print("All images built successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
