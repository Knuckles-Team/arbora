"""Contract test: the MCP tool surface and the REST gateway must stay in lockstep.

Both surfaces dispatch through ``_execute_tool`` against the same in-process
engine, while the immutable ToolSpec universe is the capability source of
truth. This test enforces that every canonical profile tool has exactly one
collapsed REST twin (via the tool table routes) and that the twin is actually
mounted by ``_mount_rest_routes`` — and vice versa. If a new MCP tool is added
without a REST route (or a route is added for a non-existent tool), this fails,
preventing the two surfaces from drifting.
"""

from __future__ import annotations

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.tool_specs import (
    INTENT_VERBS,
    TOOL_SPECS_BY_NAME,
    canonical_tool_names,
)
from arbora.mcp_server.backing.tools import engine_tools
from arbora.mcp_server.host_caches import HostCaches, build_host_caches
from arbora.mcp_server.routes import mount_rest_routes

# The 21 ``engine_<domain>`` tool families are declared as REQUIRED
# (``feature=None``) in the distribution-owned ``ToolSpec`` manifest -- a real
# deployment always ships them (they are not deployment-configurable like the
# ``quant``/``finance`` optional feature). They are registered at runtime only
# when the real ``medulla`` client package is importable
# (``engine_tools.ENGINE_DOMAINS``, via ``engine_tools._discover_domains``).
# In the lean CI `gates` lane (`uv sync --no-install-package medulla`,
# documented in tests/conftest.py's ``_is_engine_unreachable_error``) that
# package is deliberately absent, so the two runtime-vs-canonical parity
# checks below see a real, environment-caused mismatch rather than a
# registration bug -- skip only those two, not the other four checks in this
# file (mounted-routes/prefix/skill-coverage), which hold regardless.
_NEEDS_ENGINE_DOMAINS = pytest.mark.skipif(
    not engine_tools.ENGINE_DOMAINS,
    reason=(
        "medulla package not installed in this environment -- the "
        "required engine_<domain> tool families cannot be registered, so "
        "the host tool table vs. the canonical ToolSpec universe legitimately "
        "diverge by exactly those families."
    ),
)


class _RecordingApp:
    """Minimal Starlette-compatible app that records mounted routes."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, list[str]]] = []

    def add_route(self, path, handler, methods=None) -> None:  # noqa: ANN001
        self.routes.append((path, list(methods or [])))


def _mounted_paths(caches: HostCaches, prefix: str = "") -> set[str]:
    runtime.ensure_tools_registered(caches)
    app = _RecordingApp()
    mount_rest_routes(app, caches.tools, prefix=prefix)
    return {path for path, _methods in app.routes}


def _canonical_runtime_names(caches: HostCaches) -> frozenset[str]:
    tools = set(caches.tools)
    unknown = tools - set(TOOL_SPECS_BY_NAME)
    assert not unknown, (
        f"runtime registered unknown ToolSpec families: {sorted(unknown)}"
    )
    features = frozenset(
        spec.feature
        for name, spec in TOOL_SPECS_BY_NAME.items()
        if name in tools and spec.feature is not None
    )
    include_intent = set(INTENT_VERBS) <= tools
    return canonical_tool_names(features=features, include_intent=include_intent)


@_NEEDS_ENGINE_DOMAINS
def test_every_mcp_tool_has_a_rest_route():
    caches = build_host_caches(RuntimeConfig())
    runtime.ensure_tools_registered(caches)
    tools = set(caches.tools)
    mapped = set(caches.tools.routes)

    assert tools == set(_canonical_runtime_names(caches))

    missing = tools - mapped
    assert not missing, (
        f"MCP tools with no REST twin in ACTION_TOOL_ROUTES: {sorted(missing)}. "
        "Add a collapsed REST route so the gateway reaches every MCP capability."
    )


@_NEEDS_ENGINE_DOMAINS
def test_no_phantom_routes_for_missing_tools():
    caches = build_host_caches(RuntimeConfig())
    runtime.ensure_tools_registered(caches)
    tools = set(caches.tools)
    mapped = set(caches.tools.routes)

    assert tools == set(_canonical_runtime_names(caches))

    phantom = mapped - tools
    assert not phantom, (
        f"ACTION_TOOL_ROUTES references tools not in the host tool table: {sorted(phantom)}."
    )


def test_mapped_routes_are_actually_mounted():
    caches = build_host_caches(RuntimeConfig())
    paths = _mounted_paths(caches)
    for tool, path in caches.tools.routes.items():
        assert path in paths, (
            f"Tool '{tool}' maps to '{path}' but that route is not mounted by "
            "_mount_rest_routes."
        )


def test_prefix_is_applied_to_mounted_routes():
    # The API gateway mounts these under /api — confirm the prefix is honoured.
    caches = build_host_caches(RuntimeConfig())
    paths = _mounted_paths(caches, prefix="/api")
    for path in caches.tools.routes.values():
        assert ("/api" + path) in paths


def test_retired_legacy_tool_fanout_is_not_mounted():
    """Old standalone mining/research routes are absent from AU's served API."""
    caches = build_host_caches(RuntimeConfig())
    paths = _mounted_paths(caches)
    retired = {"graph_mine", "graph_mine_deep", "graph_ops_causal", "graph_research"}
    assert retired.isdisjoint(caches.tools)
    assert retired.isdisjoint(caches.tools.routes)
    assert "/graph/research" not in paths
    assert "/ops/causal" not in paths
    assert not any(path.startswith("/mining/") for path in paths)
    assert not any(
        path.startswith("/graph/analyze/")
        and path.rsplit("/", 1)[-1]
        in {"synthesize", "deep-extract", "background-research", "relevance-sweep"}
        for path in paths
    )


