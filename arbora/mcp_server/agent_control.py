"""Arbora transport adapter for AU's typed application control plane."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from arbora.agent_runs import AgentRunPort
from arbora.api.contracts import (
    AgentControlPlane,
    GraphRlmRequest,
    compose_agent_control_plane,
)
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import runtime
from arbora.mcp_server.host_caches import ToolTable

ControlPlaneFactory = Callable[..., AgentControlPlane]
ClientForSession = Callable[[Any], Any]


def compose_run_control_plane(
    client: Any, session: Any, *, origins: Any, config: RuntimeConfig
) -> AgentControlPlane:
    """The control plane that runs agents through the host's own run port."""
    return compose_agent_control_plane(
        client,
        session,
        origins=origins,
        agent_executor=AgentRunPort(origins=origins, ports=None, config=config),
    )


class ToolRegistrar(Protocol):
    def tool(self, **kwargs: Any) -> Callable[[Any], Any]: ...


def register_graph_rlm(
    mcp: ToolRegistrar,
    tools: ToolTable,
    *,
    client_for_session: ClientForSession,
    origins: Any,
    config: RuntimeConfig,
    factory: ControlPlaneFactory = compose_run_control_plane,
) -> None:
    """Register the sole retained AU operation from verified injected inputs."""

    @mcp.tool(
        name="graph_rlm",
        description=(
            "Run a recursive language model through the verified agent control plane."
        ),
        tags={"arbora", "agent-control-plane", "rlm"},
    )
    async def graph_rlm(request: GraphRlmRequest) -> str:
        with runtime.verified_tool_session_scope(tools.authority) as session:
            control_plane = factory(
                client_for_session(session), session, origins=origins, config=config
            )
            result = await control_plane.graph_rlm(request)
        return result.model_dump_json()

    tools["graph_rlm"] = graph_rlm
    runtime.ACTION_TOOL_ROUTES["graph_rlm"] = "/graph/rlm"


__all__ = ["register_graph_rlm"]
