"""Prepared FastMCP registration exercised with explicit synthetic authorities."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from arbora.api.invoke import OpError, OpResult, VerifiedCaller
from arbora.api.mcp.verbs import MCPProjection
from arbora.api.ops.fleet import operations
from arbora.api.policy import PolicyGate
from arbora.api.registry import Registry, Surface
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.child_mounts import _make_forwarder
from arbora.fleet.multiplexer import make_governed_tool_mount
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.session_loads import SessionLoads
from arbora.mcp_server.registration import (
    RESIDENT_NAMES,
    FleetMCPBinding,
    GovernedSessionVisibility,
    register_mcp_tools,
)
from arbora.observability.state import SubsystemWindows


@pytest.fixture
def surface() -> Any:
    scopes = frozenset({"mcp:discover", "mcp:delegate", "data:read"})
    caller = VerifiedCaller(
        principal="alice",
        tenant="tenant",
        effective_scopes=scopes,
        engine_claims={
            "principal": "alice",
            "tenant": "tenant",
            "scopes": scopes,
            "policy_version": "p1",
            "delegated": False,
        },
        principal_kind="human",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="p1",
        session="session-a",
    )
    state = SimpleNamespace(
        caller=caller,
        allowed=True,
        outcome=None,
        calls=[],
        notifications=[],
        mcp=FastMCP("prepared"),
        mux=SimpleNamespace(),
        services=object(),
    )
    rows = (
        CatalogItem(
            "fleet:tool:s/read",
            "tool",
            "read",
            server="s",
            required_scopes=frozenset({"data:read"}),
            schema={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "additionalProperties": False,
            },
        ),
        CatalogItem("fleet:skill:s/help", "skill", "help", server="s", body="help"),
    )

    async def source():
        return rows

    async def allowed(_item, _caller):
        return state.allowed

    async def notify(key):
        state.notifications.append(key)
        return True

    async def invoke(op, params, current, surface, *, services, **kwargs):
        assert services is state.services
        assert surface == Surface.MCP
        state.calls.append((op, dict(params), current))
        if state.outcome is not None:
            return state.outcome
        routes = {
            "fleet.catalog.search": state.ops.search,
            "fleet.tools.load": state.ops.load,
            "fleet.tools.unload": state.ops.unload,
            "fleet.status": state.ops.status,
        }
        if op in routes:
            return OpResult(await routes[op](current, **params))
        assert op == "fleet.call"
        return OpResult({"child": "called"})

    async def bound_invoke(op, params, current, surface):
        return await invoke(op, params, current, surface, services=state.services)

    def native_name(_item):
        return "s_read"

    state.ops = MultiplexerOps(
        catalog=FleetCatalog([source], allowed),
        sessions=SessionLoads(),
        loadable=allowed,
        callable_item=allowed,
        mount=make_governed_tool_mount(
            state.mcp, state.mux, native_name, RESIDENT_NAMES
        ),
        notify=notify,
        invoke=bound_invoke,
        health=lambda: {},
        session_key_for=lambda current: current.session,
        native_name=native_name,
    )
    state.projection = MCPProjection(
        registry=Registry(operations()),
        services=state.services,
        resolver=SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
        caller_for_request=lambda: state.caller,
        policy_gate=PolicyGate(mode="none"),
        invoke=invoke,
        windows=SubsystemWindows(),
    )
    state.binding = FleetMCPBinding(
        state.projection, state.ops, lambda current: current.session
    )
    state.tool_id = rows[0].id
    state.native_name = "s_read"
    return state


def test_exactly_ten_residents_and_browse_replaces_list_catalog(surface) -> None:
    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        tools = await surface.mcp.list_tools()
        assert {tool.name for tool in tools} == set(RESIDENT_NAMES)
        assert len(tools) == 10
        assert "list_catalog" not in {tool.name for tool in tools}
        tool = next(tool for tool in tools if tool.name == "find_tools")
        result = await tool.run({"browse": True})
        assert {
            row["kind"] for row in result.structured_content["result"]["items"]
        } == {"tool", "skill"}
        assert surface.calls[0][0:2] == ("fleet.catalog.search", {"browse": True})
        with pytest.raises(RuntimeError, match="empty and unbound"):
            await register_mcp_tools(surface.mcp, surface.mux, surface.binding)

    asyncio.run(run())


def test_load_native_and_intent_share_invoke_and_refresh_caller(surface) -> None:
    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        native = await surface.mcp.get_tool(surface.native_name)
        result = await native.run({"x": 3})
        assert result.structured_content["result"] == {"child": "called"}
        assert surface.calls[-1][0:2] == (
            "fleet.call",
            {"server": "s", "tool": "read", "arguments": {"x": 3}},
        )
        surface.caller = replace(surface.caller, request_id="fresh")
        act = await surface.mcp.get_tool("act")
        await act.run({"op": "fleet.call", "params": surface.calls[-1][1]})
        assert surface.calls[-1][2].request_id == "fresh"
        surface.caller = replace(surface.caller, session="session-b")
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await native.run({"x": 3})
        assert len(surface.calls) == 2

    asyncio.run(run())


def test_load_and_call_reauthorize_and_retract_revoked_tool(surface) -> None:
    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        surface.allowed = False
        with pytest.raises(PermissionError):
            await surface.ops.load(surface.caller, items=[surface.tool_id])
        assert not surface.ops.sessions.loaded("session-a")
        surface.allowed = True
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        surface.outcome = OpError("POLICY_DENIED")
        native = await surface.mcp.get_tool(surface.native_name)
        with pytest.raises(ToolError, match="POLICY_DENIED"):
            await native.run({})
        assert not surface.ops.dispatchable("session-a", surface.native_name)
        assert surface.notifications == ["session-a"] * 3
        assert len(surface.calls) == 1

    asyncio.run(run())


def test_scope_revocation_filters_list_and_prevents_stale_native_call(surface) -> None:
    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        surface.caller = replace(surface.caller, effective_scopes=frozenset())
        middleware = GovernedSessionVisibility(surface.binding)

        async def listed(_context):
            return [
                SimpleNamespace(name=name)
                for name in (*RESIDENT_NAMES, surface.native_name)
            ]

        visible = await middleware.on_list_tools(None, listed)
        assert {tool.name for tool in visible} == set(RESIDENT_NAMES)
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await _make_forwarder(
                lambda: getattr(surface.mux, "_governed_fleet", None),
                surface.native_name,
            )()
        assert surface.calls == []

    asyncio.run(run())


def test_missing_governed_binding_never_dispatches_child() -> None:
    async def run():
        async def forbidden(*_args):
            pytest.fail("direct child dispatch bypass")

        mux = SimpleNamespace(call_proxied_tool=forbidden)
        with pytest.raises(ToolError, match="governed_fleet_unavailable"):
            await _make_forwarder(
                lambda: getattr(mux, "_governed_fleet", None), "child"
            )()

    asyncio.run(run())


def test_incomplete_attachment_fails_before_binding(surface, monkeypatch) -> None:
    add = surface.mcp.add_tool
    monkeypatch.setattr(
        surface.mcp, "add_tool", lambda tool: add(tool) if tool.name != "act" else None
    )
    with pytest.raises(RuntimeError, match="attachment was incomplete"):
        asyncio.run(register_mcp_tools(surface.mcp, surface.mux, surface.binding))
    assert not hasattr(surface.mux, "_governed_fleet")


def test_governed_native_preserves_child_annotations_and_meta(surface) -> None:
    from mcp.types import Annotations, CallToolResult, TextContent

    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        child = CallToolResult(
            content=[
                TextContent(
                    type="text", text="result", annotations=Annotations(priority=0.8)
                )
            ],
            structuredContent={"answer": 42},
            _meta={"child_trace": "trace"},
        )
        surface.outcome = OpResult(
            {"value": child.model_dump(mode="json", by_alias=True)}
        )
        result = await _make_forwarder(
            lambda: getattr(surface.mux, "_governed_fleet", None), surface.native_name
        )()
        assert result.content[0].annotations.priority == 0.8
        assert result.structured_content == {"answer": 42}
        assert result.meta == {"child_trace": "trace"}
        assert surface.calls[-1][0] == "fleet.call"

    asyncio.run(run())


def test_native_idle_expiry_and_one_shot_never_bypass_membership(surface) -> None:
    async def run():
        clock = [0.0]
        surface.ops.sessions = SessionLoads(clock=lambda: clock[0])
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        await surface.ops.load(
            surface.caller, items=[surface.tool_id], auto_unload=True
        )
        forward = _make_forwarder(
            lambda: getattr(surface.mux, "_governed_fleet", None), surface.native_name
        )
        await forward()
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await forward()
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        clock[0] = 3600.0
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await forward()
        assert len(surface.calls) == 1

    asyncio.run(run())


def test_all_four_residents_use_the_shared_services(surface) -> None:
    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        for name, params in (
            ("find_tools", {"browse": True}),
            ("load_tools", {"items": [surface.tool_id]}),
            ("multiplexer_status", {}),
            ("unload_tools", {"all_items": True}),
        ):
            tool = await surface.mcp.get_tool(name)
            result = await tool.run(params)
            assert result.structured_content["ok"] is True
        assert [call[0] for call in surface.calls] == [
            "fleet.catalog.search",
            "fleet.tools.load",
            "fleet.status",
            "fleet.tools.unload",
        ]
        assert not surface.ops.sessions.loaded("session-a")

    asyncio.run(run())


def test_current_unauthenticated_caller_cannot_use_previously_loaded_tool(
    surface,
) -> None:
    async def run():
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        surface.caller = None
        with pytest.raises(ToolError, match="UNAUTHENTICATED"):
            await _make_forwarder(
                lambda: getattr(surface.mux, "_governed_fleet", None),
                surface.native_name,
            )()
        assert surface.calls == []

    asyncio.run(run())


async def _reached(entered: asyncio.Event, task: asyncio.Task) -> None:
    """Return once the paused section is entered; event-driven, no wall-clock deadline.

    If the task ends before it enters (an unexpected error), surface that result
    instead of waiting, so the test fails by assertion and never hangs or races a timer.
    """
    waiter = asyncio.ensure_future(entered.wait())
    await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not entered.is_set():
        waiter.cancel()
        task.result()
        raise AssertionError("the call finished without reaching the paused section")


async def _real_native_fixture():
    from mcp.types import CallToolResult, TextContent

    from tests.fleet.catalog_fixture import bind_governed_forwarder_fixture

    return await bind_governed_forwarder_fixture(
        SimpleNamespace(),
        CallToolResult(content=[TextContent(type="text", text="ok")]),
    )


async def _loaded_one_shot_fixture():
    """A real-native fixture with one auto-unload one-shot item already
    loaded, plus the paired events the pause/release tests below drive."""
    state = await _real_native_fixture()
    ops = state.binding.ops
    await ops.load(state.caller, items=["fleet:tool:synthetic/tool"], auto_unload=True)
    entered, release = asyncio.Event(), asyncio.Event()
    return state, ops, entered, release


def test_concurrent_one_shot_acquires_only_one_real_invocation() -> None:
    async def run():
        state, ops, entered, release = await _loaded_one_shot_fixture()
        original = state.services.runtime.dispatch
        attempts = []

        async def paused(op, params, context):
            attempts.append(op.id)
            entered.set()
            await release.wait()
            return await original(op, params, context)

        state.services.runtime.dispatch = paused
        first = asyncio.create_task(state.binding.call_native("synthetic__tool", {}))
        await _reached(entered, first)
        try:
            with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
                await state.binding.call_native("synthetic__tool", {})
        finally:
            release.set()
            await first
        assert attempts == ["fleet.call"]
        assert len(state.child_calls) == 1
        assert not ops.sessions.loaded("fixture:session")

    asyncio.run(run())


def test_unload_during_catalog_lookup_cannot_reach_invoke() -> None:
    async def run():
        state = await _real_native_fixture()
        ops = state.binding.ops
        entered, release = asyncio.Event(), asyncio.Event()
        original = ops.catalog.get
        lookups = 0

        async def paused(*args):
            nonlocal lookups
            lookups += 1
            if lookups == 2:
                entered.set()
                await release.wait()
            return await original(*args)

        ops.catalog.get = paused
        pending = asyncio.create_task(state.binding.call_native("synthetic__tool", {}))
        await _reached(entered, pending)
        await ops.unload(state.caller, items=["fleet:tool:synthetic/tool"])
        release.set()
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await pending
        assert state.dispatches == []
        assert state.child_calls == []

    asyncio.run(run())


def test_loaded_target_change_is_invalidated_instead_of_redirected() -> None:
    async def run():
        state = await _real_native_fixture()
        ops = state.binding.ops

        async def changed():
            return [
                CatalogItem(
                    "fleet:tool:synthetic/tool", "tool", "other", server="elsewhere"
                )
            ]

        ops.catalog._sources = (changed,)
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await state.binding.call_native("synthetic__tool", {})
        assert state.dispatches == []
        assert state.child_calls == []
        assert not ops.sessions.loaded("fixture:session")

    asyncio.run(run())


def test_reload_while_invoke_waits_invalidates_old_grant_not_new_load() -> None:
    async def run():
        state = await _real_native_fixture()
        ops = state.binding.ops
        entered, release = asyncio.Event(), asyncio.Event()
        original = state.services.runtime.dispatch

        async def paused(op, params, context):
            entered.set()
            await release.wait()
            return await original(op, params, context)

        state.services.runtime.dispatch = paused
        pending = asyncio.create_task(state.binding.call_native("synthetic__tool", {}))
        await _reached(entered, pending)
        await ops.unload(state.caller, items=["fleet:tool:synthetic/tool"])
        await ops.load(state.caller, items=["fleet:tool:synthetic/tool"])
        release.set()
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await pending
        assert state.child_calls == []
        assert ops.sessions.loaded("fixture:session") == {"fleet:tool:synthetic/tool"}

    asyncio.run(run())


def test_consumed_one_shot_is_revocable_while_transport_setup_waits() -> None:
    from arbora.fleet.gateway_ops import check_native_transport

    async def run():
        state, ops, entered, release = await _loaded_one_shot_fixture()
        original = state.gateway._delegated_call

        async def paused(server, tool, arguments, caller):
            entered.set()
            await release.wait()
            check_native_transport(server, tool)
            return await original(server, tool, arguments, caller)

        state.gateway._delegated_call = paused
        pending = asyncio.create_task(state.binding.call_native("synthetic__tool", {}))
        await _reached(entered, pending)
        assert not ops.sessions.loaded("fixture:session")
        await ops.unload(state.caller, all_items=True)
        release.set()
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await pending
        assert state.child_calls == []

    asyncio.run(run())


def _projected_tools(surface) -> Any:
    """A host tool table with the surface's served projection bound."""
    from arbora.core.settings import RuntimeConfig
    from arbora.mcp_server.host_caches import ToolTable
    from arbora.security.process_authority import ProcessAuthority

    tools = ToolTable(ProcessAuthority(config=RuntimeConfig()))
    tools.bind_projection(surface.projection)
    return tools