# ── Third leg: MCP verb ⇄ domain-skill coverage (CONCEPT:MERISTEM-ECO.mcp.kg-skill-verb-coverage) ──
# Beyond REST⇄MCP parity, every Arbora verb must be claimed explicitly by a
# retained workflow skill's ``agents/arbora.yaml`` sidecar. The contract lives
# in ``arbora.mcp_server.backing.skill_coverage`` and has no slug inference or waivers.
#
# Every domain-tier skill (the ones that claimed verb coverage) moved to
# arbora on 2026-10-03; meristem now bundles only the platform-tier
# meristem-development skill. Full fleet-wide coverage completeness
# can only be verified where the covering skills actually live -- arbora's
# own test suite, which bundles them and depends on meristem (the
# reverse is not true) -- so the exact completeness assertion that used to
# run here moved there. ``test_domain_skill_sidecars_are_valid_and_have_no_orphans``
# stays: it is still meaningful (vacuously or not) for whatever domain skill
# meristem bundles in the future.


def test_domain_skill_sidecars_are_valid_and_have_no_orphans():
    from arbora.mcp_server.backing import skill_coverage

    report = skill_coverage.compute_coverage()
    assert not report.orphans, (
        "Domain skills claiming verbs outside the live Arbora surface: "
        f"{report.orphans}. Fix the explicit sidecar claim."
    )
    assert not report.duplicates, (
        "Arbora verbs with ambiguous domain ownership: "
        f"{report.duplicates}. Keep each verb in exactly one domain sidecar."
    )
    assert not report.invalid_sidecars, (
        "Invalid agents/arbora.yaml sidecars: "
        f"{report.invalid_sidecars}. Follow the closed version-2 schema."
    )


def test_arbora_external_rlm_dependency_is_declared() -> None:
    """``graph_rlm`` stays a declared external Arbora dependency.

    Its owning domain skill, ``graph-research-and-analysis``, moved to
    arbora on 2026-10-03 along with every other domain-tier skill, so the
    "is it claimed by that skill's sidecar" half of this contract now lives
    in arbora's own test suite; this half (the name itself is still the
    one agreed external-dependency marker) stays here.
    """
    from arbora.mcp_server.backing.skill_coverage import EXTERNAL_ARBORA_TOOL_NAMES

    assert EXTERNAL_ARBORA_TOOL_NAMES == {"graph_rlm"}
