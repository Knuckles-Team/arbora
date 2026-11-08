"""arbora registers its harness host bindings with agent-utilities' thin client."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from meristem import host_ports as host

from arbora.mcp_server import harness_host, runtime


@pytest.fixture(autouse=True)
def _clear():
    harness_host.unregister_harness_host()
    yield
    harness_host.unregister_harness_host()


class _Mux:
    def load_catalog(self):
        return {"github-mcp": {"url": "http://127.0.0.1:9001/mcp/"}, "x-mcp": {}}

    def server_prefix(self, name):
        return {"github-mcp": "github", "x-mcp": "x"}[name]


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_without_arbora_the_harness_fails_closed():
    assert host.native_tool_names() == frozenset()
    assert host.fleet_catalog() == {}
    with pytest.raises(RuntimeError):
        host.build_native_toolset(["graph_query"], toolset_id="t")


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_registration_binds_native_tools_fleet_and_keepalive(monkeypatch):
    built = []
    monkeypatch.setitem(runtime.REGISTERED_TOOLS, "graph_query", lambda: None)
    monkeypatch.setitem(runtime.NATIVE_INTENT_TOOLS, "find", lambda: None)
    monkeypatch.setattr(
        runtime,
        "build_native_arbora_toolset",
        lambda names, *, toolset_id: built.append((names, toolset_id)) or "ts",
    )
    entered = []

    @contextlib.asynccontextmanager
    async def scope(session=None):
        entered.append(True)
        yield

    monkeypatch.setattr(runtime, "authority_keepalive_scope", scope)

    harness_host.register_harness_host(_Mux())

    assert {"graph_query", "find"} <= host.native_tool_names()
    assert host.build_native_toolset(["graph_query"], toolset_id="t1") == "ts"
    assert built == [(["graph_query"], "t1")]
    fleet = host.fleet_catalog()
    assert fleet["github-mcp"] == host.FleetServer(
        prefix="github", url="http://127.0.0.1:9001/mcp"
    )
    assert fleet["x-mcp"].url == ""

    import asyncio

    async def run():
        async with host.authority_keepalive_scope():
            pass

    asyncio.run(run())
    assert entered == [True]


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_unregister_clears_every_binding():
    harness_host.register_harness_host(SimpleNamespace(load_catalog=dict))
    harness_host.unregister_harness_host()
    assert host.fleet_catalog() == {}
    with pytest.raises(RuntimeError):
        host.build_native_toolset(["graph_query"], toolset_id="t")
    with pytest.raises(RuntimeError):
        host.build_url_toolset("http://127.0.0.1:9/mcp")
    with pytest.raises(RuntimeError):
        host.open_tool_client("http://127.0.0.1:9/mcp")


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_registration_binds_remote_toolsets_and_tool_clients(monkeypatch, tmp_path):
    from arbora.fleet import client_toolsets

    loaded = []

    def fake_load(path):
        loaded.append(path)
        return []

    monkeypatch.setattr("pydantic_ai.mcp.load_mcp_toolsets", fake_load)
    harness_host.register_harness_host()

    config = tmp_path / "mcp_config.json"
    assert host.load_config_toolsets(config) == []
    assert loaded == [config]
    assert host.open_tool_client is not None
    assert host._TOOL_CLIENT is client_toolsets.open_tool_client


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

    harness_host.register_harness_host()

    async def run():
        async with host.open_tool_client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            result = await client.call_tool("ok", {"value": 1})
            with pytest.raises(host.HostToolError):
                await client.call_tool("broken")
        return names, result

    names, result = asyncio.run(run())
    assert names == {"ok", "broken"}
    assert result.data == 2


@pytest.mark.spec("ARBORA-HOST-R001.2")
def test_mcp_server_entry_point_registers_the_host():
    import inspect

    from arbora.mcp_server import server

    source = inspect.getsource(server.mcp_server)
    assert "register_harness_host(fleet_mux)" in source
    assert "unregister_harness_host()" in source
