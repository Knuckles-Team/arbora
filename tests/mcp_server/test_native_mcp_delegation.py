"""ARBORA-HOST-R017: the WebUI inventory lists native Arbora tools.

The ``arbora`` server is listed from the served host's registered tool
surface. It is never probed as a fleet catalog child.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import webui_mcp_delegation as mcp_delegation
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.process_authority import ProcessAuthority

pytestmark = pytest.mark.spec("ARBORA-HOST-R017")


class _FakeMux:
    def __init__(self) -> None:
        self._host_mcp = object()
        self.fleet = SimpleNamespace(config=RuntimeConfig())
        self.child_probes: list[str] = []

    async def delegated_server_tools(self, server_name: str) -> list[dict[str, Any]]:
        self.child_probes.append(server_name)
        return [{"name": f"{server_name}_tool"}]


#: The host tool table of the delegation under test.
_TOOLS = ToolTable(ProcessAuthority(config=RuntimeConfig()))
_TOOLS["graph_registered"] = object()
_TOOLS.bind_verb("find", object())


@pytest.fixture
def mux(monkeypatch: pytest.MonkeyPatch) -> _FakeMux:
    fake = _FakeMux()

    async def run_on_served(operation: Any) -> Any:
        return await operation(fake)

    host_tools = {
        "find": SimpleNamespace(description="discover", parameters={"type": "object"}),
        "graph_registered": SimpleNamespace(description="registered", parameters=None),
        "unregistered_leak": SimpleNamespace(description="hidden", parameters={}),
    }
    monkeypatch.setattr(mcp_delegation, "run_on_served_multiplexer", run_on_served)
    monkeypatch.setattr(
        mcp_delegation, "require_fleet_capability", lambda kind, config: None
    )
    monkeypatch.setattr(mcp_delegation, "provider_tools", lambda host: host_tools)
    return fake


def _list(server_name: str) -> list[dict[str, Any]]:
    helpers = mcp_delegation.webui_mcp_delegation_helpers(_TOOLS)

    async def _call() -> list[dict[str, Any]]:
        return await helpers["list_mcp_server_tools"](server_name=server_name)

    return asyncio.run(_call())


def test_native_server_lists_registered_tools_without_a_child_probe(
    mux: _FakeMux,
) -> None:
    tools = _list(mcp_delegation.NATIVE_SERVER_NAME)
    assert [tool["name"] for tool in tools] == ["find", "graph_registered"]
    assert tools[0]["inputSchema"] == {"type": "object"}
    assert tools[1]["inputSchema"] == {}
    assert mux.child_probes == []


def test_a_child_server_is_probed_through_the_catalog(mux: _FakeMux) -> None:
    helpers = mcp_delegation.webui_mcp_delegation_helpers(_TOOLS)

    async def _call() -> list[dict[str, Any]]:
        return await helpers["list_mcp_server_tools"](server_name="jellyfin")

    tools = asyncio.run(_call())
    assert tools == [{"name": "jellyfin_tool"}]
    assert mux.child_probes == ["jellyfin"]


def test_native_listing_refuses_when_the_host_surface_is_absent(
    mux: _FakeMux,
) -> None:
    from fastmcp.exceptions import ToolError

    mux._host_mcp = None
    with pytest.raises(ToolError):
        _list(mcp_delegation.NATIVE_SERVER_NAME)
    assert mux.child_probes == []
