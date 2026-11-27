"""Attaching the fleet loader to a served MCP server: the governed tool mount and the child result projection (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import collections.abc as _collections_abc
import logging
import typing as _typing

import fastmcp.tools as _fastmcp_tools

if _typing.TYPE_CHECKING:
    from mcp_types import CallToolResult as MCPCallToolResult
    from mcp_types import Tool as MCPTool
import arbora.api.domain.fleet.catalog_reader as _catalog_reader
from arbora.fleet.child_mounts import (
    _forwarder_component,
    _make_forwarder,
)
from arbora.fleet.multiplexer import (
    MCPMultiplexer,
)
from arbora.fleet.state import FleetState
from arbora.security.run_token import RunTokenSigner

logger = logging.getLogger("mcp_multiplexer")


def tool_result_from_child(result: MCPCallToolResult) -> _fastmcp_tools.ToolResult:
    """Convert a child MCP call result into the host's ``_fastmcp_tools.ToolResult`` WITHOUT
    losing semantic parity with the upstream protocol (D-CDX-45).

    A manual ``_fastmcp_tools.ToolResult(content=..., structured_content=...)``
    reconstruction (the prior implementation) silently dropped the result's
    wire ``_meta`` — never forwarded at all — creating semantic drift between
    a direct-provider call and the same call routed through arbora/the
    multiplexer, and hiding any safety/routing metadata a child attaches
    there. Per-content-block ``annotations`` (e.g. ``readOnlyHint``-style
    hints on a ``TextContent``/``ImageContent``/etc.) already survive
    ``_fastmcp_tools.ToolResult``'s own constructor because FastMCP's
    ``_convert_to_content`` returns a list of already-``ContentBlock``
    items unchanged rather than rebuilding them — but this still uses
    FastMCP's own protocol-parity constructor rather than relying on that as
    an implementation detail, because ``from_mcp_result`` ALSO retains the
    exact upstream ``CallToolResult`` object (``_raw_mcp_result``) so
    ``to_mcp_result()`` later round-trips byte-for-byte, not merely
    field-for-field.

    Falls back to an explicit manual reconstruction — forwarding ``meta``
    itself instead of omitting it — if a future FastMCP version drops
    ``from_mcp_result``: a loudly-logged degrade, never a silent return to
    the lossier prior behavior.
    """
    from_mcp_result = getattr(_fastmcp_tools.ToolResult, "from_mcp_result", None)
    if callable(from_mcp_result):
        return from_mcp_result(result)
    logger.warning(
        "FastMCP _fastmcp_tools.ToolResult.from_mcp_result is unavailable on this SDK "
        "version; falling back to a manual reconstruction that still "
        "forwards meta/content/structured_content explicitly so annotation "
        "and _meta parity is not silently lost."
    )
    return _fastmcp_tools.ToolResult(
        content=list(getattr(result, "content", []) or []),
        structured_content=getattr(result, "structured_content", None),
        meta=getattr(result, "meta", None),
    )


def make_governed_tool_mount(
    mcp,
    mux: MCPMultiplexer,
    native_name,
    resident_names: _collections_abc.Collection[str],
):
    """Prepare MultiplexerOps' tool mount callback without activating the host.

    Non-tool native mounts need their own verified read adapter. The process
    owner composes that adapter rather than pretending a resource is a tool.
    """

    async def mount(item, _forwarder):
        if item.kind != "tool":
            raise RuntimeError("governed non-tool mount adapter is required")
        name = native_name(item)
        if name in resident_names:
            raise ValueError("native fleet name collides with a resident tool")
        tool = _fastmcp_tools.FunctionTool(
            name=name,
            description=item.description,
            parameters=dict(item.schema),
            fn=_make_forwarder(lambda: getattr(mux, "_governed_fleet", None), name),
        )
        mcp.add_tool(tool)
        attached = await mcp.get_tool(name)
        if attached is None or getattr(attached, "fn", None) is not tool.fn:
            raise RuntimeError("native fleet tool attachment was incomplete")

    return mount


def _register_forwarder(mcp, mux: MCPMultiplexer, tool: MCPTool) -> bool:
    """Register ONE aggregated child tool as a live FastMCP forwarding tool.

    Idempotent via ``mux._exposed`` so lazy mounts never double-register.
    Schema replacement is deliberately handled by
    :meth:`MCPMultiplexer._replace_exposed_forwarders`, where the complete
    FastMCP registry can be staged atomically. Returns True if FastMCP was
    asked to register a tool. Shared by eager startup and the dynamic
    ``load_tools`` meta-tool.
    """
    if tool.name in mux._exposed:
        return False
    mcp.add_tool(
        _forwarder_component(lambda: getattr(mux, "_governed_fleet", None), tool)
    )
    mux._exposed.add(tool.name)
    return True


def attach_fleet_loader(
    mcp,
    *,
    catalog_reader: _catalog_reader.FleetCatalogSource,
    fleet: FleetState,
    self_server: str = "arbora",
    authority_scope=None,
    signer: RunTokenSigner | None = None,
) -> MCPMultiplexer:
    """Attach on-demand MCP fleet access to an EXISTING FastMCP server (arbora).

    arbora serves its own intent tools. This composes the fleet-aggregation
    engine on top so the SAME server can also reach the rest of the MCP fleet,
    from the mandatory EG catalog reader, through them: ``find`` searches the
    fleet catalog, ``act(action="fleet.call")`` calls a fleet tool and
    ``manage(action="fleet.load"|"fleet.unload")`` mounts or releases one. No
    fleet tool or meta-tool is listed separately. Child servers are mounted
    LAZILY (each as an isolated subprocess/HTTP session via
    :class:`~arbora.fleet.child_resilience.ChildRuntime`, with its own breaker +
    concurrency limit) only when a tool is actually called. Returns the
    :class:`MCPMultiplexer` for lifecycle — call ``await mux.aclose()`` on
    shutdown. (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)

    All wiring here is synchronous (catalog-state initialization + binding); no
    child is spawned at attach time, so it drops cleanly into arbora's
    synchronous ``mcp.run(...)`` startup with no event loop.

    ``authority_scope`` is the host's trusted context manager for stdio process
    authority. Arbora supplies its existing verified-tool scope; network calls
    keep their request-minted ambient session. The callback is never derived
    from child configuration or caller arguments.

    ``catalog_reader`` is the mandatory arbora/medulla read port. The
    returned multiplexer starts empty and the composition root must await
    ``refresh_engine_catalog()`` before starting children.

    ``fleet`` is the host context's fleet state (ARBORA-HOST-R039.2).
    """
    logger.info("arbora fleet loader initializing")
    mux = MCPMultiplexer(catalog_reader, fleet=fleet, signer=signer)
    # Keep the host solely for lifecycle replacement of mux-owned forwarding
    # schemas after a child generation recovers.  Standalone mux/probe paths
    # deliberately leave this unset.
    mux._host_mcp = mcp
    mux._authority_scope = authority_scope
    # arbora is the HOST server — never mount it (or the retired standalone
    # multiplexer name) as a child of itself.
    mux._skip_servers = {"mcp-multiplexer", self_server}
    # Initialize the local catalog state without spawning children. A native EG
    # reader intentionally leaves this empty until the async composition root
    # calls refresh_engine_catalog(); it never consults the static file.
    mux.load_catalog()
    # Reuse the host's one native WorkItem Tasks extension for owning-server
    # follow-ups.  FastMCP 4 stores extensions by identifier; adding a second
    # ``io.modelcontextprotocol/tasks`` extension would overwrite handlers and
    # create an accidental parallel authority.  The private mapping is stable
    # in the exact locked FastMCP 4.0.0b1 API and is guarded for older/degraded
    # images where the extension was intentionally not mounted.
    tasks_extension = getattr(mcp, "_extensions", {}).get(
        "io.modelcontextprotocol/tasks"
    )
    if tasks_extension is not None:
        setter = getattr(tasks_extension, "set_task_router", None)
        if callable(setter):
            setter(mux)
        else:
            logger.warning(
                "native Tasks extension does not expose multiplexer route binding"
            )
    # CONCEPT:MERISTEM-ECO.mcp.intent-surface-condensed-collapse — the fleet is reached
    # through the host's intent tools (``find`` searches the catalog, ``act``
    # calls a fleet tool, ``manage`` mounts/releases), never as separately listed
    # MCP tools, so no meta-tool or per-session visibility layer is attached.
    # Stash the mux on the server for those intent tools.
    mcp._fleet_mux = mux
    import arbora.fleet.shared_multiplexer as _shared_multiplexer

    _shared_multiplexer.bind_served_multiplexer(mux)
    mcp.add_extension(_shared_multiplexer.ServedMultiplexerLoopExtension(mux))
    mux._claim_serving_loop = lambda: _shared_multiplexer.claim_served_multiplexer_loop(
        mux
    )
    logger.info(
        "arbora fleet loader ready: %d MCP server(s) reachable through the "
        "intent tools.",
        len(mux.load_catalog()),
    )
    return mux
