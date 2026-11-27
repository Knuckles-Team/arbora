"""HostCaches owns the tool table; two hosts share none of it (ARBORA-HOST-R039.5)."""

from __future__ import annotations

import dataclasses
import importlib

import pytest
from fastmcp import FastMCP

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.arbora_surface import register_arbora_surface
from arbora.mcp_server.host_caches import HostCaches
from arbora.mcp_server.host_context import build_host_context

_REPLACED = {
    "arbora.security.auth": ("_jwks_cache",),
    "arbora.security.oidc_discovery": ("_cache",),
    "arbora.mcp_server.catalog_warmup": ("_CURRENT",),
    "arbora.mcp_server.bootstrap": ("_BUNDLED_SKILL_READINESS",),
    "arbora.mcp_server.backing.tools.engine_surface_tools": ("_RAM_CHECKPOINT_STORE",),
    "arbora.mcp_server.backing.tools.analysis_tools": ("_analysis_core_handler",),
    "arbora.mcp_server.backing.tools.intent_tools": (
        "_CANDIDATES_CACHE",
        "_ACTIONS_BY_TOOL_CACHE",
        "_RESOLUTION_CACHE",
        "_PREVIEW_PLAN_CACHE",
        "_OPERATION_VERBS_CACHE",
        "_OUTCOME_ROUTER",
        "_APPROVALS",
    ),
}


def _assert_replaced_names_are_gone() -> None:
    """Every other cache the row names left its module too."""
    for module, names in _REPLACED.items():
        loaded = importlib.import_module(module)
        assert [n for n in names if hasattr(loaded, n)] == [], module


def _register(caches: HostCaches) -> None:
    register_arbora_surface(
        FastMCP("host"),
        action_policies=ActionPolicies(),
        approvals=ApprovalDecisions(),
        caches=caches,
        engine_provider=lambda: None,
        intent_tools=False,
    )


@pytest.mark.spec("ARBORA-HOST-R039.5")
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
    _assert_replaced_names_are_gone()


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


def test_intent_state_is_per_host_and_previews_and_approvals_expire() -> None:
    first, second = HostCaches().intent, HostCaches().intent
    first.approve("op", now=0.0)
    first.remember_preview("ref", "plan", now=0.0)
    assert first.approved("op", now=1.0)
    assert first.preview("ref", now=1.0) == "plan"
    assert not second.approved("op", now=1.0)
    assert second.preview("ref", now=1.0) is None
    later = first.TTL_SECONDS + 1
    assert not first.approved("op", now=later)
    assert first.preview("ref", now=later) is None


def test_a_rebuilt_candidate_table_versions_the_resolution_key() -> None:
    cache = HostCaches().intent
    assert cache.candidates() is None
    assert cache.generation == 0
    cache.store_candidates([])
    assert cache.candidates() == []
    assert cache.generation == 1
