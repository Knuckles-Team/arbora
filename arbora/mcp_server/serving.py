"""Composition root for Arbora's served MCP intent surface (ARBORA-HOST-R023).

Arbora serves its eight-tool MCP contract itself: the six intent verbs
(``ask``, ``find``, ``why``, ``write``, ``act``, ``manage``) over its own
operation registry and invocation pipeline, plus the two MCP Apps launcher
tools (task progress, trace waterfall). The agent runtime (meristem)
serves no MCP tools of its own; it only supplies the reusable registrar
function this module calls for the two launcher tools, exactly like any
other library dependency.

``arbora.mcp_server.server`` calls :func:`register_intent_surface` instead
of the agent runtime's ``arbora.mcp_server.backing.arbora_surface.register_arbora_surface``
(ARBORA-HOST-R024); that cutover, and the agent runtime's MCP-serving removal
(ARBORA-HOST-R025), are separate, later changes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from arbora.api.mcp.discovery import FleetCatalog, FleetSearch
from arbora.api.mcp.verbs import VERBS, MCPProjection, native_verb
from arbora.api.registry import Invoke
from arbora.observability.state import SubsystemWindows

__all__ = [
    "PARITY_SCHEMA_DIVERGED",
    "PARITY_TOOL_MISSING",
    "ParityRefused",
    "compare_catalogs",
    "http_catalog",
    "mcp_catalog",
    "register_intent_surface",
]


def register_intent_surface(
    mcp: Any,
    *,
    registry: Any,
    services: Any,
    resolver: Any,
    caller_for_request: Callable[[], Any],
    policy_gate: Any,
    invoke: Invoke,
    fleet_search: FleetSearch | None = None,
    fleet_catalog: FleetCatalog | None = None,
    windows: SubsystemWindows,
) -> MCPProjection:
    """Compose Arbora's served surface on ``mcp``; return the built projection.

    Builds the projection over ``registry``/``services``/``invoke`` (the shared
    operation chokepoint, ARBORA-OPS-R007), binds the native verb twins and
    registers the two MCP Apps launcher tools from the agent runtime's reusable
    registrar. The verbs themselves are attached by ``register_mcp_tools``. The returned
    :class:`~arbora.api.mcp.verbs.MCPProjection` lets a caller read
    ``registry.digest`` or build :func:`~arbora.api.mcp.verbs.mcp_instructions`.
    """

    projection = MCPProjection(
        registry,
        services,
        resolver,
        caller_for_request,
        policy_gate,
        invoke,
        fleet_search,
        fleet_catalog,
        windows=windows,
    )
    from arbora.mcp_server import runtime

    # The verbs are attached to MCP by ``register_mcp_tools`` at startup
    # (ARBORA-OPS-R035.2), the one registration path; the native twins below
    # are what the REST gateway reaches.
    for name in VERBS:
        runtime.NATIVE_INTENT_TOOLS[name] = native_verb(name, projection)
    _register_app_launchers(mcp)
    return projection


def _register_app_launchers(mcp: Any) -> None:
    """The two MCP Apps launcher tools (task progress, trace waterfall).

    Standalone: each launcher tool only returns an opaque id; the app's own
    HTML polls the real data back through the ``ask`` tool this module just
    registered, over the host's own postMessage bridge. No intent-surface
    routing logic lives in the agent runtime's registrar.
    """

    from arbora.mcp_server.backing.tools.mcp_apps import register_mcp_apps_tools

    register_mcp_apps_tools(mcp)


# ---- transport parity (ARBORA-HOST-R023.2) ----
#
# The HTTP registrar and the MCP verbs are both built from one registry generation. These
# functions compare the two catalogs a client of each transport reads: the HTTP ``/registry``
# document and the MCP ``find`` descriptors. A tool one transport lacks is
# ``PARITY_TOOL_MISSING``; a tool whose parameter schema differs is ``PARITY_SCHEMA_DIVERGED``.

PARITY_TOOL_MISSING = "PARITY_TOOL_MISSING"
PARITY_SCHEMA_DIVERGED = "PARITY_SCHEMA_DIVERGED"


class ParityRefused(Exception):
    """One transport lacks a tool or serves it with a different schema."""

    def __init__(self, code: str, tool: str) -> None:
        super().__init__(f"{code}: {tool}")
        self.code = code
        self.tool = tool


def _normalized(schema: Any) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)


def http_catalog(document: Mapping[str, Any]) -> dict[str, str]:
    """``{tool name: normalized parameter schema}`` from the HTTP ``/registry`` document."""
    catalog: dict[str, str] = {}
    for row in document.get("ops", ()):
        params = row["params"]
        shape = (
            {"eg_schema": params["eg_schema"]}
            if "eg_schema" in params
            else params["schema"]
        )
        catalog[str(row["id"])] = _normalized(shape)
    return catalog


def mcp_catalog(descriptors: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``{tool name: normalized parameter schema}`` from the MCP ``find`` descriptors."""
    catalog: dict[str, str] = {}
    for item in descriptors:
        schema = item["params_schema"]
        shape = {"eg_schema": schema["$ref"]} if "$ref" in schema else schema
        catalog[str(item["op"])] = _normalized(shape)
    return catalog


def compare_catalogs(http: Mapping[str, str], mcp: Mapping[str, str]) -> None:
    """Return when both transports serve the same tools with the same schemas."""
    for name in sorted(http.keys() ^ mcp.keys()):
        raise ParityRefused(PARITY_TOOL_MISSING, name)
    for name in sorted(http):
        if http[name] != mcp[name]:
            raise ParityRefused(PARITY_SCHEMA_DIVERGED, name)
