"""graph-os registers its harness host bindings with agent-utilities' thin client."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from agent_utilities.graphos_client import host

from graph_os.mcp_server import harness_host, runtime


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


@pytest.mark.spec("GRAPHOS-HOST-R001.2")
def test_without_graph_os_the_harness_fails_closed():
    assert host.native_tool_names() == frozenset()
    assert host.fleet_catalog() == {}
    with pytest.raises(RuntimeError):
        host.build_native_toolset(["graph_query"], toolset_id="t")


@pytest.mark.spec("GRAPHOS-HOST-R001.2")
def test_registration_binds_native_tools_fleet_and_keepalive(monkeypatch):
    built = []
    monkeypatch.setitem(runtime.REGISTERED_TOOLS, "graph_query", lambda: None)
    monkeypatch.setattr(
        runtime,
        "build_native_graphos_toolset",
        lambda names, *, toolset_id: built.append((names, toolset_id)) or "ts",
    )
    entered = []

    @contextlib.asynccontextmanager
    async def scope(session=None):
        entered.append(True)
        yield

    monkeypatch.setattr(runtime, "authority_keepalive_scope", scope)

    harness_host.register_harness_host(_Mux())

    assert "graph_query" in host.native_tool_names()
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


@pytest.mark.spec("GRAPHOS-HOST-R001.2")
def test_unregister_clears_every_binding():
    harness_host.register_harness_host(SimpleNamespace(load_catalog=dict))
    harness_host.unregister_harness_host()
    assert host.fleet_catalog() == {}
    with pytest.raises(RuntimeError):
        host.build_native_toolset(["graph_query"], toolset_id="t")


@pytest.mark.spec("GRAPHOS-HOST-R001.2")
def test_mcp_server_entry_point_registers_the_host():
    import inspect

    from graph_os.mcp_server import server

    source = inspect.getsource(server.mcp_server)
    assert "register_harness_host(fleet_mux)" in source
    assert "unregister_harness_host()" in source
