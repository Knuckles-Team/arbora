"""Served city presence: one snapshot read on one bounded channel (ARBORA-OPS-R915).

Round-trips ``fleet.city.presence`` through the real invocation chokepoint
(HTTP and MCP, not a stub): the typed ``CityStatePort`` is the only path to
the Medulla city state, and an uncomposed port fails closed with a typed
``UNAVAILABLE``. The snapshot interval is explicit: a consumer polling
faster than ``presence.snapshot_interval_s`` receives the same snapshot
flagged ``fresh: false``, never a new computation.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.verbs import dispatch_verb
from arbora.api.ops import presence as presence_ops
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Registry, Surface
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.serving import MCPProjection
from arbora.observability.state import SubsystemWindows
from tests.api._engine_double import fresh_config
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import setup_services

pytestmark = pytest.mark.spec("ARBORA-OPS-R915")

caller = make_verified_caller("fleet:read")


class FakeCityState:
    """A recorded ``CityStatePort`` double: one canned snapshot, no engine."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def snapshot(self, *, tenant: str, context: Any) -> Any:
        self.calls.append({"tenant": tenant, "context": context})
        return self.state

    async def heartbeat(self, **_: Any) -> Any:
        raise AssertionError("this test serves no heartbeat")


def _snapshot(simulated_at: datetime, *, revision: int = 7) -> dict[str, Any]:
    return {
        "tenant": "tenant:one",
        "revision": revision,
        "simulated_at": simulated_at,
        "layout_revision": 3,
        "entities": (
            {
                "id": "entity:one",
                "kind": "ai",
                "x": 1.0,
                "y": 2.0,
                "heading": 0.5,
                "activity": "working",
                "target": "task:one",
                "observed_at": simulated_at,
                "provenance": "run-trace:one",
            },
            {
                "id": "entity:two",
                "kind": "human",
                "x": 3.0,
                "y": 4.0,
                "heading": 1.0,
                "activity": "idle",
                "target": None,
                "observed_at": simulated_at,
                "provenance": "heartbeat:two",
                "expires_at": simulated_at + timedelta(minutes=10),
            },
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


def _services(port: Any | None, config: RuntimeConfig | None = None) -> Any:
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
        raise AssertionError("city presence never dispatches the engine port")

    runtime = BoundOperationRuntime(
        caller_client=caller_client,
        verify_current=verify_current,
        service_client=service_client,
        service_claims=service_claims,
        check_access=check_access,
        eg_dispatch=eg_dispatch,
        service_scopes=frozenset({"example:execute"}),
        service_principal="service:arbora",
        bindings={
            "config": fresh_config(config),
            **({} if port is None else {"city_state": port}),
        },
    )
    services, *_rest = setup_services(
        effects=None,
        registry=Registry(presence_ops.operations()),
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


async def _http(services: Any) -> OpResult | OpError:
    return await invoke(
        "fleet.city.presence", {}, caller(), Surface.HTTP, services=services
    )


async def _mcp(services: Any) -> dict[str, Any]:
    projection = _projection(services)
    return await dispatch_verb("ask", projection, op="fleet.city.presence", params={})


def test_presence_serves_one_snapshot_at_a_bounded_interval() -> None:
    """HTTP and MCP serve the engine snapshot; fast polls share it, stale ages flag it."""
    now = datetime.now(UTC)
    port = FakeCityState(_snapshot(now - timedelta(seconds=1)))
    config = RuntimeConfig()
    # The interval is an explicit setting, and the served value carries it.
    config.presence_snapshot_interval_s = 3600.0
    services = _services(port, config)

    # One hour of headroom keeps the fresh flag independent of invoke latency.
    result = asyncio.run(_http(services))
    assert isinstance(result, OpResult) and result.code == "OK", result
    served = result.value["value"]
    assert served["revision"] == 7
    assert served["layout_revision"] == 3
    assert served["interval_s"] == 3600.0
    assert datetime.fromisoformat(served["computed_at"]) == now - timedelta(seconds=1)
    assert served["fresh"] is True
    assert [e["id"] for e in served["entities"]] == ["entity:one", "entity:two"]
    # The human entity alone carries an expiry.
    assert served["entities"][0]["expires_at"] is None
    assert served["entities"][1]["expires_at"] is not None
    # The read is tenant-scoped through the verified caller, never a param.
    assert port.calls == [{"tenant": "tenant:one", "context": port.calls[0]["context"]}]

    # A fast poll receives the same snapshot, not a new computation.
    again = asyncio.run(_http(services))
    assert isinstance(again, OpResult), again
    assert again.value["value"]["revision"] == served["revision"]
    assert again.value["value"]["computed_at"] == served["computed_at"]
    assert again.value["value"]["entities"] == served["entities"]
    assert again.value["value"]["fresh"] is True

    # A snapshot older than the default interval is served flagged fresh: false.
    stale_port = FakeCityState(_snapshot(now - timedelta(hours=1)))
    stale = asyncio.run(_http(_services(stale_port)))
    assert isinstance(stale, OpResult), stale
    assert stale.value["value"]["interval_s"] == 5.0
    assert stale.value["value"]["fresh"] is False
    assert stale.value["value"]["revision"] == 7

    # The MCP ask verb reaches the same handler through the real pipeline.
    mcp_result = asyncio.run(_mcp(services))
    assert mcp_result["ok"] is True, mcp_result
    assert mcp_result["result"]["value"]["revision"] == served["revision"]
    assert mcp_result["result"]["value"]["fresh"] is True


def test_presence_refuses_unbound_and_foreign_tenant() -> None:
    """An uncomposed port is a typed UNAVAILABLE; a foreign tenant is refused."""
    refused = asyncio.run(_http(_services(None)))
    assert isinstance(refused, OpError) and refused.code == "UNAVAILABLE"
    assert refused.details == {"reason": "city state engine unbound"}

    foreign = FakeCityState(_snapshot(datetime.now(UTC), revision=9))
    foreign.state["tenant"] = "tenant:foreign"
    denied = asyncio.run(_http(_services(foreign)))
    assert isinstance(denied, OpError) and denied.code == "SUBJECT_ACCESS_DENIED"

    mcp = asyncio.run(_mcp(_services(None)))
    assert mcp["ok"] is False
    assert mcp["error"]["code"] == "UNAVAILABLE"
