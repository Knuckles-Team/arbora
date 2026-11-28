#!/usr/bin/env python
"""Multi-MCP Server Multiplexer.

Aggregates MCP servers declared by medulla's fleet authorities
into a single unified server, delegating tool calls dynamically based on
prefixed tool names. This speeds up boot times and avoids per-server process
resource contention for clients with tool-count limits.

Composed by the native Arbora MCP server: it uses
``create_mcp_server()`` for the standard ``--transport/--host/--port`` args and
middleware, and exposes the aggregated tools through a FastMCP instance so the
multiplexer can be deployed as either a **stdio** or **streamable-http** server.
The proven child-server lifecycle, algorithmic collision-free prefixing, and
enable/disable tool filtering are preserved (see :class:`MCPMultiplexer`).

CONCEPT:MERISTEM-ECO.mcp.standardized-interfaces — MCP Standardized Interfaces
"""

from __future__ import annotations

import asyncio
import collections.abc as _collections_abc
import contextlib
import json
import logging
import re
import secrets
import sys
import time
import typing as _typing

import fastmcp.tools as _fastmcp_tools
from mcp.client.session import ClientSession

from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    from mcp_types import CallToolResult as MCPCallToolResult
    from mcp_types import Tool as MCPTool

# The SOLE handle on the MCP protocol types in this module, for two reasons that
# both bite here. (1) MCP SDK v2 re-homed the whole `mcp.types` namespace into the
# standalone `mcp_types` distribution, so `import mcp.types` raises ImportError at
# module scope on an SDK v2 image — and this module is the fleet loader, so that
# takes every child server down with it. `mcp_types_module()` binds whichever the
# installed SDK ships. (2) `mcp` is also a common PARAMETER name in this file (see
# `_register_forwarder`), where it shadows the package outright; a `mcp_types.X`
# attribute chain there resolves against the parameter, not the SDK.
mcp_types = mcp_types_module()
# Keep the concrete result type available to the focused forwarding tests and
# callers that inspect the native tool-result contract; the module import above
# remains the single dependency binding counted by the package wiring gate.
ToolResult = _fastmcp_tools.ToolResult

# Remote transports. The MCP SDK v2 line (>=2.0.0, pulled in by the
# `fastmcp>=4.0.0b1` floor of the `[mcp]` extra) renamed
# `streamablehttp_client` -> `streamable_http_client` and replaced its
# headers/auth/httpx_client_factory keywords with a single pre-configured
# `http_client=` — see the call site below. Both names are hard imports: the
# `[mcp]` extra can no longer resolve an SDK that lacks them, so the old
# defensive `try/except ImportError` guards only hid a real breakage (they
# turned the rename into a silent `RuntimeError: mcp SDK has no
# streamablehttp_client` on EVERY remote child, which is the whole deployed
# fleet — `deploy/mcp-fleet.registry.yml` defaults to `streamable-http`).

import arbora.api.domain.fleet.catalog_reader as _catalog_reader
import arbora.core.priority as _resource_priority
import arbora.fleet.child_resilience as _child_resilience
from arbora.api.domain.fleet import catalog_machine as _catalog_machine
from arbora.fleet.oauth_brokers import oauth_gated
from arbora.fleet.session_notifications import SessionCatalogNotifications
from arbora.fleet.state import FleetState
from arbora.fleet.tool_rows import ProbeToolRows, ToolRowReader
from arbora.fleet.workflow_binding import WorkflowBindings
from arbora.pin_lag.unavailable import PinLagUnavailable
from arbora.security.log_redaction import redact_for_log
from arbora.security.run_token import RunTokenSigner

# Direct all logs to stderr so stdout remains perfectly clean for stdio JSON-RPC
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
from arbora.fleet.child_mounts import (
    ChildMounts,
    provider_tools,
)
from arbora.fleet.child_payload_bounds import (
    _bounded_tool_catalog,
    _list_all_tools,
)
from arbora.fleet.child_runtime_policy import (
    _close_runtime_child_policy,
    _prepare_runtime_child_policy,
    _runtime_materialized,
    _validate_externalized_child_secrets,
    attest_runtime_child_config,
)
from arbora.fleet.child_transport import (
    ChildTransport,
    _child_launch_shape,
)
from arbora.fleet.fleet_capabilities import (
    require_fleet_capability,
)
from arbora.fleet.fleet_probe import FleetProbe
from arbora.fleet.proxied_calls import (
    ProxiedCalls,
)
from arbora.fleet.session_keys import (
    ambient_session_key,
)
from arbora.fleet.task_forwarding import (
    TaskForwarding,
)
from arbora.fleet.tool_discovery import ToolDiscovery

logger = logging.getLogger("mcp_multiplexer")
_CONFIG_MAX_BYTES = 4 * 1024 * 1024
_ENGINE_CONFIG_FIELDS = frozenset(
    {
        "transport",
        "headers",
        "oauth_provider",
        "tls_profile",
        "allowed_private_hosts",
        "timeout",
        "call_timeout",
        "max_concurrency",
        "queue_timeout",
        "pool_size",
        "enabledTools",
        "disabledTools",
        # Declares a child tool's owner-stamped service-credential path
        # (ARBORA-FLEET-R015/R021; see arbora.fleet.gateway_ops and
        # arbora.api.domain.fleet.service_child) instead of the caller's own
        # delegated credential. Absent or any other value means "delegated".
        "credential_mode",
        # An operator-declared actor-required connector and the client id its
        # exchanged token is addressed to (ARBORA-IDENTITY-R028.3).
        "requires_actor",
        "client_id",
        # The principal whose provider credential this connector reuses under an engine
        # grant for the calling agent (ARBORA-IDENTITY-R023.2).
        "credential_owner",
    }
)


class _ProbeOnlyCatalogReader:
    """Fail-closed reader for one explicitly supplied, non-serving probe."""

    async def read(self) -> _catalog_reader.FleetCatalog:
        raise RuntimeError("a probe-only multiplexer cannot refresh fleet authority")


def _catalog_error_text(result: _typing.Any) -> str:
    return " ".join(
        str(getattr(item, "text", ""))
        for item in (getattr(result, "content", None) or ())[:8]
    )[:4096].lower()


