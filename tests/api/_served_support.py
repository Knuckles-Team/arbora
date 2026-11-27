"""Shared harness for served-operation tests with a recording engine.

The ``served`` fixture of ``tests/test_served_operation_api.py`` composes the
real invocation; this module captures it, routes named engine methods to a
recording handler, and lets a test choose which verified person calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastmcp import FastMCP

from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import runtime as mcp_runtime
from arbora.mcp_server.host_caches import build_host_caches
from arbora.observability.state import SubsystemWindows
from arbora.security import request_identity
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.key_cache import JwksCache
from tests._support import bound_authority
from tests.test_served_operation_api import SCOPES

TENANT = "tenant:one"


def person(
    name: str, tenant: str = TENANT, extra_scopes: tuple[str, ...] = ()
) -> ActorContext:
    return ActorContext(
        actor_id=f"person:{name}",
        actor_type=ActorType.HUMAN,
        tenant_id=tenant,
        authenticated=True,
        roles=(*SCOPES, *extra_scopes),
        credential_expires_at=int(time.time()) + 3600,
    )


def capture_invocation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the invocation the ``served`` fixture composes (request it first)."""
    import tests.test_served_operation_api as harness

    captured: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def capturing(**kwargs: Any) -> Any:
        captured["invocation"] = compose(**kwargs)
        return captured["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", capturing)
    return captured


def confirmable_invocation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the served invocation with in-memory plan leases and effect journal.

    The plan and the confirmed-replay owner are engine-backed in production;
    the double keeps the real invoke pipeline and replaces only those two.
    """
    import dataclasses
    from types import SimpleNamespace

    import tests.test_served_operation_api as harness
    from arbora.api.invoke.plan import EgPlanStore
    from tests.api.test_invoke_contract import FixtureJournal, FixtureLeases

    captured: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def capturing(**kwargs: Any) -> Any:
        invocation = compose(**kwargs)
        leases, journal = FixtureLeases(), FixtureJournal()
        services = dataclasses.replace(
            invocation.services,
            plans=EgPlanStore(
                SimpleNamespace(control_leases=leases),
                lease_kind="fixture.plan",
                seal_key=b"x" * 32,
            ),
            effects=journal,
        )
        captured["invocation"] = dataclasses.replace(invocation, services=services)
        captured["journal"] = journal
        return captured["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", capturing)
    return captured


def route_methods(
    client: Any,
    handlers: dict[str, Callable[[dict[str, Any], str], Any]],
    monkeypatch: pytest.MonkeyPatch,
    people: dict[str, ActorContext],
) -> None:
    """Send ``handlers``' methods to a recording engine as the verified person.

    ``people`` maps a bearer credential to the person it verifies as. The
    handler receives the wire params and that person's principal id.
    """
    import medulla.generated as generated
    from medulla.generated import _runtime

    # The pinned engine release predates these methods; declare them for the test.
    declared = frozenset(generated.METHOD_IDS) | frozenset(handlers)
    monkeypatch.setattr(generated, "METHOD_IDS", declared)
    monkeypatch.setattr(_runtime, "METHOD_IDS", declared)
    original = client._send
    current: dict[str, str] = {"principal": "person:one"}

    async def credential_owner(credential: str, **_kw) -> ActorContext:
        actor = people[credential]
        current["principal"] = actor.actor_id
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)

    async def send(method: str, params: Any, graph: str | None, **kw: Any) -> Any:
        if method in handlers:
            return handlers[method](params, current["principal"])
        return await original(method, params, graph, **kw)

    client._send = send


def serve_verbs(mcp: Any, invocation: Any, authority: Any) -> Any:
    """Compose the served surface and attach the verbs the way startup does.

    Startup attaches them through ``register_mcp_tools`` once the fleet is
    bound; these harnesses have no fleet, so they attach the same verbs only.
    """
    from arbora.api.mcp.verbs import VERBS, make_verb

    projection = mcp_runtime._register_served_intent_surface(
        mcp, invocation, authority, SubsystemWindows()
    )
    for name in VERBS:
        mcp.add_tool(make_verb(name, projection))
    return projection


class McpAsk:
    """The served MCP intent surface over the captured invocation."""

    def __init__(
        self,
        invocation: Any,
        monkeypatch: pytest.MonkeyPatch,
        caller: ActorContext | None = None,
    ) -> None:
        process = request_identity.mint_graph_session(
            caller or person("one"), RuntimeConfig()
        )
        self.mcp = FastMCP("served-ops")
        serve_verbs(self.mcp, invocation, bound_authority(process))

    def __call__(self, verb: str, op: str, params: dict[str, Any], **extra: Any) -> Any:
        async def call() -> Any:
            result = await self.mcp.call_tool(
                verb, {"op": op, "params": params, **extra}
            )
            if result.structured_content is not None:
                # The text part is a 1000-character summary; the payload is whole.
                return result.structured_content
            text = "".join(getattr(part, "text", "") for part in result.content)
            return json.loads(text)

        return asyncio.run(call())


@contextlib.contextmanager
def nothing() -> Iterator[None]:
    yield


class RecordingEngine:
    """Records every engine method with the verified principal and answers from handlers.

    One fixture for the served tests: ``route`` sends the named methods here, a
    handler may be a canned answer, a callable ``(params, principal)`` or raise an
    ``EngineResponseError``. ``calls`` holds ``(method, params, principal)``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str]] = []

    def record(self, name: str, answer: Any) -> Callable[[Any, str], Any]:
        def handle(params: Any, principal: str) -> Any:
            self.calls.append((name, params, principal))
            return answer(params, principal) if callable(answer) else answer

        return handle

    def route(
        self,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        answers: dict[str, Any],
        people: dict[str, ActorContext],
    ) -> RecordingEngine:
        route_methods(
            client,
            {name: self.record(name, answer) for name, answer in answers.items()},
            monkeypatch,
            people,
        )
        return self

    def names(self) -> list[str]:
        return [name for name, *_ in self.calls]


