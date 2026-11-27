"""arbora carries its harness host bindings in the one ``HostPorts`` record."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from meristem import host_ports as host
from meristem.host_ports import HostPorts

from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import runtime
from arbora.mcp_server.harness_ports import bind_host_ports_of, build_host_ports
from arbora.mcp_server.host_context import build_host_context


def _ports(host_context, multiplexer=None) -> HostPorts:
    """The ``HostPorts`` the served host builds, over ``host_context``."""
    return build_host_ports(host_context, multiplexer=multiplexer)


class _Mux:
    fleet = SimpleNamespace(config=RuntimeConfig())

    def load_catalog(self):
        return {"github-mcp": {"url": "http://127.0.0.1:9001/mcp/"}, "x-mcp": {}}

    def server_prefix(self, name):
        return {"github-mcp": "github", "x-mcp": "x"}[name]


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_without_arbora_the_harness_fails_closed():
    ports = HostPorts()
    assert ports.native_tool_names() == frozenset()
    assert ports.fleet_catalog() == {}
    with pytest.raises(RuntimeError):
        ports.build_native_toolset(["graph_query"], toolset_id="t")


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_the_record_binds_native_tools_fleet_and_keepalive(monkeypatch):
    built = []
    host_context = build_host_context()
    host_context.caches.tools["graph_query"] = lambda: None
    monkeypatch.setitem(runtime.NATIVE_INTENT_TOOLS, "find", lambda: None)
    monkeypatch.setattr(
        runtime,
        "build_native_arbora_toolset",
        lambda names, tools, *, toolset_id, emit=None: (
            built.append((names, toolset_id)) or "ts"
        ),
    )
    entered = []

    @contextlib.asynccontextmanager
    async def scope(authority, session=None):
        entered.append(True)
        yield

    monkeypatch.setattr(runtime, "authority_keepalive_scope", scope)

    ports = _ports(host_context, _Mux())

    assert {"graph_query", "find"} <= ports.native_tool_names()
    assert ports.build_native_toolset(["graph_query"], toolset_id="t1") == "ts"
    assert built == [(["graph_query"], "t1")]
    fleet = ports.fleet_catalog()
    assert fleet["github-mcp"] == host.FleetServer(
        prefix="github", url="http://127.0.0.1:9001/mcp"
    )
    assert fleet["x-mcp"].url == ""

    import asyncio

    async def run():
        async with ports.authority_keepalive_scope():
            pass

    asyncio.run(run())
    assert entered == [True]


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_an_unbound_host_slot_carries_no_binding():
    context = build_host_context()
    assert context.runs.ports.binding is None
    ports = context.runs.ports.current()
    assert ports.fleet_catalog() == {}
    with pytest.raises(RuntimeError):
        ports.build_native_toolset(["graph_query"], toolset_id="t")
    with pytest.raises(RuntimeError):
        ports.build_url_toolset("http://127.0.0.1:9/mcp")
    with pytest.raises(RuntimeError):
        ports.open_tool_client("http://127.0.0.1:9/mcp")


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_binding_the_host_holds_the_record_on_its_slot():
    context = build_host_context()
    bind_host_ports_of(context, multiplexer=SimpleNamespace(load_catalog=dict))
    assert context.runs.ports.binding.refusal is None
    assert context.runs.ports.current() is context.runs.ports.binding.ports
    assert context.runs.ports.current().fleet_catalog() == {}


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_the_record_binds_remote_toolsets_and_tool_clients(monkeypatch, tmp_path):
    from arbora.fleet import client_toolsets

    loaded = []

    def fake_load(path):
        loaded.append(path)
        return []

    monkeypatch.setattr("pydantic_ai.mcp.load_mcp_toolsets", fake_load)
    ports = _ports(build_host_context())

    config = tmp_path / "mcp_config.json"
    assert ports.load_config_toolsets(config) == []
    assert loaded == [config]
    assert ports.tool_client is client_toolsets.open_tool_client


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_tool_client_reports_tool_errors_as_host_errors():
    import asyncio

    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    server = FastMCP("probe")

    @server.tool
    def ok(value: int) -> int:
        return value + 1

    @server.tool
    def broken() -> int:
        raise ToolError("nope")

    ports = _ports(build_host_context())

    async def run():
        async with ports.open_tool_client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            result = await client.call_tool("ok", {"value": 1})
            with pytest.raises(host.HostToolError):
                await client.call_tool("broken")
        return names, result

    names, result = asyncio.run(run())
    assert names == {"ok", "broken"}
    assert result.data == 2


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_mcp_server_entry_point_binds_the_host_record():
    import inspect

    from arbora.mcp_server import server

    source = inspect.getsource(server.mcp_server)
    assert "bind_host_ports_of(host_context" in source
    assert "register_harness_host" not in source