def _tool_schema_compatible(
    descriptor: dict[str, _typing.Any], relisted_tool: _typing.Any
) -> bool:
    """Positive proof the relisted tool still accepts the exact call in hand.

    The snapshot's own ``inputSchema`` for this tool must be byte-identical
    (via a canonical JSON compare, order-independent) to the LIVE relisted
    tool's ``inputSchema``. A name match alone is not compatibility: a child
    can legitimately redefine a tool's schema between generations while
    keeping its name, and blindly resending the same arguments against a
    changed schema is exactly the "unknown tool" retry's failure mode this
    guards against.
    """
    try:
        expected = json.dumps(
            descriptor.get("inputSchema") or {}, sort_keys=True, default=str
        )
        actual = json.dumps(
            getattr(relisted_tool, "input_schema", None) or {},
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return False
    return expected == actual


# Prefixes are derived 100% algorithmically (no per-server lookup table), so any
# MCP server — bundled or third-party — gets a sensible, unique prefix with zero
# code changes. Pin a specific one per server via the ``prefix`` key in its
# AgentComponent entry; uniqueness across the fleet is then guaranteed by the
# catalog-aware collision resolver (:meth:`MCPMultiplexer._build_prefix_map`).


def _operator_call_timeouts() -> dict[str, _typing.Any]:
    """Per-server ``call_timeout`` from the operator's fleet config (R075).

    The engine owns each server's identity and endpoint; the call deadline is
    operator policy, so a ``call_timeout`` in the deployment's MCP config
    overrides the one the connector declared. An unreadable config yields
    none; an out-of-range value is refused at call time, never clamped.
    """
    from arbora.core.settings import mcp_config_path
    from arbora.fleet.mcp_catalog import parse_mcp_servers

    path = mcp_config_path()
    try:
        servers = parse_mcp_servers(path.read_bytes()) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return {
        name: spec["call_timeout"]
        for name, spec in servers.items()
        if isinstance(spec, dict) and spec.get("call_timeout") is not None
    }


def _decode_engine_server_config(body: bytes) -> dict[str, _typing.Any]:
    """Decode bounded, non-secret transport metadata from one EG component."""
    if len(body) > _CONFIG_MAX_BYTES:
        return {}
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    nested = decoded.get("config")
    if not isinstance(nested, dict):
        nested = decoded.get("mcpServer")
    if isinstance(nested, dict):
        return dict(nested)
    return {
        key: value for key, value in decoded.items() if key in _ENGINE_CONFIG_FIELDS
    }


class MCPMultiplexer:
    """Aggregates and proxies multiple MCP servers over a single stdio connection."""

    #: ARBORA-FLEET-R073.2: the one reader every tool-row site goes through.
    _tool_rows: ToolRowReader = ProbeToolRows()

    def __init__(
        self,
        catalog_reader: _catalog_reader.FleetCatalogSource,
        *,
        fleet: FleetState,
        signer: RunTokenSigner | None = None,
    ):
        self._fleet = fleet
        #: The host's run-token signer; ``None`` on a probe-only multiplexer, which
        #: then refuses to mint a delegation proof.
        self._signer = signer
        self._fleet_catalog_reader = catalog_reader
        self._fleet_catalog: _catalog_reader.FleetCatalog | None = None
        # Wall-clock epoch of the installed snapshot above (ARBORA-FLEET-R031):
        # a tool descriptor read FROM the snapshot (never probed live) stamps
        # its honest age from this, rather than claiming ``now`` like a fresh
        # probe would.
        self._fleet_catalog_loaded_at: float | None = None
        # The last background onboarding pass (arbora.fleet.onboarding).
        self._fleet_onboarding: dict[str, _typing.Any] | None = None
        self._list_changed_tasks: set[_typing.Any] = set()
        self.exit_stack = contextlib.AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        # Per-child hardening layer (CONCEPT:MERISTEM-ECO.mcp.profile-differences-from-client): concurrency limits and
        # bounded queueing live on the _child_resilience.ChildRuntime, not the raw session.
        self.children: dict[str, _child_resilience.ChildRuntime] = {}
        # D-CDX-44: per-server singleflight for the first ``mount_child`` of
        # a not-yet-mounted server. Keyed by server_name; a present entry is
        # the shared future for whichever task is currently the LEADER
        # mounting that server. Never global — different servers mount fully
        # in parallel; only concurrent first-loads of the SAME server share
        # one attempt.
        self._mount_inflight: dict[str, asyncio.Future[list[MCPTool]]] = {}
        self._closing = False
        self._child_runtime_policies: dict[str, _typing.Any] = {}
        self._child_policy_admitted_tools: dict[str, frozenset[str]] = {}
        self._child_catalog_fingerprints: dict[str, str] = {}
        # Bounded digest/revision state for the schemas currently exposed to
        # Arbora clients.  A provider reconnect is cheap to compare against
        # this state and does not trigger a fresh provider probe per call.
        self._child_tool_digests: dict[str, str] = {}
        self._child_schema_revisions: dict[str, int] = {}
        # A schema refresh failure is fail-closed for that child only.  The
        # category is intentionally fixed-vocabulary: raw provider details
        # belong only in the server-side redacted log.
        self._child_schema_refresh_errors: dict[str, str] = {}
        # A child recovery runs on a detached supervisor task, where a request
        # context is unsafe to retain or reuse.  Queue changed exposed schemas
        # for each affected client session and deliver its standard MCP
        # ``tools/list_changed`` notification on that session's next request.
        # Entries are bounded by the existing per-session visibility state and
        # disappear with it, so an idle client cannot accumulate revisions.
        self._session_notifications = SessionCatalogNotifications()
        # Incremented before a hot catalog reload tears down child runtimes so
        # a late callback from an old generation can never repopulate fresh
        # routing state with a stale declaration.
        self._catalog_epoch = 0
        #: The host's credential authority (ARBORA-OPS-R893); ``None`` until bound.
        self.credential_authority: _typing.Any = None
        # Set by ``attach_fleet_loader``.  Keeping this optional preserves the
        # standalone probe and unit-test paths, which do not own a FastMCP
        # server or live forwarders.
        self._host_mcp: _typing.Any | None = None
        self.tool_to_server: dict[
            str, tuple[str, str]
        ] = {}  # prefixed_name -> (server_name, original_name)
        self.aggregated_tools: list[MCPTool] = []
        # CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog — dynamic tool gateway state. The catalog is the
        # full set of mountable servers parsed from config WITHOUT spawning
        # them, so find_tools/load_tools know what exists before any child is
        # started. ``_exposed`` tracks prefixed tool names currently registered
        # as live FastMCP tools (so lazy mounts don't double-register).
        self._catalog: dict[str, dict] | None = None
        # ARBORA-FLEET-R081: the one serving state of the catalog. Cold is not
        # empty: a failed first read leaves it Cold. Written only through
        # ``_catalog_event`` (``arbora.api.domain.fleet.catalog_machine.transition``).
        self._catalog_state = _catalog_machine.CatalogState.COLD
        # ``_exposed`` tracks prefixed tools registered as live FastMCP tools
        # (process-global, so lazy mounts don't double-register). Visibility,
        # however, is PER-SESSION on a shared HTTP server (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog,
        # plan Phase 5): a forwarder is registered once but only listed/callable
        # for sessions that have ``load_tools``-ed it. ``_session_loaded`` maps a
        # session id -> the prefixed names that session has loaded; meta-tools and
        # always-on tools live in ``_global_visible`` and are shown to everyone.
        self._exposed: set[str] = set()
        self._session_loaded: dict[str, set[str]] = {}
        #: ARBORA-FLEET-R021.1: workflow tools need their admitted binding per call.
        self.workflow_bindings = WorkflowBindings()
        self._global_visible: set[str] = set()
        # CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog — self-catalog: per-server {"tools": [...], "error": str|None}
        # learned by probing each child (connect → list_tools → release), cached so
        # find_tools ranks real fleet-wide tools without holding connections and
        # without depending on the (separately-flaky) KG live discovery. Every
        # entry carries ``probed_at`` (epoch seconds of the probe that produced
        # it) so a caller can compute truthful staleness instead of a fleet-wide
        # figure silently being served as if it were live (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).
        self._probe_cache: dict[str, dict] = {}
        # Process-owned discovery authority is deliberately kept out of the
        # public probe payload.  The catalog is caller-visible JSON metadata,
        # while an OAuth grant or tenant-local binding must only reach the
        # internal relational writer after this exact probe completes.  The
        # identity tuple prevents a caller-shaped/copy of an info dict from
        # manufacturing a binding; the bounded map also prevents abandoned
        # probes from retaining authority indefinitely.
        self._discovery_binding_sidechannel: dict[
            int, tuple[str, dict[str, _typing.Any], _typing.Any]
        ] = {}
        # Successful local probes retain their exact verified tenant provenance
        # alongside the cache object.  The one-shot side channel is consumed by
        # source sync; this private record lets a later cache read re-establish
        # authority without relabelling an uninitialized probe.
        self._local_discovery_cache_authority: dict[
            int, tuple[str, dict[str, _typing.Any], _typing.Any]
        ] = {}
        # A server probe that hasn't finished when an interactive caller's
        # budget expires is NEVER cancelled — cancelling it would also cancel
        # the ``self._probe_cache`` write at the end of :meth:`probe_server`,
        # which is exactly why an unreachable/slow server used to be re-probed
        # from scratch on EVERY subsequent call (the timed-out result was
        # discarded, not cached). Instead the task keeps running in the
        # background at its own per-server deadline; this map lets a LATER
        # call join the SAME in-flight probe instead of starting a duplicate,
        # and lets :meth:`aclose` await outstanding probes on shutdown.
        self._probe_inflight: dict[str, asyncio.Task] = {}
        # EVERY live probe task, joinable or not. ``_probe_inflight`` answers
        # "can a later call join this?" and therefore holds at most one task per
        # server; a forced re-probe deliberately is not joinable and so never
        # appears there. :meth:`aclose` needs the other question — "what is
        # still running?" — and must see forced probes too, or one can outlive
        # the multiplexer that spawned it.
        self._probe_tasks: set[asyncio.Task] = set()
        # Server names never mountable as a child of this multiplexer (self +
        # retired aliases) — set post-construction by the arbora fleet loader
        # (:func:`attach_fleet_loader`); defaults to just "mcp-multiplexer" via
        # the ``getattr(..., None) or {...}`` fallback in ``load_catalog``.
        self._skip_servers: set[str] | None = None
        # CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog — catalog-aware, collision-free prefix assignment,
        # computed deterministically over the whole server set so similarly
        # named servers (e.g. scholarx/searxng, both preferring "sx") never
        # share a namespace however the fleet scales. Built lazily from catalog.
        self._prefix_map: dict[str, str] | None = None
        self._prefix_reverse: dict[str, str] = {}
        # CONCEPT:MERISTEM-ECO.mcp.intent-surface-condensed-collapse (Seam 8) — the HOST server's OWN
        # granular tools held back by ``MCP_TOOL_MODE=intent`` (seeded from
        # ``verbose_tools.gated_tool_names`` post-build). Unlike ``_exposed``
        # (fleet forwarders that must be MOUNTED) these are already registered
        # local FastMCP tools that only need a session-visibility flip — no
        # child process, no ``resolve_and_mount``. ``load_tools`` reveals them
        # the same way it reveals a fleet tool: add to ``_session_loaded``.
        self._local_gated: set[str] = set()
        # CONCEPT:MERISTEM-ECO.mcp.intent-surface-tool-lifecycle (Seam 8) — session -> tool names
        # ``load_tools(..., auto_unload=True)`` marked for automatic retraction the
        # NEXT time they're called (one-shot: load -> use -> auto-unload), so a
        # long session's tool surface doesn't monotonically grow.
        self._auto_unload: dict[str, set[str]] = {}
        self._authority_scope: _typing.Any = None
        # Serving composition replaces this no-op with the process singleton's
        # event-loop claim. Standalone multiplexers retain local-loop behavior.
        self._claim_serving_loop: _typing.Callable[[], None] = lambda: None
        # CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog — the ONE bounded eager posture.
        # Catalog server names (``MCP_ALWAYS_LOAD``) and server-qualified or
        # prefixed tool names (``MCP_ALWAYS_LOAD_TOOLS``) that are mounted on a
        # session's FIRST contact instead of costing a ``find_tools`` round
        # trip. Populated by :func:`attach_fleet_loader` from AgentConfig;
        # empty (fully lazy) for a bare multiplexer.
        self._always_load_servers: list[str] = []
        self._always_load_tool_specs: list[str] = []
        # Per-session "already attempted" marker. Eager mounting runs at most
        # once per session even when every entry failed — a fleet outage must
        # not turn every tools/list into a fresh round of doomed connections.
        self._always_load_done: dict[str, dict[str, _typing.Any]] = {}
        # CONCEPT:MERISTEM-ECO.mcp.stable-dispatch-bounded-retry — `(server_name,
        # child_connection_generation)` -> retries already spent on an
        # "unknown tool" relist-and-retry for that exact child incarnation
        # (`_retry_read_only_unknown`). A single `dispatch_catalog_tool` call
        # only ever issues one retry, but nothing previously stopped a caller
        # from re-invoking dispatch repeatedly against a child that keeps
        # reporting the same tool unknown — this budget makes the total
        # retries per child generation finite instead of relying on caller
        # good behavior.
        self.transport = ChildTransport(
            child_catalog_fingerprints=self._child_catalog_fingerprints,
            child_policy_admitted_tools=self._child_policy_admitted_tools,
            fleet=self._fleet,
            list_changed_tasks=self._list_changed_tasks,
            probe_cache=self._probe_cache,
            publish_live_tools=lambda *a, **k: self.mounts._publish_live_tools(*a, **k),
        )
        #: Installs the callback that schedules onboarding after a handshake.
        self.set_handshake_listener = self.transport.set_handshake_listener
        self.tasks = TaskForwarding(
            fleet=self._fleet,
            signer=self._signer,
            children=self.children,
            catalog_epoch=lambda: self._catalog_epoch,
            load_catalog=lambda *a, **k: self.load_catalog(*a, **k),
            mount_child=lambda *a, **k: self.mounts.mount_child(*a, **k),
        )
        self.calls = ProxiedCalls(
            child_policy_admitted_tools=self._child_policy_admitted_tools,
            child_runtime_policies=self._child_runtime_policies,
            child_schema_refresh_errors=self._child_schema_refresh_errors,
            child_schema_revisions=self._child_schema_revisions,
            fleet=self._fleet,
            children=self.children,
            workflow_bindings=self.workflow_bindings,
            catalog=lambda: self._catalog,
            catalog_epoch=lambda: self._catalog_epoch,
            host_mcp=lambda: self._host_mcp,
            aggregated_tools=lambda: self.aggregated_tools,
            credential_authority=lambda: self.credential_authority,
            tool_to_server=lambda: self.tool_to_server,
            admit_runtime_policy_tools=lambda *a, **k: (
                self.transport._admit_runtime_policy_tools(*a, **k)
            ),
            open_one_session=lambda *a, **k: self.transport._open_one_session(*a, **k),
            load_catalog=lambda *a, **k: self.load_catalog(*a, **k),
            notify_pending_tools_changed=lambda *a, **k: (
                self.mounts.notify_pending_tools_changed(*a, **k)
            ),
            tool_dispatchable=lambda *a, **k: self.tool_dispatchable(*a, **k),
        )
        self.probe = FleetProbe(
            fleet=self._fleet,
            children=self.children,
            sessions=self.sessions,
            transport=self.transport,
            probe_cache=self._probe_cache,
            probe_inflight=self._probe_inflight,
            probe_tasks=self._probe_tasks,
            child_policy_admitted_tools=self._child_policy_admitted_tools,
            discovery_binding_sidechannel=self._discovery_binding_sidechannel,
            local_discovery_cache_authority=self._local_discovery_cache_authority,
            fleet_catalog=lambda: self._fleet_catalog,
            fleet_catalog_loaded_at=lambda: self._fleet_catalog_loaded_at,
            tool_rows=lambda: self._tool_rows,
            mounts=lambda: self.mounts,
            load_catalog=lambda *a, **k: self.load_catalog(*a, **k),
        )
        self.discovery = ToolDiscovery(
            fleet=self._fleet,
            children=self.children,
            exposed=self._exposed,
            probe_cache=self._probe_cache,
            probe=self.probe,
            mounts=lambda: self.mounts,
            tool_to_server=lambda: self.tool_to_server,
            tool_rows=lambda: self._tool_rows,
            load_catalog=lambda *a, **k: self.load_catalog(*a, **k),
            tool_dispatchable=lambda *a, **k: self.tool_dispatchable(*a, **k),
        )
        self.mounts = ChildMounts(
            governed_fleet=lambda: getattr(self, "_governed_fleet", None),
            always_load_servers=self._always_load_servers,
            always_load_tool_specs=self._always_load_tool_specs,
            auto_unload=self._auto_unload,
            child_runtime_policies=self._child_runtime_policies,
            child_schema_refresh_errors=self._child_schema_refresh_errors,
            child_schema_revisions=self._child_schema_revisions,
            child_tool_digests=self._child_tool_digests,
            exposed=self._exposed,
            fleet=self._fleet,
            mount_inflight=self._mount_inflight,
            probe_cache=self._probe_cache,
            session_loaded=self._session_loaded,
            session_notifications=self._session_notifications,
            children=self.children,
            sessions=self.sessions,
            catalog_epoch=lambda: self._catalog_epoch,
            catalog_state=lambda: self._catalog_state,
            host_mcp=lambda: self._host_mcp,
            prefix_map=lambda: self._prefix_map,
            prefix_reverse=lambda: self._prefix_reverse,
            tool_rows=lambda: self._tool_rows,
            aggregated_tools=lambda: self.aggregated_tools,
            tool_to_server=lambda: self.tool_to_server,
            admit_runtime_policy_tools=lambda *a, **k: (
                self.transport._admit_runtime_policy_tools(*a, **k)
            ),
            announce_handshake=lambda *a, **k: self.transport._announce_handshake(
                *a, **k
            ),
            catalog_event=lambda *a, **k: self._catalog_event(*a, **k),
            drop_discovery_bindings_for_server=lambda *a, **k: (
                self.probe._drop_discovery_bindings_for_server(*a, **k)
            ),
            live_primary_session=lambda *a, **k: self.probe._live_primary_session(
                *a, **k
            ),
            probe_prompts=lambda *a, **k: self.probe._probe_prompts(*a, **k),
            probe_skills=lambda *a, **k: self.probe._probe_skills(*a, **k),
            relay_read=lambda *a, **k: self.probe._relay_read(*a, **k),
            start_child=lambda *a, **k: self._start_child(*a, **k),
            store_aggregated_tools=lambda *a, **k: self._store_aggregated_tools(
                *a, **k
            ),
            store_prefix_maps=lambda *a, **k: self._store_prefix_maps(*a, **k),
            store_tool_to_server=lambda *a, **k: self._store_tool_to_server(*a, **k),
            call_proxied_tool=lambda *a, **k: self.call_proxied_tool(*a, **k),
            load_catalog=lambda *a, **k: self.load_catalog(*a, **k),
            probe_server=lambda *a, **k: self.probe.probe_server(*a, **k),
            prune_session_visibility=lambda *a, **k: self.prune_session_visibility(
                *a, **k
            ),
            tool_object=lambda *a, **k: self.tool_object(*a, **k),
        )
        fleet.multiplexers.add(self)

    def server_prefix(self, server_name: str) -> str:
        """The unique, collision-free prefix for a server (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)."""
        return self.mounts.server_prefix(server_name)

    async def notify_pending_tools_changed(self) -> bool:
        """Deliver this live session's queued ``tools/list_changed`` event."""
        return await self.mounts.notify_pending_tools_changed()

    async def mount_child(self, server_name: str) -> list[MCPTool]:
        """Start ONE configured child on demand and register its tools"""
        return await self.mounts.mount_child(server_name)

    def bind_tool_rows(self, reader: ToolRowReader) -> None:
        """Bind the one reader every tool-row site goes through (ARBORA-FLEET-R073.2)."""
        self._tool_rows = reader

    async def delegated_server_tools(
        self, server_name: str
    ) -> list[dict[str, _typing.Any]]:
        """Return one child's bounded tools through the served probe cache."""
        return await self.mounts.delegated_server_tools(server_name)

    async def delegate_server_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, _typing.Any],
        timeout: float,
    ) -> _typing.Any:
        """Invoke one original child tool through this served child pool."""
        return await self.mounts.delegate_server_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            timeout=timeout,
        )

    async def read_server_resource(
        self, *, server_name: str, uri: str, timeout: float
    ) -> dict[str, str]:
        """Read one text resource through this served child connection."""
        return await self.mounts.read_server_resource(
            server_name=server_name, uri=uri, timeout=timeout
        )

    async def get_server_prompt(
        self, *, server_name: str, name: str, arguments: dict[str, str], timeout: float
    ) -> dict[str, _typing.Any]:
        """Render one child prompt through this served child connection."""
        return await self.mounts.get_server_prompt(
            server_name=server_name, name=name, arguments=arguments, timeout=timeout
        )

    def prefixed_tools_for_server(self, server_name: str) -> list[MCPTool]:
        """All aggregated prefixed tools currently owned by ``server_name``."""
        return self.mounts.prefixed_tools_for_server(server_name)

    async def start_children(self):
        """Parse configuration and start all child processes concurrently"""
        return await self.mounts.start_children()

    def always_load_tool_owner(self, spec: str) -> tuple[str | None, str | None]:
        """Resolve one ``MCP_ALWAYS_LOAD_TOOLS`` entry to ``(server, original)``."""
        return self.mounts.always_load_tool_owner(spec)

    def prefixed_for_original(self, server_name: str, original: str) -> str | None:
        """The aggregated prefixed name a child's ORIGINAL tool name maps to."""
        return self.mounts.prefixed_for_original(server_name, original)

    def always_load_declared(self) -> bool:
        """True when this multiplexer has any eager always-load declaration."""
        return self.mounts.always_load_declared()

    async def call_proxied_tool(
        self, prefixed_name: str, arguments: dict[str, _typing.Any] | None = None
    ) -> MCPCallToolResult:
        """Forward a prefixed tool call to the owning child server's session."""
        return await self.calls.call_proxied_tool(prefixed_name, arguments)

    async def call_admitted_child_once(
        self,
        server_name: str,
        original_name: str,
        arguments: dict[str, _typing.Any] | None = None,
        *,
        timeout: float | None = None,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """Make exactly one governed child call over a dedicated session."""
        return await self.calls.call_admitted_child_once(
            server_name,
            original_name,
            arguments,
            timeout=timeout,
            approval_ref=approval_ref,
        )

    async def call_oauth_gated_tool(
        self,
        server_name: str,
        original_name: str,
        arguments: dict[str, _typing.Any] | None = None,
        *,
        timeout: float | None = None,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """Per-principal tool call for an OAuth-gated remote MCP child (NE-008,"""
        return await self.calls.call_oauth_gated_tool(
            server_name,
            original_name,
            arguments,
            timeout=timeout,
            approval_ref=approval_ref,
        )

    async def forward_task_method(
        self,
        method: str,
        params: _collections_abc.Mapping[str, _typing.Any],
        *,
        caller: _collections_abc.Mapping[str, _typing.Any] | None = None,
        route: _collections_abc.Mapping[str, _typing.Any] | None = None,
    ) -> _typing.Any:
        """Forward one native Tasks request to its owning child server."""
        return await self.tasks.forward_task_method(
            method, params, caller=caller, route=route
        )

    def _store_prefix_maps(
        self, forward: dict[str, str], reverse: dict[str, str]
    ) -> None:
        """Replace the server-to-prefix map and its reverse."""
        self._prefix_map = forward
        self._prefix_reverse = reverse

    def _store_tool_to_server(self, mapping: dict[str, tuple[str, str]]) -> None:
        """Replace the prefixed-name to (server, original name) map."""
        self.tool_to_server = mapping

    def _store_aggregated_tools(self, rows: list[MCPTool]) -> None:
        """Replace the aggregated tool rows."""
        self.aggregated_tools = rows

    @property
    def fleet(self) -> FleetState:
        """The host context's fleet state this multiplexer was built with."""
        return self._fleet

    @staticmethod
    def _catalog_config_from_server(
        server: _catalog_reader.CatalogServer,
    ) -> dict[str, _typing.Any]:
        """Project one verified EG server row into a bounded child config.

        Registration owns the live endpoint.  The optional server component
        body may carry non-secret transport policy (for example an OAuth
        provider reference), but it is never trusted for endpoint identity or
        allowed to replace the engine-owned URL.  Invalid/non-JSON content is
        therefore a valid metadata shape with the minimal remote transport.
        """
        config = _decode_engine_server_config(server.content.body)
        if server.registration is None:
            raise RuntimeError("unavailable fleet server has no runnable endpoint")
        config["url"] = server.registration.url
        config.setdefault("transport", "streamable-http")
        if server.registration.resources:
            config.setdefault("resources", dict(server.registration.resources))
        return config

    @classmethod
    def _catalog_from_engine(
        cls, catalog: _catalog_reader.FleetCatalog
    ) -> dict[str, dict[str, _typing.Any]]:
        """Build the loader's child declarations from one EG catalog snapshot."""
        return {
            server.registration.name: cls._catalog_config_from_server(server)
            for server in catalog.servers
            if server.registration is not None
        }

    @staticmethod
    async def _publish_reconciled(
        reader: _typing.Any, catalog: _catalog_reader.FleetCatalog
    ) -> None:
        """Swap the candidate in as the active generation through the receipt guard.

        The receipt is fetched from the engine (ARBORA-FLEET-R051.2.2). An
        unreconciled candidate raises ``PublicationRefusedError`` and the
        last-known-good generation keeps serving. PIN-LAG: a pinned engine that
        cannot issue the receipt installs the read unpublished and logs it.
        """
        publish = getattr(reader, "publish_from_engine", None)
        if publish is None or catalog.generation is None:
            return
        try:
            await publish(catalog, now_ms=int(time.time() * 1000))
        except PinLagUnavailable as unavailable:
            logger.warning("catalog generation not published: %s", unavailable)

    async def refresh_engine_catalog(self) -> dict[str, dict[str, _typing.Any]]:
        """Read and publish the authoritative EG fleet catalog.

        This is the asynchronous composition seam for arbora startup.  The
        The first successful refresh installs the verified snapshot and all
        prefix/discovery state is rebuilt from it.
        """
        reader = self._fleet_catalog_reader
        self._catalog_event(_catalog_machine.CatalogEvent.LOAD_START)
        try:
            catalog = await reader.read()
            if self.children or self.sessions:
                raise RuntimeError(
                    "the engine fleet catalog can only be refreshed before child startup"
                )
            await self._publish_reconciled(reader, catalog)
        except BaseException:
            self._catalog_event(_catalog_machine.CatalogEvent.READ_ERR)
            raise
        self._fleet_catalog = catalog
        self._fleet_catalog_loaded_at = time.time()
        raw_catalog = self._catalog_from_engine(catalog)
        for server_name, call_timeout in _operator_call_timeouts().items():
            if server_name in raw_catalog:
                raw_catalog[server_name]["call_timeout"] = call_timeout
        skip = getattr(self, "_skip_servers", None) or {"mcp-multiplexer"}
        self._catalog = {}
        for server_name, config in raw_catalog.items():
            if not self._catalog_entry_admissible(server_name, config, skip):
                continue
            admitted = self._admit_catalog_entry(server_name, config, False)
            if admitted is not None:
                self._catalog[server_name] = admitted
        self._prefix_map = None
        self._prefix_reverse.clear()
        self._catalog_epoch += 1
        self._catalog_event(_catalog_machine.CatalogEvent.READ_OK)
        return self._catalog

    def _catalog_event(self, event: _catalog_machine.CatalogEvent) -> None:
        """Send one event to the catalog serving machine (ARBORA-FLEET-R081.4)."""
        self._catalog_state = _catalog_machine.transition(
            self._catalog_state, event, has_snapshot=self._catalog is not None
        )

    @property
    def catalog_state(self) -> _catalog_machine.CatalogState:
        """The serving state ``fleet.status`` and readiness derive from."""
        return self._catalog_state

    async def _start_child(
        self, server_name: str, cfg: dict
    ) -> tuple[str, _child_resilience.ChildRuntime, list[MCPTool], dict] | None:
        """Starts a single child server, registers its exit stack on success, and returns its tools and runtime."""
        # NE-008 (GOC-85 Deliverable 3): an OAuth-gated child (``oauth_provider``
        # declared on its catalog entry) is NEVER pool-mounted -- this is the ONE
        # shared, principal-agnostic ``_child_resilience.ChildRuntime``/session-per-server pool
        # every caller draws from regardless of who they are, which is exactly
        # the sharing the U-44/U-45 canary exists to prevent for a per-user
        # grant. Such a server is served exclusively through the ephemeral,
        # per-request session path (``probe_server`` for discovery,
        # :meth:`call_oauth_gated_tool` for calls) -- both call
        # :meth:`_open_one_session` directly and never reach here.
        if oauth_gated(cfg):
            logger.info(
                "MCP child is OAuth-gated (oauth_provider); never pool-mounted, "
                "served only via ephemeral per-principal sessions"
            )
            return None
        # Preserve the catalog generation that authorized this spawn.  A hot
        # reload may run while the child is handshaking; later callbacks from
        # that retired runtime must not re-populate the new catalog's state.
        catalog_epoch = self._catalog_epoch
        try:
            cfg, runtime_policy = _prepare_runtime_child_policy(cfg, self._fleet.config)
        except RuntimeError:
            logger.error("MCP child runtime policy resolution failed")
            return None

        def _close_policy() -> None:
            if runtime_policy is not None:
                _close_runtime_child_policy(runtime_policy)

        shape = _child_launch_shape(cfg, self._fleet.config)
        if shape is None:
            _close_policy()
            return None
        is_remote, pool_size = shape

        logger.info(
            "Starting MCP child (transport=%s)", "remote" if is_remote else "stdio"
        )

        async def _connect_one(
            stack: contextlib.AsyncExitStack, generation_secret: str | None
        ):
            return await self.transport._open_one_session(
                server_name, cfg, stack, generation_secret
            )

        async def _connect(stack: contextlib.AsyncExitStack):
            """One connection generation: full session pool + tool list.

            The stack is owned by the runtime's supervisor task (entered and
            exited there), so each crash/restart cleanly tears down and
            rebuilds every transport of the generation."""
            generation_secret = secrets.token_urlsafe(48) if not is_remote else None
            runtime._task_generation_secret = generation_secret
            sessions = [
                await _connect_one(stack, generation_secret) for _ in range(pool_size)
            ]
            tools = await _list_all_tools(sessions[0])
            _bounded_tool_catalog(tools)
            await self.transport._announce_handshake(server_name, sessions[0], tools)
            if runtime_policy is not None:
                tools = self.transport._admit_runtime_policy_tools(
                    server_name,
                    runtime_policy,
                    tools,
                    record_state=catalog_epoch == self._catalog_epoch,
                )
            return sessions, tools

        # Service-authenticated remote children must recycle their session before
        # the bearer's TTL elapses (the result stream is authed once at connect
        # and then wedges on expiry); derive that lifetime from the token TTL.
        session_max_age: float | None = None
        if is_remote:
            from arbora.security.client_credentials import (
                service_session_max_age,
            )

            session_max_age = service_session_max_age(
                cfg.get("headers"), self._fleet.config
            )
        runtime: _child_resilience.ChildRuntime

        async def _on_generation(live_tools: list[_typing.Any]) -> None:
            await self.mounts._refresh_child_tools(
                server_name,
                runtime,
                cfg,
                catalog_epoch,
                _typing.cast("list[MCPTool]", live_tools),
            )

        runtime = _child_resilience.ChildRuntime(
            server_name,
            cfg,
            connect=_connect,
            session_max_age=session_max_age,
            on_generation=_on_generation,
            confirmations=self._fleet.confirmations,
            config=self._fleet.config,
        )
        try:
            tools = await runtime.start()
        except TimeoutError:
            logger.error("MCP child startup timed out")
            _close_policy()
            return None
        except Exception as exc:
            logger.error(
                "Failed to start MCP child: %s: %s",
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
            _close_policy()
            return None

        if runtime_policy is not None:
            self._child_runtime_policies[server_name] = runtime_policy

        logger.info(
            "Loaded %d tools from MCP child (%d session%s)",
            len(tools),
            pool_size,
            "" if pool_size == 1 else "s",
        )
        return server_name, runtime, tools, cfg

    @staticmethod
    def _catalog_entry_admissible(
        server_name: _typing.Any, cfg: _typing.Any, skip: _typing.Any
    ) -> bool:
        """Whether one raw config entry is even shaped like a mountable child."""
        return (
            isinstance(server_name, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", server_name) is not None
            and isinstance(cfg, dict)
            and len(cfg) <= 128
            and server_name not in skip
            and not cfg.get("disabled", False)
        )

    @staticmethod
    def _admit_catalog_entry(
        server_name: str, cfg: dict, runtime_materialized: bool
    ) -> dict | None:
        """The catalog config for ONE entry, or ``None`` when it is not mountable."""
        from arbora.core.transport import is_self_url as _is_self_mcp_url
        from arbora.observability.langfuse_trust import (
            LangfuseTrustError,
            is_langfuse_server,
            prepare_langfuse_mcp_config,
        )

        # Never surface a self-entry as a mountable child, regardless of the name it
        # is filed under. ``skip`` covers it by NAME ("arbora"), but a fresh
        # ``MCPMultiplexer`` built off the raw config (e.g. by ``_fleet_server_url``)
        # has not had ``attach_fleet_loader`` widen ``skip``. Match by the process's
        # own advertised identity (config-driven) so the gateway's own endpoint is
        # never dialed as if it were a fleet child — it is fronted in-process instead.
        if _is_self_mcp_url(str(cfg.get("url") or "")):
            return None
        if not runtime_materialized:
            try:
                _validate_externalized_child_secrets(cfg)
            except RuntimeError:
                logger.error("MCP child disabled: credential policy violation")
                return None
        if not is_langfuse_server(str(server_name), cfg) or not cfg.get("command"):
            # Only a child Arbora launches itself receives projected Langfuse
            # settings. A remote child (``url``, no ``command``) holds its own
            # credentials in its own deployment; Arbora has nothing to project
            # into it, so requiring Langfuse keys here disabled a healthy child.
            return cfg
        try:
            if not runtime_materialized:
                cfg = prepare_langfuse_mcp_config(cfg)
            return attest_runtime_child_config(cfg)
        except LangfuseTrustError as exc:
            # Category and reason are fixed-vocabulary, non-sensitive labels.
            category = exc.category
            reason = exc.reason
            logger.error(
                "Langfuse MCP entry disabled: %s configuration invalid (%s)",
                category,
                reason,
            )
            return None

    def load_catalog(self) -> dict[str, dict]:
        """Parse the config once into the mountable-server catalog WITHOUT
        spawning any child (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        Idempotent: the parsed ``{server_name: cfg}`` map is cached on
        ``self._catalog`` and reused. Self (``mcp-multiplexer``) and entries
        flagged ``disabled`` are excluded — they are never mountable. A
        multiplexer with an EG reader is intentionally empty until its async
        ``refresh_engine_catalog`` seam has installed a verified snapshot; it
        never falls back to the static file.
        """
        if self._catalog is not None:
            return self._catalog

        self._catalog = {}
        return self._catalog

    # ------------------------------------------------------------------
    # Always-load (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Dynamic tool gateway (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)
    # ------------------------------------------------------------------

    async def probe_server(
        self, server_name: str, force: bool = False, timeout: float | None = None
    ) -> dict:
        """Probe ONE catalog server for its tool list: connect → list_tools → release (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). Returns (and caches) ``{"tools": [...], "error": str|None, "probed_at": float}``. An already-mounted child reuses its live tools instead of reconnecting; an unreachable server records its error string (so find_tools/load_tools can report *why* it is unavailable) rather than raising."""
        return await self.probe.probe_server(server_name, force, timeout)

    @classmethod
    async def probe_declaration(
        cls,
        server_name: str,
        declaration: dict[str, _typing.Any],
        *,
        timeout: float,
        fleet: FleetState,
    ) -> dict[str, _typing.Any]:
        """Probe one in-memory declaration through the canonical child boundary.

        KG ingestion and Arbora fleet discovery intentionally share this entry
        point.  Declarations never pass through an alternate raw MCP client:
        credential externalization, AgentConfig TLS profiles, pinned egress,
        redirect denial, bounded stdio delegation, auth, time, and catalog
        limits are therefore identical on both paths.
        """

        if (
            not isinstance(server_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", server_name)
            or not isinstance(declaration, dict)
            or len(declaration) > 128
        ):
            raise RuntimeError("MCP child declaration is invalid")
        materialized = _runtime_materialized(declaration)
        if not materialized:
            _validate_externalized_child_secrets(declaration)
        multiplexer = cls(_ProbeOnlyCatalogReader(), fleet=fleet)
        multiplexer._catalog = {server_name: dict(declaration)}
        try:
            return await multiplexer.probe_server(
                server_name,
                force=True,
                timeout=timeout,
            )
        finally:
            await multiplexer.aclose()

    async def probe_catalog(
        self,
        force: bool = False,
        timeout: float | None = None,
        budget: float | None = None,
        servers: list[str] | tuple[str, ...] | None = None,
        priority: _resource_priority.PriorityClass | None = None,
    ) -> dict[str, dict]:
        """Probe every catalog server concurrently (bounded) and cache the result, so find_tools can rank the whole fleet's real tools."""
        return await self.probe.probe_catalog(force, timeout, budget, servers, priority)

    async def discover_tools(
        self, query: str, top_k: int | None = None, loaded: set[str] | None = None
    ) -> dict:
        """Rank candidate tools across the whole fleet for an NL ``query`` (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog), without exposing or holding any child."""
        return await self.discovery.discover_tools(query, top_k, loaded)

    async def list_catalog(self, server: str = "", include_tools: bool = True) -> dict:
        """Browse configured fleet metadata without unnecessary child starts."""
        return await self.discovery.list_catalog(server, include_tools)

    async def resolve_and_mount(
        self, tools: list[str] | None = None, servers: list[str] | None = None
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Mount whatever children are needed to satisfy a ``load_tools`` request and compute the set of prefixed names to expose."""
        return await self.discovery.resolve_and_mount(tools, servers)

    def tool_object(self, prefixed_name: str) -> MCPTool | None:
        """The aggregated ``_fastmcp_tools.Tool`` object for a prefixed name, if known."""
        for tool in self.aggregated_tools:
            if tool.name == prefixed_name:
                return tool
        return None

    def forget_tool(self, prefixed_name: str) -> str | None:
        """Drop a prefixed tool from the aggregation maps (used by unload).
        Returns the owning server name, if any."""
        owner = None
        mapping = self.tool_to_server.pop(prefixed_name, None)
        if mapping:
            owner = mapping[0]
        self.aggregated_tools = [
            t for t in self.aggregated_tools if t.name != prefixed_name
        ]
        self._exposed.discard(prefixed_name)
        return owner

    def require_capability(self, kind: str) -> None:
        """Authorize one fleet operation kind (``discover``/``delegate``) for the
        current caller — the check the intent tools apply before they search
        or call the fleet."""
        require_fleet_capability(kind, self._fleet.config)

    def session_loaded(self, session_key: str) -> set[str]:
        """The set of prefixed tools loaded (visible) for one session."""
        return self._session_loaded.setdefault(session_key, set())

    def is_serving(self) -> bool:
        """Whether this instance's catalog actually names at least one server.

        CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog — checks truthiness
        (non-empty), not merely ``self._catalog is not None``: calling
        :meth:`tool_dispatchable` on ANY instance — including a
        freshly-constructed, never-served one (what ``source_sync``/a fleet
        harvest builds standalone, D-SH-6, ``reports/deferred/
        lane-skill-harvest.md``) — reaches :meth:`server_for_prefixed`,
        which lazily calls :meth:`load_catalog` as a side effect via
        :meth:`_build_prefix_map`. That side effect turns ``self._catalog``
        from ``None`` into (at least) ``{}``, so an ``is not None`` check
        would already read ``True`` by the time this runs — it is the
        catalog being genuinely EMPTY (verified live in-pod: a harvest's
        throwaway instance resolved zero servers for both real probed tool
        names and an invented one) that distinguishes a non-serving instance,
        not whether ``load_catalog`` merely ran.
        """
        return bool(self._catalog)

    def tool_dispatchable(
        self, prefixed_name: str, *, session_key: str | None = None
    ) -> bool:
        """Whether ``prefixed_name`` may dispatch: its workflow binding holds
        (ARBORA-FLEET-R021.1) and the catalog says it is callable now."""
        if self.calls._workflow_refusal(prefixed_name, session_key):
            return False
        return self._catalog_dispatchable(prefixed_name, session_key=session_key)

    def _catalog_dispatchable(
        self, prefixed_name: str, *, session_key: str | None = None
    ) -> bool:
        """Whether ``prefixed_name`` is ACTUALLY callable right now for a session.

        Single source of truth for "can this session dispatch this tool" —
        the exact predicate :class:`SessionVisibilityMiddleware` enforces at
        the ``tools/call`` gate. Every status-reporting surface (``list_catalog``,
        ``find_tools``, ``load_tools``) MUST derive its "mounted"/"loaded" claims
        from this one function instead of re-deriving the same logic against a
        parallel bookkeeping structure (e.g. ``server in self.children``, which
        only reflects the CHILD PROCESS being up, not whether THIS session may
        call one of its tools) — that divergence is exactly the control-plane
        truthfulness bug this exists to close: a caller must never be told a
        tool is usable when the dispatch gate would reject it.

        D-SH-6 (``reports/deferred/lane-skill-harvest.md``): on a
        freshly-constructed instance that never loaded a catalog
        (:meth:`is_serving` is ``False``), ``_global_visible``/
        ``_local_gated``/``server_for_prefixed`` all resolve nothing for
        EVERY name — including an invented one — so this used to fall
        through to the final "unknown to our bookkeeping" branch
        unconditionally, re-creating the exact mounted-vs-callable lie the
        reconciliation gate exists to close, one layer up. That final
        branch's premise (an out-of-band native host tool the real dispatch
        middleware would also allow through) only holds for an instance
        that is actually serving; a non-serving instance has no dispatch
        middleware running at all, so there is nothing to defer to — refuse
        rather than default-open.
        """
        if prefixed_name in self._global_visible:
            return True
        if prefixed_name in self._local_gated:
            key = session_key if session_key is not None else ambient_session_key()
            return prefixed_name in self._session_loaded.get(key, set())
        # ``server_for_prefixed`` resolves ownership from the catalog's
        # collision-free prefix map, which works even BEFORE the owning child
        # is mounted — so this branch also catches a probed-but-never-mounted
        # fleet tool, not just an already-mounted one.
        if self.mounts.server_for_prefixed(prefixed_name) is not None:
            # A KNOWN fleet tool (its owning server is at least catalogued,
            # whether or not it has been mounted yet) is only dispatchable
            # once ``load_tools`` actually registered a live forwarder for it
            # (``_exposed``) AND this session has it loaded. Being merely
            # catalogued/mountable is not enough — that gap (catalog says it
            # exists vs. a forwarder was ever built) is exactly what let
            # ``list_catalog``/``find_tools`` claim a tool was usable before
            # any session had loaded it.
            if prefixed_name not in self._exposed:
                return False
            key = session_key if session_key is not None else ambient_session_key()
            return prefixed_name in self._session_loaded.get(key, set())
        if not self.is_serving():
            return False
        # Unknown to the catalog/probe/session-load bookkeeping entirely. The
        # only remaining legitimate case is a native tool registered directly
        # on the host FastMCP server outside the progressive-disclosure
        # surface (e.g. one of this module's own meta-tools, or a host tool
        # arbora serves natively) — verify it is ACTUALLY registered there
        # rather than assume some other layer admits it. No host bound at all
        # means nothing here can vouch for the name, so it is denied too.
        if self._host_mcp is None:
            return False
        return prefixed_name in provider_tools(self._host_mcp)

    def prune_session_visibility(self, session_key: str) -> None:
        """Drop empty per-session visibility state after explicit retraction."""
        if not self._auto_unload.get(session_key):
            self._auto_unload.pop(session_key, None)
        if not self._session_loaded.get(session_key):
            self._auto_unload.pop(session_key, None)
            # Keep an empty visibility record only while a detached recovery
            # still owes this client a schema-removal notification. The record
            # is removed by ``notify_pending_tools_changed`` after delivery.
            if not self._session_notifications.has_pending(session_key):
                self._session_loaded.pop(session_key, None)

    def requested_prefixed(
        self, tools: list[str] | None, servers: list[str] | None
    ) -> list[str]:
        """All catalog prefixed names a ``load_tools`` request resolves to.

        Unlike ``resolve_and_mount`` (which returns only the *newly registerable*
        names), this is the FULL set the requesting session should see — including
        tools another session already registered. Call after ``resolve_and_mount``
        so the owning children are mounted and known.
        """
        wanted: set[str] = set(tools or [])
        for server in servers or []:
            wanted.update(t.name for t in self.mounts.prefixed_tools_for_server(server))
        return sorted(n for n in wanted if n in self.tool_to_server)

    def _mounted_tool_counts(self) -> dict[str, int]:
        """Live forwarding tools currently registered, per child server.

        ``_exposed`` holds the prefixed names registered as real FastMCP tools;
        ``tool_to_server`` resolves each back to its child. In dynamic mode a
        catalogued-but-unmounted child legitimately counts zero.
        """
        counts: dict[str, int] = {name: 0 for name in self.children}
        for prefixed in self._exposed:
            entry = self.tool_to_server.get(prefixed)
            if entry is not None:
                counts[entry[0]] = counts.get(entry[0], 0) + 1
        return counts

    def _unadmitted_servers(self) -> list[str]:
        """Live registrations the last EG catalog read could not admit."""
        catalog = self._fleet_catalog
        return [] if catalog is None else list(catalog.unadmitted)

    def status_snapshot(self) -> dict[str, _typing.Any]:
        """Fleet health surface: per-child state, limits, load, restarts.

        CONCEPT:MERISTEM-ECO.multiplexer.running-vs-dispatchable-metrics — this is the
        ONE producer of child health, rendered two ways: the
        ``multiplexer_status`` tool returns the dict, and the Prometheus child
        gauges are published from the very same dict on the way out, so the
        scrape and the tool can never disagree. ``mounted_tools`` makes the
        process-up / tools-callable distinction explicit in the payload instead
        of leaving it implied by a child's absence from ``children``.
        """
        mounted = self._mounted_tool_counts()
        snapshot = {
            "catalog_source": "medulla",
            "catalog_state": self._catalog_state.value,
            "children": {
                name: {
                    **runtime.status(),
                    "mounted_tools": mounted.get(name, 0),
                    "catalog_revision": self._child_schema_revisions.get(name, 0),
                    **(
                        {"catalog_fingerprint": self._child_catalog_fingerprints[name]}
                        if name in self._child_catalog_fingerprints
                        else {}
                    ),
                    **(
                        {
                            "catalog_refresh_error": self._child_schema_refresh_errors[
                                name
                            ]
                        }
                        if name in self._child_schema_refresh_errors
                        else {}
                    ),
                }
                for name, runtime in sorted(self.children.items())
            },
            "total_children": len(self.children),
            "total_tools": len(self.aggregated_tools),
            # Children the release gate refused, with the typed refusal code.
            "release_refusals": dict(sorted(self.transport.release_refusals.items())),
            "unadmitted_servers": self._unadmitted_servers(),
            "fleet_onboarding": self._fleet_onboarding,
        }
        try:
            from arbora.observability.gateway_metrics import (
                publish_multiplexer_child_gauges,
            )

            publish_multiplexer_child_gauges(snapshot)
        except Exception as exc:
            # Metrics must never break the health surface — but never silently:
            # a health snapshot that stopped feeding the scrape is exactly the
            # kind of blind spot these gauges exist to remove.
            logger.warning(
                "Could not publish multiplexer child gauges (exception_type=%s): %s",
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
        return snapshot

    async def aclose(self) -> None:
        """Shut down every child runtime and direct stack registration."""
        self._closing = True
        if self._catalog_state is not _catalog_machine.CatalogState.CLOSING:
            self._catalog_event(_catalog_machine.CatalogEvent.CLOSE)
        # Background catalog probes (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog) are
        # deliberately left running past an interactive call's own budget so a
        # later call can benefit from their result — but they must not outlive
        # the multiplexer itself. Cancel them here; each already carries its
        # own try/except around the connect, so cancellation is a clean abort,
        # not a leaked subprocess/session.
        # ``_probe_tasks``, not ``_probe_inflight`` — the latter only holds the
        # JOINABLE probe per server, so cancelling from it would leave a forced
        # re-probe running after the multiplexer it belongs to is gone.
        inflight = tuple(self._probe_tasks)
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        self._probe_inflight.clear()
        self._probe_tasks.clear()
        self._discovery_binding_sidechannel.clear()
        self._local_discovery_cache_authority.clear()
        # D-CDX-44: no lifecycle action needed on the mount-singleflight
        # futures themselves — each runs inside its OWN caller's task (never
        # a task this multiplexer spawned), so that caller's own cancellation
        # settles it. Clearing the map just stops a NEW caller arriving
        # during/after shutdown from joining a future that will never
        # resolve to a live mount.
        self._mount_inflight.clear()
        policies = tuple(self._child_runtime_policies.values())
        try:
            for runtime in self.children.values():
                await runtime.aclose()
        finally:
            self._child_runtime_policies.clear()
            self._child_policy_admitted_tools.clear()
            self._child_catalog_fingerprints.clear()
            self._child_tool_digests.clear()
            self._child_schema_revisions.clear()
            self._child_schema_refresh_errors.clear()
            self._session_notifications.clear()
            for policy in policies:
                _close_runtime_child_policy(policy)
        await self.exit_stack.aclose()
