"""Served proof: pins survive eviction, idle expiry, and restart.

ARBORA-FLEET-R059.9. Drives the served ``manage`` intent tool over the real
MCP registration. The pin store is the in-memory fixture stand-in: the
production bind composes it from the service-authorized engine client, and
the medulla per-principal record kind row keeps the engine-backed L4 proof
in the medulla specs until that row lands.

The harness uses a session cap of 128: with the default cap of 64 no single
70-item load can succeed alongside two pinned items (72 > 64), so a larger
configured cap is what makes the spec's "load 70 more with ``evict=lru``"
scenario reachable; a second 70-item load then forces real LRU eviction
with the pinned tools present, oldest first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from arbora.api.invoke import OpResult, VerifiedCaller
from arbora.api.mcp.registration import (
    RESIDENT_NAMES,
    FleetMCPBinding,
    GovernedSessionVisibility,
    register_mcp_tools,
)
from arbora.api.mcp.verbs import MCPProjection
from arbora.api.ops.fleet import operations
from arbora.api.policy import PolicyGate
from arbora.api.registry import Registry, Surface
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.pin_store import InMemoryPinStore
from arbora.fleet.session_loads import SessionLoads

_TOOL_COUNT = 150


def _caller() -> VerifiedCaller:
    scopes = frozenset({"mcp:discover", "mcp:delegate"})
    return VerifiedCaller(
        principal="alice",
        tenant="tenant",
        effective_scopes=scopes,
        engine_claims={
            "principal": "alice",
            "tenant": "tenant",
            "scopes": list(scopes),
            "policy_version": "p1",
            "delegation": False,
        },
        principal_kind="human",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="p1",
        request_id="request:one",
        session="session-a",
    )


def _make_ops(
    store: InMemoryPinStore,
    now: list[float],
    services: object,
    invoke_fn,
) -> MultiplexerOps:
    rows = tuple(
        CatalogItem(f"fleet:tool:s/t{i}", "tool", f"t{i}", server="s")
        for i in range(_TOOL_COUNT)
    )

    async def source():
        return rows

    async def allowed(_item, _caller):
        return True

    async def mount(item, call):
        return None

    async def notify(key):
        return True

    async def invoke_op(op, params, current, surface_arg):
        return await invoke_fn(
            op, params, current, Surface(surface_arg), services=services
        )

    return MultiplexerOps(
        catalog=FleetCatalog([source], allowed),
        sessions=SessionLoads(
            cap=128, pinned_cap=32, idle_ttl_seconds=3600, clock=lambda: now[0]
        ),
        loadable=allowed,
        callable_item=allowed,
        mount=mount,
        notify=notify,
        invoke=invoke_op,
        health=lambda: {},
        session_key_for=lambda current: f"{current.tenant}:{current.principal}",
        pin_store=store,
        coverage_registry=lambda: (),
        coverage_endpoints=lambda: (),
        unadmitted_servers=lambda: [],
    )


@pytest.fixture
def surface() -> SimpleNamespace:
    now = [0.0]
    store = InMemoryPinStore()
    state = SimpleNamespace(now=now, store=store, calls=[], mcp=FastMCP("served"))
    state.services = SimpleNamespace()
    state.caller_for_request = lambda: _caller()

    routes = {
        "fleet.catalog.search": "search",
        "fleet.tools.load": "load",
        "fleet.tools.unload": "unload",
        "fleet.tools.pin": "pin",
        "fleet.tools.unpin": "unpin",
        "fleet.pins.list": "pins_list",
        "fleet.status": "status",
    }

    async def invoke(op, params, current, surface_arg, *, services, **kwargs):
        assert services is state.services
        assert surface_arg == Surface.MCP
        state.calls.append(op)
        method = routes.get(op)
        if method is None:
            raise AssertionError(f"unexpected served op: {op}")
        return OpResult(await getattr(state.ops, method)(current, **params))

    state.invoke = invoke
    state.ops = _make_ops(store, now, state.services, invoke)
    state.projection = MCPProjection(
        registry=Registry(operations()),
        services=state.services,
        resolver=SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
        caller_for_request=state.caller_for_request,
        policy_gate=PolicyGate(mode="none"),
        invoke=invoke,
    )
    state.binding = FleetMCPBinding(
        state.projection,
        state.ops,
        lambda current: f"{current.tenant}:{current.principal}",
    )
    return state


def _rebuild(surface: SimpleNamespace) -> None:
    """Recompose the multiplexer: new session state, same durable store."""
    surface.ops = _make_ops(
        surface.store, surface.now, surface.services, surface.invoke
    )
    surface.binding = FleetMCPBinding(
        surface.projection, surface.ops, surface.binding.session_key_for
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059", "ARBORA-FLEET-R059.9")
async def test_pin_survives_restart_and_eviction(surface: SimpleNamespace) -> None:
    now = surface.now
    key = "tenant:alice"
    pinned = ["fleet:tool:s/t0", "fleet:tool:s/t1"]
    await register_mcp_tools(surface.mcp, SimpleNamespace(), surface.binding)
    manage = await surface.mcp.get_tool("manage")

    # pin two tools through the served manage intent tool
    result = await manage.run({"op": "fleet.tools.pin", "params": {"items": pinned}})
    assert result.structured_content["ok"] is True
    assert surface.ops.sessions.pinned_items(key) == set(pinned)

    # load 70 more with evict=lru, one at a time so each item gets its own
    # timestamp and LRU is time-ordered (a batched load marks every item with
    # one clock reading)
    for i in range(2, 72):
        now[0] += 0.01
        result = await manage.run(
            {
                "op": "fleet.tools.load",
                "params": {"items": [f"fleet:tool:s/t{i}"], "evict": "lru"},
            }
        )
        assert result.structured_content["ok"] is True
    assert len(surface.ops.sessions.loaded(key)) == 72
    assert set(pinned) <= surface.ops.sessions.loaded(key)

    # a second 70-item load overflows the session: LRU evicts the oldest
    # non-pinned items (t2..t15), never the pinned tools (the oldest loads)
    now[0] += 1.0
    result = await manage.run(
        {
            "op": "fleet.tools.load",
            "params": {
                "items": [f"fleet:tool:s/t{i}" for i in range(72, 142)],
                "evict": "lru",
            },
        }
    )
    assert result.structured_content["ok"] is True
    loaded = surface.ops.sessions.loaded(key)
    assert len(loaded) == 128
    assert set(pinned) <= loaded
    assert not {f"fleet:tool:s/t{i}" for i in range(2, 16)} & loaded
    assert {f"fleet:tool:s/t{i}" for i in range(72, 142)} <= loaded

    # past the idle TTL the session expires: only the pins remain
    now[0] += 3600.0 + 1.0
    assert surface.ops.sessions.loaded(key) == set(pinned)

    # the multiplexer is rebuilt to model a restart
    _rebuild(surface)
    assert surface.ops.sessions.loaded(key) == frozenset()

    # the next request restores the pins through the governed visibility
    middleware = GovernedSessionVisibility(surface.binding)

    async def listed(_context):
        return [SimpleNamespace(name=name) for name in RESIDENT_NAMES]

    await middleware.on_list_tools(None, listed)
    assert surface.ops.sessions.loaded(key) == set(pinned)
    assert surface.ops.sessions.pinned_items(key) == set(pinned)

    # find flags both and status lists them
    find = await surface.mcp.get_tool("find_tools")
    result = await find.run({"browse": True})
    flagged = {row["id"]: row for row in result.structured_content["result"]["items"]}
    for item_id in pinned:
        assert flagged[item_id]["pinned"] is True
    # fleet.status is the served ask op, not a manage op
    ask = await surface.mcp.get_tool("ask")
    result = await ask.run({"op": "fleet.status", "params": {}})
    session = result.structured_content["result"]["session"]
    assert session["pinned"] == sorted(pinned)
    assert session["pinned_used"] == 2
    assert session["pinned_cap"] == 32
    assert session["used"] == 2

    # unpin removes one
    result = await manage.run(
        {"op": "fleet.tools.unpin", "params": {"items": [pinned[0]]}}
    )
    assert result.structured_content["ok"] is True
    assert surface.ops.sessions.pinned_items(key) == {pinned[1]}
    records = await surface.store.list("tenant", "alice")
    assert [record.item_id for record in records] == [pinned[1]]
    # the unpinned item is still loaded until eviction takes it
    assert surface.ops.sessions.loaded(key) == set(pinned)
