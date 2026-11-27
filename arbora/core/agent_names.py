"""The names and tool grants of the agents the host runs by default."""

from __future__ import annotations

__all__ = [
    "FLEET_AGENT_NAME",
    "FLEET_TOOLS",
    "WEBUI_AGENT_NAME",
    "preloaded_agent_tools",
]

#: The intent tools through which the fleet agent reaches the multiplexer.
FLEET_TOOLS = ("find", "act")
#: The fleet's default agent: an A2A message or ``agents.run`` that names no
#: ``metadata.agent_name`` runs on it.
FLEET_AGENT_NAME = "arbora"
#: The default profile requested by overstory's orchestrator model.
WEBUI_AGENT_NAME = "webui-assistant"


def preloaded_agent_tools(agent_name: str) -> tuple[str, ...] | None:
    """Select hosted intent tools; caller/session admission remains unchanged."""
    return FLEET_TOOLS if agent_name in {FLEET_AGENT_NAME, WEBUI_AGENT_NAME} else None
