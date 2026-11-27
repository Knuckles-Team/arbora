"""HostCaches owns the tool table; two hosts share none of it (ARBORA-HOST-R039.5)."""

from __future__ import annotations

import dataclasses

import pytest
from fastmcp import FastMCP

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.arbora_surface import register_arbora_surface
from arbora.mcp_server.host_caches import HostCaches
from arbora.mcp_server.host_context import build_host_context


def _register(caches: HostCaches) -> None:
    register_arbora_surface(
        FastMCP("host"),
        action_policies=ActionPolicies(),
        approvals=ApprovalDecisions(),
        caches=caches,
        engine_provider=lambda: None,
        intent_tools=False,
    )


def test_two_host_caches_register_independent_tool_tables() -> None:
    first = build_host_context().caches
    second = build_host_context().caches
    assert first.tools is not second.tools

    _register(first)
    assert "graph_query" in first.tools
    assert len(second.tools) == 0

    _register(second)
    assert "graph_query" in second.tools
    assert first.tools["graph_query"] is not second.tools["graph_query"]
    # The tool table moved off the module: no global table or name set remains.
    assert not hasattr(runtime, "REGISTERED_TOOLS")
    assert not hasattr(runtime, "_FULL_TOOL_NAMES")


def test_a_table_is_complete_only_after_a_finished_build_and_only_while_whole() -> None:
    caches = HostCaches()
    assert not caches.tools.is_complete()
    caches.tools["find"] = object()
    assert not caches.tools.is_complete()
    caches.tools.mark_complete()
    assert caches.tools.is_complete()
    del caches.tools["find"]
    assert not caches.tools.is_complete()


def test_host_caches_is_frozen_and_slotted() -> None:
    caches = HostCaches()
    with pytest.raises(dataclasses.FrozenInstanceError):
        caches.tools = caches.tools  # type: ignore[misc]
    assert not hasattr(caches, "__dict__")