TENANT_TWO = "tenant:two"
CREDENTIAL_ONE = "fixture-verified"  # sanitizer:ignore - synthetic
CREDENTIAL_TWO = "second-tenant-verified"  # sanitizer:ignore - synthetic


@pytest.fixture
def confirmable(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The served invocation with in-memory plan leases (request before ``served``)."""
    return confirmable_invocation(monkeypatch)


class TwoTenants:
    """One engine that admits two verified principals of different tenants.

    ``one`` is bound to ``tenant:one`` and ``two`` to ``tenant:two``. Each tenant
    has its own served runtime and graph, as in production, and both reach the
    one engine the handlers declare, so a foreign object is reachable only if the
    door lets it through. A request resolves to the tenant of its own
    credential; no header names the other tenant, and a credential presented to
    the other tenant's runtime is refused at admission.
    """

    def __init__(
        self,
        apps: dict[str, Any],
        invocations: dict[str, Any],
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        scopes: tuple[str, ...],
    ) -> None:
        self.apps = apps
        self.invocations = invocations
        self.client = client
        self.monkeypatch = monkeypatch
        self.credentials = {TENANT: CREDENTIAL_ONE, TENANT_TWO: CREDENTIAL_TWO}
        self.actors = {
            TENANT: person("one", TENANT, scopes),
            TENANT_TWO: person("two", TENANT_TWO, scopes),
        }

    def route(self, handlers: dict[str, Callable[[dict[str, Any], str], Any]]) -> None:
        people = {
            self.credentials[tenant]: actor for tenant, actor in self.actors.items()
        }
        route_methods(self.client, handlers, self.monkeypatch, people)

    def call(
        self,
        tenant: str,
        op_id: str,
        params: dict[str, Any],
        *,
        credential_of: str | None = None,
    ) -> Any:
        """The served HTTP operation of ``tenant``'s runtime.

        ``credential_of`` presents another tenant's credential to this runtime.
        """
        from tests.test_served_operation_api import _call

        bearer = self.credentials[credential_of or tenant]
        return _call(self.apps[tenant], op_id, params, Authorization=f"Bearer {bearer}")

    def mcp(self, tenant: str) -> McpAsk:
        """The served MCP intent surface of ``tenant``'s runtime and principal."""
        return McpAsk(self.invocations[tenant], self.monkeypatch, self.actors[tenant])


@pytest.fixture
def two_tenant_served(
    confirmable: dict[str, Any],
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> TwoTenants:
    """Admit two tenants against one engine (import ``served`` beside it)."""
    import dataclasses
    from types import SimpleNamespace

    from fastapi import FastAPI

    import tests.test_served_operation_api as harness
    from arbora.access.action_policy import ActionPolicies
    from arbora.access.approval import ApprovalDecisions
    from arbora.api.invoke.plan import EgPlanStore
    from arbora.fleet.state import FleetState
    from arbora.gateway.graph_api import register_graph_routes
    from arbora.mcp_server.composition import install_gateway_application
    from arbora.mcp_server.host_context import build_host_context
    from arbora.mcp_server.served_invocation import refused_fleet_effect
    from tests.api.test_invoke_contract import FixtureJournal, FixtureLeases

    http, client = served
    scopes = ("workstream:read", "workstream:write")
    apps: dict[str, Any] = {TENANT: http}
    invocations: dict[str, Any] = {TENANT: confirmable["invocation"]}
    actor = person("two", TENANT_TWO, scopes)
    process = request_identity.mint_graph_session(actor, RuntimeConfig())
    invocation = harness.compose_served_invocation(
        config=RuntimeConfig(),
        keys=JwksCache(RuntimeConfig()),
        pool=harness.FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        action_policies=ActionPolicies(),
        bindings={},
        fleet_effect=refused_fleet_effect,
    )
    leases, journal = FixtureLeases(), FixtureJournal()
    invocation = dataclasses.replace(
        invocation,
        services=dataclasses.replace(
            invocation.services,
            plans=EgPlanStore(
                SimpleNamespace(control_leases=leases),
                lease_kind="fixture.plan",
                seal_key=b"y" * 32,
            ),
            effects=journal,
        ),
    )
    host = build_host_context()
    install_gateway_application(
        invocation,
        gateway=host.gateway,
        fleet=FleetState(),
        approvals=ApprovalDecisions(),
        engine_provider=lambda: None,
        caches=build_host_caches(RuntimeConfig()),
    )
    second = FastAPI()
    register_graph_routes(second, host)
    apps[TENANT_TWO], invocations[TENANT_TWO] = second, invocation
    return TwoTenants(apps, invocations, client, monkeypatch, scopes)
