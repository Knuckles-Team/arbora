"""One deployed-shape fixture for the orchestration probes (ARBORA-FLEET-R052.4).

A container-manager and a mail connector sit in a real fleet catalog behind the real
fleet gateway; the engine is the shared recording engine. ``ask``, ``find`` and ``act``
run over the served MCP intent surface as the verified person, so a probe exercises
intent resolution, admission, the plan preview and the engine call, not a stub of them.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from fastmcp import FastMCP

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.gateway_ops import (
    ConfirmingTools,
    FleetGateway,
    tool_for_multiplexer_ops,
)
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.session_loads import SessionLoads
from arbora.mcp_server import runtime as mcp_runtime
from arbora.security import request_identity
from tests.api._served_support import RecordingEngine, confirmable_invocation, person

READ = {"readOnlyHint": True}


def _tool(server: str, name: str, description: str, **extra: Any) -> CatalogItem:
    return CatalogItem(
        f"fleet:tool:{server}/{name}",
        "tool",
        name,
        description=description,
        server=server,
        schema={"type": "object", "properties": {"all": {"type": "boolean"}}},
        annotations=extra.pop("annotations", READ),
        **extra,
    )


#: The fleet catalog: container-manager's four tools and one unrelated mail tool.
FLEET_ITEMS = (
    _tool("container-manager", "list_containers", "List the Docker containers"),
    _tool("container-manager", "pull_image", "Pull a container image"),
    _tool("container-manager", "create_container", "Create a container"),
    _tool("container-manager", "start_container", "Start a container"),
    _tool("mail", "send_email", "Send an email message"),
)
CONTAINER_TOOLS = tuple(
    item.name for item in FLEET_ITEMS if item.server == "container-manager"
)
CONTAINERS = [{"id": "c1", "image": "nginx", "state": "running"}]
RECORD = "record:engine:1"


class ProbeStack:
    """The served intent surface plus the engine and fleet the probes read."""

    def __init__(
        self,
        invocation: Any,
        engine: RecordingEngine,
        gateway: Any,
        monkeypatch: pytest.MonkeyPatch,
        who: Any,
    ) -> None:
        process = request_identity.mint_graph_session(who)
        monkeypatch.setattr(mcp_runtime, "_PROCESS_SESSION", process)
        self.engine = engine
        self.gateway = gateway
        self.child_calls: list[tuple[str, str, Any]] = []
        self._mcp = FastMCP("probe")
        mcp_runtime._register_served_intent_surface(self._mcp, invocation)

    def __call__(
        self, verb: str, intent: str, params: dict[str, Any] | None = None, **x: Any
    ) -> Any:
        async def call() -> Any:
            result = await self._mcp.call_tool(
                verb, {"intent": intent, "params": params or {}, **x}
            )
            return result.structured_content

        return asyncio.run(call())

    def op(self, verb: str, op: str, params: dict[str, Any], **x: Any) -> Any:
        async def call() -> Any:
            result = await self._mcp.call_tool(verb, {"op": op, "params": params, **x})
            return result.structured_content

        return asyncio.run(call())


class Fleet:
    """The real fleet gateway over the fixture catalog; child calls are recorded."""

    def __init__(self) -> None:
        self.child_calls: list[tuple[str, str, Any]] = []

        async def allowed(*_args: Any) -> bool:
            return True

        async def source() -> tuple[CatalogItem, ...]:
            return FLEET_ITEMS

        async def mount(*_a: Any, **_k: Any) -> None:
            return None

        async def invoke(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the probes load no tool into the session")

        ops = MultiplexerOps(
            catalog=FleetCatalog([source], allowed),
            sessions=SessionLoads(),
            loadable=allowed,
            mount=mount,
            notify=allowed,
            invoke=invoke,
            health=lambda: {},
        )

        async def delegated(
            server: str, tool: str, arguments: Any, _caller: Any
        ) -> Any:
            self.child_calls.append((server, tool, arguments))
            return {
                "content": [],
                "structuredContent": {"containers": CONTAINERS},
                "isError": False,
            }

        self.gateway = FleetGateway(
            tool_for=tool_for_multiplexer_ops(ops),
            policy_check=allowed,
            delegated_call=delegated,
            catalog_ops=ops,
            confirming_tools=ConfirmingTools(),
        )
        gateway = self.gateway

        async def fleet_search(
            *, caller: Any, query: str, context_budget_tokens: Any = None
        ) -> Any:
            out = (await gateway.search(caller, query=query))["items"]
            return out

        # The served ``find`` reads this binding off the gateway.
        gateway.fleet_search = fleet_search  # type: ignore[attr-defined]


def install_fleet(monkeypatch: pytest.MonkeyPatch) -> tuple[Fleet, dict[str, Any]]:
    """Compose the served invocation with the fleet gateway bound, then confirmable."""
    import tests.test_served_operation_api as harness

    fleet = Fleet()
    compose = harness.compose_served_invocation

    def with_fleet(**kwargs: Any) -> Any:
        # A ``None`` planner binding keeps the engine's own NlQuery: the probes ask the
        # engine, and no federated source is composed in this fixture.
        kwargs["bindings"] = {
            **kwargs.get("bindings", {}),
            "fleet_gateway": fleet.gateway,
            "nl_federation_planner": None,
        }
        kwargs["fleet_effect"] = fleet.gateway.effect
        return compose(**kwargs)

    monkeypatch.setattr(harness, "compose_served_invocation", with_fleet)
    return fleet, confirmable_invocation(monkeypatch)


def build_probe_stack(
    monkeypatch: pytest.MonkeyPatch,
    served: Any,
    held: tuple[Fleet, dict[str, Any]],
    engine_answers: dict[str, Any],
) -> ProbeStack:
    """The served intent surface as the verified person over the fixture engine."""
    import tests.api._served_support as support

    _app, client = served
    fleet, holder = held
    scopes = (
        "mcp:discover",
        "mcp:delegate",
        "query:nl",
        "query:unified",
        "sparql:read",
        "kg:read",
        "retrieval:read",
    )
    one = person("one")
    who = dataclasses.replace(one, roles=(*one.roles, *scopes))
    monkeypatch.setattr(support, "person", lambda *_a, **_k: who)
    engine = RecordingEngine().route(
        client, monkeypatch, engine_answers, {"fixture-verified": who}
    )
    probe = ProbeStack(holder["invocation"], engine, fleet.gateway, monkeypatch, who)
    probe.child_calls = fleet.child_calls
    return probe
