"""Revisioned durable city state: one snapshot, subject-bound heartbeat.

Round-trips ``fleet.city.presence`` and ``fleet.city.presence.heartbeat``
through the real invocation chokepoint (ARBORA-OPS-R915.1): refresh-equivalent
reads share a revision, two viewers receive the same snapshot, the heartbeat
is subject-bound to the verified caller principal and fenced by
``expected_revision``, a stale or cross-subject write refuses, the anonymous
sandbox cannot write, and no physical-location field is ever accepted.
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

pytestmark = pytest.mark.spec("ARBORA-OPS-R915.1")

viewer = make_verified_caller("fleet:read")
writer = make_verified_caller("scene:write")


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


class FakeCityState:
    """A recorded ``CityStatePort`` double with an engine-side CAS."""

    def __init__(
        self, *, revision: int = 7, subjects: tuple[str, ...] = ("person:one",)
    ) -> None:
        self.revision = revision
        self.subjects = set(subjects)
        self.positions: dict[str, dict[str, float]] = {}
        self.calls: list[dict[str, Any]] = []
        self.updates = 0
        # One durable simulated_at per revision, as the engine advances it.
        self.simulated_at = datetime.now(UTC) - timedelta(seconds=1)

    async def snapshot(self, *, tenant: str, context: Any) -> Any:
        self.calls.append({"method": "snapshot", "tenant": tenant})
        simulated_at = self.simulated_at
        entities = tuple(
            {
                "id": f"entity:{subject}",
                "kind": "human",
                "x": self.positions.get(subject, {"x": 0.0, "y": 0.0})["x"],
                "y": self.positions.get(subject, {"x": 0.0, "y": 0.0})["y"],
                "heading": 0.0,
                "activity": "idle",
                "target": None,
                "observed_at": simulated_at,
                "provenance": f"heartbeat:{subject}",
                "expires_at": simulated_at + timedelta(minutes=10),
            }
            for subject in sorted(self.subjects)
        )
        return {
            "tenant": tenant,
            "revision": self.revision,
            "simulated_at": simulated_at,
            "layout_revision": 3,
            "entities": entities,
        }

    async def heartbeat(
        self,
        *,
        tenant: str,
        subject: str,
        expected_revision: int,
        x: float,
        y: float,
        heading: float,
        context: Any,
    ) -> Any:
        self.calls.append(
            {
                "method": "heartbeat",
                "tenant": tenant,
                "subject": subject,
                "expected_revision": expected_revision,
            }
        )
        if subject not in self.subjects:
            raise _refused("SUBJECT_ACCESS_DENIED", {"subject": subject})
        if expected_revision != self.revision:
            raise _refused(
                "REVISION_CONFLICT", {"expected_revision": expected_revision}
            )
        self.updates += 1
        self.revision += 1
        self.simulated_at = datetime.now(UTC) - timedelta(seconds=1)
        self.positions[subject] = {"x": x, "y": y, "heading": heading}
        return {
            "tenant": tenant,
            "subject": subject,
            "revision": self.revision,
            "accepted_at": datetime.now(UTC),
        }


def _refused(code: str, details: dict[str, Any]) -> Any:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused(code, details)


def _services(port: Any | None, config: RuntimeConfig | None = None) -> Any:
    """One invoke composition whose composite dispatch reaches the real handlers."""
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
        raise AssertionError("city state never dispatches the engine port")

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
            "heartbeat_fence": presence_ops.HeartbeatFence(),
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
        lambda: viewer(),
        PolicyGate("none"),
        invoke,
        windows=SubsystemWindows(),
    )


def test_snapshot_is_revisioned_tenant_scoped_and_heartbeat_is_subject_bound() -> None:
    """HTTP and MCP serve the revisioned snapshot; the heartbeat is subject-bound."""
    # person:three is admitted by the city state but never updated it, so its
    # stale write reaches the compare-and-set.
    port = FakeCityState(subjects=("person:one", "person:three"))
    services = _services(port)

    # Refresh-equivalent reads share a revision.
    first = asyncio.run(
        invoke("fleet.city.presence", {}, viewer(), Surface.HTTP, services=services)
    )
    assert isinstance(first, OpResult) and first.code == "OK", first
    second = asyncio.run(
        invoke("fleet.city.presence", {}, viewer(), Surface.HTTP, services=services)
    )
    assert isinstance(second, OpResult), second
    assert second.value["value"]["revision"] == first.value["value"]["revision"]
    assert second.value["value"]["layout_revision"] == 3
    assert second.value["value"]["simulated_at"] == first.value["value"]["simulated_at"]
    # The snapshot carries the engine revision, not a viewer-local counter.
    assert first.value["value"]["revision"] == port.revision

    # Two viewers of the same tenant receive the same snapshot.
    other_viewer = viewer(principal="person:two")
    theirs = asyncio.run(
        invoke("fleet.city.presence", {}, other_viewer, Surface.HTTP, services=services)
    )
    assert isinstance(theirs, OpResult), theirs
    assert theirs.value["value"]["revision"] == first.value["value"]["revision"]
    assert theirs.value["value"]["entities"] == first.value["value"]["entities"]
    # Every read is tenant-scoped through the verified caller.
    assert [c["tenant"] for c in port.calls if c["method"] == "snapshot"] == [
        "tenant:one",
    ] * 3

    # The heartbeat is subject-bound to the verified caller principal.
    heartbeat = {
        "expected_revision": port.revision,
        "x": 10.0,
        "y": 20.0,
        "heading": 1.57,
    }
    moved = asyncio.run(
        invoke(
            "fleet.city.presence.heartbeat",
            dict(heartbeat),
            writer(),
            Surface.HTTP,
            services=services,
        )
    )
    assert isinstance(moved, OpResult) and moved.code == "OK", moved
    value = moved.value
    assert value["value"]["subject"] == "person:one"
    assert value["value"]["revision"] == port.revision
    assert value["receipt"], "the write echoes its audit receipt"
    # The port saw the verified principal, and only one engine update happened.
    hb_calls = [c for c in port.calls if c["method"] == "heartbeat"]
    assert hb_calls == [
        {
            "method": "heartbeat",
            "tenant": "tenant:one",
            "subject": "person:one",
            "expected_revision": heartbeat["expected_revision"],
        }
    ]
    assert port.updates == 1

    # A replay of the accepted (subject, revision) is idempotent: same
    # outcome, no second engine update.
    replay = asyncio.run(
        invoke(
            "fleet.city.presence.heartbeat",
            dict(heartbeat),
            writer(),
            Surface.HTTP,
            services=services,
        )
    )
    assert isinstance(replay, OpResult), replay
    assert replay.value["value"] == value["value"]
    assert port.updates == 1

    # A new update inside the five-second window is rate-limited.
    throttled = asyncio.run(
        invoke(
            "fleet.city.presence.heartbeat",
            {"expected_revision": port.revision, "x": 1.0, "y": 2.0, "heading": 0.0},
            writer(),
            Surface.HTTP,
            services=services,
        )
    )
    assert isinstance(throttled, OpError) and throttled.code == "RATE_LIMITED"
    assert throttled.details == {
        "subject": "person:one",
        "window_s": presence_ops.HEARTBEAT_WINDOW_S,
    }
    assert port.updates == 1

    # A stale revision from a fresh subject refuses REVISION_CONFLICT.
    stranger = writer(principal="person:three")
    stale = asyncio.run(
        invoke(
            "fleet.city.presence.heartbeat",
            {"expected_revision": 1, "x": 1.0, "y": 2.0, "heading": 0.0},
            stranger,
            Surface.HTTP,
            services=services,
        )
    )
    assert isinstance(stale, OpError) and stale.code == "REVISION_CONFLICT"
    assert port.updates == 1

    # A subject the city state does not admit is refused.
    unknown = writer(principal="person:unknown")
    denied = asyncio.run(
        invoke(
            "fleet.city.presence.heartbeat",
            {"expected_revision": port.revision, "x": 1.0, "y": 2.0, "heading": 0.0},
            unknown,
            Surface.HTTP,
            services=services,
        )
    )
    assert isinstance(denied, OpError) and denied.code == "SUBJECT_ACCESS_DENIED"
    assert port.updates == 1


def test_heartbeat_refuses_foreign_fields_and_unverified_callers() -> None:
    """No physical-location or foreign-subject field is ever accepted."""
    port = FakeCityState()
    services = _services(port)
    base = {"expected_revision": port.revision, "x": 1.0, "y": 2.0, "heading": 0.0}

    for extra in ("subject", "actor_id", "physical_location", "tenant"):
        refused = asyncio.run(
            invoke(
                "fleet.city.presence.heartbeat",
                {
                    **base,
                    extra: "person:two" if extra in ("subject", "actor_id") else "x",
                },
                writer(),
                Surface.HTTP,
                services=services,
            )
        )
        assert isinstance(refused, OpError) and refused.code == "INVALID_ARGUMENT", (
            extra,
            refused,
        )
    assert port.calls == []

    # The anonymous sandbox (unverified, non-human) is refused before the port.
    sandbox = writer(
        principal="anonymous",
        principal_kind="robot",
        authenticated=False,
        delegated=True,
    )
    for code_caller in (
        sandbox,
        writer(principal_kind="robot"),
        writer(delegated=True),
    ):
        refused = asyncio.run(
            invoke(
                "fleet.city.presence.heartbeat",
                dict(base),
                code_caller,
                Surface.HTTP,
                services=services,
            )
        )
        assert isinstance(refused, OpError), refused
        assert refused.code in {"UNAUTHENTICATED", "PRINCIPAL_NOT_ALLOWED"}
    assert port.calls == []


def test_heartbeat_is_served_over_mcp_and_refuses_unbound() -> None:
    """The MCP write verb reaches the same handler; an unbound port is UNAVAILABLE."""
    port = FakeCityState()
    services = _services(port)
    projection = _projection(services)
    # The write verb carries the caller through the same admission path.
    projection = MCPProjection(
        services.registry,
        services,
        IntentResolver(),
        lambda: writer(),
        PolicyGate("none"),
        invoke,
        windows=SubsystemWindows(),
    )
    result = asyncio.run(
        dispatch_verb(
            "write",
            projection,
            op="fleet.city.presence.heartbeat",
            params={
                "expected_revision": port.revision,
                "x": 5.0,
                "y": 6.0,
                "heading": 0.79,
            },
        )
    )
    assert result["ok"] is True, result
    assert result["result"]["value"]["subject"] == "person:one"
    assert result["result"]["value"]["revision"] == port.revision
    assert port.updates == 1

    unbound = asyncio.run(
        invoke(
            "fleet.city.presence.heartbeat",
            {"expected_revision": 0, "x": 1.0, "y": 2.0, "heading": 0.0},
            writer(),
            Surface.HTTP,
            services=_services(None),
        )
    )
    assert isinstance(unbound, OpError) and unbound.code == "UNAVAILABLE"
    assert unbound.details == {"reason": "city state engine unbound"}