def _served(surface, monkeypatch, *, bound: bool = True) -> SimpleNamespace:
    """A served invocation whose fleet gateway is (or is not) bound."""
    from arbora.mcp_server.fleet_composition import LateFleetGateway

    late = LateFleetGateway()
    if bound:
        late.bind(object(), surface.ops)
    runtime_ns = SimpleNamespace(bindings={"fleet_gateway": late})
    return SimpleNamespace(services=SimpleNamespace(runtime=runtime_ns))


@pytest.mark.spec("ARBORA-OPS-R035.2")
def test_running_server_lists_exactly_the_resident_tools(surface, monkeypatch) -> None:
    """Startup attaches the ten resident tools once and the launchers stay."""
    from arbora.mcp_server.backing.tools.mcp_apps import register_mcp_apps_tools
    from arbora.mcp_server.server import _register_resident_surface

    invocation = _served(surface, monkeypatch)
    tools = _projected_tools(surface)
    register_mcp_apps_tools(surface.mcp)  # registered by the host before startup
    _register_resident_surface(surface.mcp, surface.mux, invocation, tools)

    async def listed() -> set[str]:
        tools = await surface.mcp.list_tools()
        return {tool.name for tool in tools}

    names = asyncio.run(listed())
    launchers = {"graph_task_progress_app", "graph_trace_waterfall_app"}
    assert names == set(RESIDENT_NAMES) | launchers
    assert surface.mux._governed_fleet is not None
    # The visibility middleware denies no resident tool and no launcher.
    middleware = GovernedSessionVisibility(surface.binding, frozenset(launchers))

    async def candidates(_context):
        return [SimpleNamespace(name=name) for name in sorted(names)]

    visible = asyncio.run(middleware.on_list_tools(None, candidates))
    assert {tool.name for tool in visible} == names
    # A second attachment refuses: one registration path, attached once.
    with pytest.raises(RuntimeError, match="empty and unbound"):
        _register_resident_surface(surface.mcp, surface.mux, invocation, tools)


@pytest.mark.spec("ARBORA-OPS-R035.2")
def test_an_unbound_fleet_gateway_aborts_startup(surface, monkeypatch) -> None:
    from arbora.mcp_server.server import _register_resident_surface

    invocation = _served(surface, monkeypatch, bound=False)
    tools = _projected_tools(surface)
    with pytest.raises(RuntimeError, match="bound fleet gateway"):
        _register_resident_surface(surface.mcp, surface.mux, invocation, tools)
    assert not hasattr(surface.mux, "_governed_fleet")
    # With no served invocation there is no resident surface to attach.
    _register_resident_surface(surface.mcp, surface.mux, None, tools)
