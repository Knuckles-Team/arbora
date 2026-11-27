"""Served city layout: the engine world view's one spatial query.

Round-trips ``fleet.city.layout`` through the real invocation chokepoint
(ARBORA-FLEET-R084): arbora serves the spatial layout of the fleet tenant
from the Medulla world view as the engine's stable envelope, unchanged --
no placement logic and no second registry in arbora. The engine's typed
unknown-tenant and unknown-layout refusals are preserved in arbora's closed
error enum, and an uncomposed world-view port fails closed with a typed
``UNAVAILABLE``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.verbs import dispatch_verb
from arbora.api.ops import fleet_city as fleet_city_ops
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Registry, Surface
from arbora.mcp_server.serving import MCPProjection
from arbora.observability.state import SubsystemWindows
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import setup_services

pytestmark = pytest.mark.spec("ARBORA-FLEET-R084")

caller = make_verified_caller("fleet:read")

#: One engine world-view envelope: the spatial vocabulary, the relations
#: and the active manual overrides, as the engine serves them.
ENVELOPE = {
    "districts": (
        {"id": "district:one", "name": "Old Town"},
        {"id": "district:two", "name": "Harbor"},
    ),
    "buildings": ({"id": "building:one", "name": "Exchange"},),
    "rooms": ({"id": "room:one", "name": "Atrium"},),
    "streets": ({"id": "street:one", "name": "Canal Row"},),
    "relations": (
        {"from": "building:one", "rel": "locatedIn", "to": "district:one"},
        {"from": "room:one", "rel": "position", "to": "building:one"},
        {"from": "building:one", "rel": "hasInterior", "to": "room:one"},
        {"from": "building:one", "rel": "position", "to": "street:one"},
    ),
    "overrides": (
        {"id": "override:one", "connector": "connector:one", "state": "active"},
    ),
}


class _ClaimsClient:
    """One shared fake client that records the bound per-request claims."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] | None = None

    @contextlib.contextmanager
    def use_verified_context(self, claims: dict[str, Any]):
        self.claims = claims
        try:
            yield self
        finally:
            self.claims = None


class FakeLayoutPort:
    """A recorded ``CityLayoutPort`` double: the engine envelope, no logic."""

    def __init__(self, *, envelope: Any = ENVELOPE, refused: Any = None) -> None:
        self.envelope = envelope
        self.refused = refused
        self.calls: list[dict[str, Any]] = []

    async def layout(self, *, tenant: str, context: Any) -> Any:
        self.calls.append({"tenant": tenant})
        if self.refused is not None:
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused(*self.refused)
        return self.envelope


def _services(port: Any | None) -> Any:
    """One invoke composition whose composite dispatch reaches the real handler."""
    client = _ClaimsClient()

    async def caller_client(_tenant: str) -> Any:
        return client

    async def verify_current(current: Any) -> Any:
        return current

    async def service_client(_tenant: str) -> Any:
        return client

    async def service_claims(_tenant: str, required_scopes: frozenset[str]) -> dict:
        return {
            "principal": "service:arbora",
            "tenant": _tenant,
            "scopes": sorted(required_scopes),
            "policy_version": "v1",
            "delegation": False,
        }

    async def check_access(_client: Any, _current: Any, _subject: str) -> bool:
        return False

    async def eg_dispatch(_binding: Any, _params: Any, _context: Any) -> Any:
        raise AssertionError("city layout never dispatches the engine port")

    runtime = BoundOperationRuntime(
        caller_client=caller_client,
        verify_current=verify_current,
        service_client=service_client,
        service_claims=service_claims,
        check_access=check_access,
        eg_dispatch=eg_dispatch,
        service_scopes=frozenset({"example:execute"}),
        service_principal="service:arbora",
        bindings={} if port is None else {"city_layout": port},
    )
    services, *_rest = setup_services(
        effects=None,
        registry=Registry(fleet_city_ops.operations()),
        runtime=runtime,
    )
    return services


def _projection(services: Any) -> MCPProjection:
    return MCPProjection(
        services.registry,
        services,
        IntentResolver(),
        lambda: caller(),
        PolicyGate("none"),
        invoke,
        windows=SubsystemWindows(),
    )


def test_city_layout_serves_the_engine_envelope_and_refuses_unbound() -> None:
    """HTTP and MCP serve the engine envelope unchanged; unbound is UNAVAILABLE."""
    port = FakeLayoutPort()
    services = _services(port)

    result = asyncio.run(
        invoke("fleet.city.layout", {}, caller(), Surface.HTTP, services=services)
    )
    assert isinstance(result, OpResult) and result.code == "OK", result
    served = result.value["value"]
    # The stable envelope is served unchanged: no placement logic in arbora.
    assert served == ENVELOPE
    assert [d["id"] for d in served["districts"]] == ["district:one", "district:two"]
    assert {r["rel"] for r in served["relations"]} == {
        "locatedIn",
        "position",
        "hasInterior",
    }
    assert served["overrides"] == ENVELOPE["overrides"]
    # The read is tenant-scoped through the verified caller, never a param.
    assert port.calls == [{"tenant": "tenant:one"}]

    # The MCP ask verb reaches the same handler through the real pipeline.
    mcp = asyncio.run(
        dispatch_verb("ask", _projection(services), op="fleet.city.layout", params={})
    )
    assert mcp["ok"] is True, mcp
    assert mcp["result"]["value"] == ENVELOPE

    # The engine's typed refusals are preserved in the closed error enum.
    denied = asyncio.run(
        invoke(
            "fleet.city.layout",
            {},
            caller(),
            Surface.HTTP,
            services=_services(
                FakeLayoutPort(
                    refused=("SUBJECT_ACCESS_DENIED", {"subject": "tenant:foreign"})
                )
            ),
        )
    )
    assert isinstance(denied, OpError) and denied.code == "SUBJECT_ACCESS_DENIED"

    missing = asyncio.run(
        invoke(
            "fleet.city.layout",
            {},
            caller(),
            Surface.HTTP,
            services=_services(
                FakeLayoutPort(refused=("NOT_FOUND", {"subject": "layout:two"}))
            ),
        )
    )
    assert isinstance(missing, OpError) and missing.code == "NOT_FOUND"

    # An uncomposed world view fails closed with no envelope.
    unbound = asyncio.run(
        invoke(
            "fleet.city.layout", {}, caller(), Surface.HTTP, services=_services(None)
        )
    )
    assert isinstance(unbound, OpError) and unbound.code == "UNAVAILABLE"
    assert unbound.details == {"reason": "world view engine unbound"}
