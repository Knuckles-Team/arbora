"""Shared harness: served fleet pin ops through the real invoke pipeline.

The authority, refusal and admin tests for ARBORA-FLEET-R059 all drive the
served manage operations through the same synthetic authorities: a bound
gateway, a fixture child transport, and a controllable load policy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from arbora.api.invoke import InvokeServices, VerifiedCaller
from arbora.api.invoke.audit import EffectReservation
from arbora.api.ops.fleet import handle_fleet_operation, operations
from arbora.api.policy import PolicyGate
from arbora.api.registry import Registry, Surface
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.gateway_ops import FleetGateway, tool_for_multiplexer_ops
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.pin_store import InMemoryPinStore
from arbora.fleet.session_loads import SessionLoads

_ROWS = (
    CatalogItem("fleet:tool:s/read", "tool", "read", server="s"),
    CatalogItem(
        "fleet:tool:s/secret",
        "tool",
        "secret",
        server="s",
        required_scopes=frozenset({"data:read"}),
    ),
)


def caller_for(
    scopes: tuple[str, ...],
    principal: str = "alice",
    tenant: str = "tenant",
) -> VerifiedCaller:
    """A minted verified caller; the default scopes see ``read`` only."""
    return VerifiedCaller(
        principal=principal,
        tenant=tenant,
        effective_scopes=frozenset(scopes),
        engine_claims={
            "principal": principal,
            "tenant": tenant,
            "scopes": list(scopes),
            "policy_version": "p1",
            "delegation": False,
        },
        principal_kind="human",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="p1",
        request_id=f"request:{tenant}:{principal}",
        session="session-a",
    )


async def harness(
    pinned_cap: int = 32,
    pin_store: InMemoryPinStore | None | Any = "default",
) -> SimpleNamespace:
    """The served pin operations bound to synthetic, fail-closed authority.

    ``state`` carries the load-policy switches (``policy_ok``/
    ``policy_raising``), the recorded child calls, the pin store and the
    three callers: ``alice`` (may load both items), ``bob`` (item scope
    missing) and ``carol`` (no delegate scope).
    """
    state = SimpleNamespace(
        policy_ok=True,
        policy_raising=False,
        delegated_calls=[],
        dispatches=[],
    )
    state.store = InMemoryPinStore() if pin_store == "default" else pin_store

    async def source():
        return _ROWS

    async def visible(item, caller):
        # item scopes filter discovery: a hidden item is a missing item
        return item.required_scopes <= caller.effective_scopes

    async def loadable(item, caller):
        # the load policy: denies, or stays unreachable
        if state.policy_raising:
            raise RuntimeError("policy decision unavailable")
        return state.policy_ok

    async def policy(_server, _tool, _caller):
        # the call policy: the same decision, keyed by server and tool
        return await loadable(None, _caller)

    async def mount(item, call):
        return None

    async def notify(key):
        return True

    async def invoke_op(op, params, current, surface):
        from arbora.api.invoke import invoke

        return await invoke(
            op, params, current, Surface(surface), services=state.services
        )

    async def delegated(server, tool, arguments, caller):
        state.delegated_calls.append((server, tool, dict(arguments), caller))
        return {"content": []}

    ops = MultiplexerOps(
        catalog=FleetCatalog([source], visible),
        sessions=SessionLoads(pinned_cap=pinned_cap),
        loadable=loadable,
        callable_item=loadable,
        mount=mount,
        notify=notify,
        invoke=invoke_op,
        health=lambda: {},
        session_key_for=lambda caller: f"{caller.tenant}:{caller.principal}",
        pin_store=state.store,
        coverage_registry=lambda: (),
        coverage_endpoints=lambda: (),
        unadmitted_servers=lambda: [],
    )
    gateway = FleetGateway(
        tool_for=tool_for_multiplexer_ops(ops),
        policy_check=policy,
        delegated_call=delegated,
        catalog_ops=ops,
    )

    async def verify_current(supplied):
        return supplied

    @asynccontextmanager
    async def as_caller(supplied):
        yield object()

    @asynccontextmanager
    async def as_service(tenant, *, required_scopes):
        yield object()

    async def check_subject_access(supplied, subject):
        return True

    async def dispatch(op, params, context):
        state.dispatches.append(op.id)
        return await handle_fleet_operation(context, params, op)

    runtime = SimpleNamespace(
        service_scopes=frozenset(),
        bindings={"fleet_gateway": gateway},
        verify_current=verify_current,
        as_caller=as_caller,
        as_service=as_service,
        check_subject_access=check_subject_access,
        dispatch=dispatch,
    )

    state.ops = ops
    state.gateway = gateway
    state.services = InvokeServices(
        registry=Registry(operations()),
        runtime=runtime,
        plans=SimpleNamespace(
            issue=AsyncMock(), validate=AsyncMock(), consume=AsyncMock()
        ),
        policy_gate=PolicyGate(mode="none"),
        audit_preflight=AsyncMock(return_value="audit:one"),
        audit_write=AsyncMock(),
        effects=SimpleNamespace(
            reserve=AsyncMock(
                return_value=EffectReservation("reservation:one", "acquired")
            ),
            complete=AsyncMock(),
        ),
        # the governed pin and unpin writes are journal-owned, like
        # fleet.call: one reservation per idempotency key
        external_effect_operations=frozenset({"fleet.tools.pin", "fleet.tools.unpin"}),
        fleet_effect=gateway.effect,
    )
    state.alice = caller_for(("mcp:discover", "mcp:delegate", "data:read"))
    state.bob = caller_for(("mcp:discover", "mcp:delegate"), principal="bob")
    state.carol = caller_for(("mcp:discover",), principal="carol")
    return state


async def pin(state: SimpleNamespace, caller: Any, items: tuple[str, ...]) -> Any:
    """Drive one served ``fleet.tools.pin`` through the real pipeline.

    The MCP manage tool's schema exposes ``idempotency_key``; the client
    supplies it for every write, so the journal dedupes one request per key.
    """
    import secrets

    from arbora.api.invoke import invoke

    return await invoke(
        "fleet.tools.pin",
        {"items": items},
        caller,
        Surface.MCP,
        services=state.services,
        idempotency_key="pin:" + secrets.token_hex(8),
    )
