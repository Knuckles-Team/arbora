"""Served city activity: run-trace facts, cursor-paged, never invented.

Round-trips ``fleet.city.activity`` through the real invocation chokepoint
(ARBORA-OPS-R916): the typed ``ActivityPort`` is the only path to the
engine's run-trace step records, each entry carries the run, the step, the
request/argument/result digests and the trace provenance, an event the
engine has not recorded is not served, a caller without run read access
gets a subject-read refusal, a malformed cursor refuses
``INVALID_ARGUMENT`` before any engine read, and an uncomposed port fails
closed with a typed ``UNAVAILABLE``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.serving import MCPProjection
from arbora.api.mcp.verbs import dispatch_verb
from arbora.api.ops import activity as activity_ops
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Registry, Surface
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import setup_services

pytestmark = pytest.mark.spec("ARBORA-OPS-R916")

caller = make_verified_caller("fleet:read")

PAGE_ONE = (
    {
        "run": "run:one",
        "step": "step:1",
        "request_digest": "digest:req:1",
        "argument_digest": "digest:arg:1",
        "result_digest": "digest:res:1",
        "provenance": "trace:one",
        "observed_at": datetime(2026, 11, 21, 12, 0, 1, tzinfo=UTC),
    },
    {
        "run": "run:one",
        "step": "step:2",
        "request_digest": "digest:req:2",
        "argument_digest": "digest:arg:2",
        "result_digest": "digest:res:2",
        "provenance": "trace:one",
        "observed_at": datetime(2026, 11, 21, 12, 0, 2, tzinfo=UTC),
    },
)
PAGE_TWO = (
    {
        "run": "run:two",
        "step": "step:1",
        "request_digest": "digest:req:3",
        "argument_digest": "digest:arg:3",
        "result_digest": "digest:res:3",
        "provenance": "trace:two",
        "observed_at": datetime(2026, 11, 21, 12, 1, 0, tzinfo=UTC),
    },
)


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


class FakeActivityPort:
    """A recorded ``ActivityPort`` double: recorded pages only, no invention."""

    def __init__(self, *, subject_denied: bool = False) -> None:
        self.subject_denied = subject_denied
        self.calls: list[dict[str, Any]] = []

    async def steps(
        self, *, tenant: str, cursor: str | None, limit: int, context: Any
    ) -> Any:
        self.calls.append({"tenant": tenant, "cursor": cursor, "limit": limit})
        if self.subject_denied:
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused("SUBJECT_ACCESS_DENIED", {"subject": tenant})
        if cursor is None:
            return {
                "tenant": tenant,
                "entries": PAGE_ONE,
                "next_cursor": "cursor.page.two",
            }
        if cursor == "cursor.page.two":
            return {"tenant": tenant, "entries": PAGE_TWO, "next_cursor": None}
        raise AssertionError(f"unexpected cursor {cursor!r}")


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
        raise AssertionError("city activity never dispatches the engine port")

    runtime = BoundOperationRuntime(
        caller_client=caller_client,
        verify_current=verify_current,
        service_client=service_client,
        service_claims=service_claims,
        check_access=check_access,
        eg_dispatch=eg_dispatch,
        service_scopes=frozenset({"example:execute"}),
        service_principal="service:arbora",
        bindings={} if port is None else {"city_activity": port},
    )
    services, *_rest = setup_services(
        effects=None,
        registry=Registry(activity_ops.operations()),
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
    )


def test_activity_serves_run_trace_facts_with_cursor_and_refuses_unrecorded() -> None:
    """HTTP and MCP serve the recorded step facts; nothing is invented."""
    port = FakeActivityPort()
    services = _services(port)

    first = asyncio.run(
        invoke("fleet.city.activity", {}, caller(), Surface.HTTP, services=services)
    )
    assert isinstance(first, OpResult) and first.code == "OK", first
    served = first.value["value"]
    assert served["next_cursor"] == "cursor.page.two"
    assert [e["step"] for e in served["entries"]] == ["step:1", "step:2"]
    entry = served["entries"][0]
    assert entry["run"] == "run:one"
    assert entry["request_digest"] == "digest:req:1"
    assert entry["argument_digest"] == "digest:arg:1"
    assert entry["result_digest"] == "digest:res:1"
    assert entry["provenance"] == "trace:one"
    # The read is tenant-scoped through the verified caller, with a bounded limit.
    assert port.calls == [{"tenant": "tenant:one", "cursor": None, "limit": 50}]

    # The cursor resumes the stream; the final page ends with no cursor.
    second = asyncio.run(
        invoke(
            "fleet.city.activity",
            {"cursor": "cursor.page.two", "limit": 100},
            caller(),
            Surface.HTTP,
            services=services,
        )
    )
    assert isinstance(second, OpResult), second
    assert second.value["value"]["next_cursor"] is None
    assert second.value["value"]["entries"][0]["run"] == "run:two"
    assert port.calls[1] == {
        "tenant": "tenant:one",
        "cursor": "cursor.page.two",
        "limit": 100,
    }

    # The MCP ask verb reaches the same handler through the real pipeline.
    mcp = asyncio.run(
        dispatch_verb("ask", _projection(services), op="fleet.city.activity", params={})
    )
    assert mcp["ok"] is True, mcp
    assert mcp["result"]["value"]["next_cursor"] == "cursor.page.two"

    # An event the engine has not recorded is not served: an empty page
    # serves no entries, and arbora invents none.
    empty = FakeActivityPort()

    async def empty_steps(**kwargs: Any) -> Any:
        return {"tenant": kwargs["tenant"], "entries": (), "next_cursor": None}

    empty.steps = empty_steps  # type: ignore[method-assign]
    nothing = asyncio.run(
        invoke(
            "fleet.city.activity", {}, caller(), Surface.HTTP, services=_services(empty)
        )
    )
    assert isinstance(nothing, OpResult), nothing
    assert nothing.value["value"]["entries"] == []
    assert nothing.value["value"]["next_cursor"] is None

    # A caller without run read access on the tenant's runs is refused.
    denied = asyncio.run(
        invoke(
            "fleet.city.activity",
            {},
            caller(),
            Surface.HTTP,
            services=_services(FakeActivityPort(subject_denied=True)),
        )
    )
    assert isinstance(denied, OpError) and denied.code == "SUBJECT_ACCESS_DENIED"

    # A malformed cursor refuses before any engine read.
    malformed = asyncio.run(
        invoke(
            "fleet.city.activity",
            {"cursor": "not a cursor; with spaces"},
            caller(),
            Surface.HTTP,
            services=_services(port),
        )
    )
    assert isinstance(malformed, OpError) and malformed.code == "INVALID_ARGUMENT"
    assert malformed.details == {"field": "cursor"}
    assert len(port.calls) == 3  # unchanged by the malformed read

    # An uncomposed port fails closed.
    unbound = asyncio.run(
        invoke(
            "fleet.city.activity", {}, caller(), Surface.HTTP, services=_services(None)
        )
    )
    assert isinstance(unbound, OpError) and unbound.code == "UNAVAILABLE"
    assert unbound.details == {"reason": "run trace engine unbound"}
