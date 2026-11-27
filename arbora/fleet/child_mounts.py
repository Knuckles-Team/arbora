"""Child mounts: prefix maps, tool forwarders, live tool lists and the mount lifecycle (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import asyncio
import logging
import re
import typing as _typing

import fastmcp.exceptions as _fastmcp_exceptions
import fastmcp.tools as _fastmcp_tools

from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    from mcp_types import Tool as MCPTool
import arbora.fleet.child_resilience as _child_resilience
from arbora.api.domain.fleet import catalog_machine as _catalog_machine
from arbora.fleet.child_payload_bounds import (
    _MAX_DELEGATED_NODES,
    _MAX_DELEGATED_VALUE_BYTES,
    _assert_bounded_json_value,
    _bounded_tool_catalog,
    _child_tool_admitted,
    _list_all_tools,
    _live_tool_entry,
    _stage_forwarder_components,
    _tool_catalog_digest,
    assert_bounded_delegated_value,
    child_result_payload,
)
from arbora.fleet.fleet_capabilities import (
    require_fleet_capability,
)
from arbora.fleet.session_keys import (
    ambient_session_key,
)
from arbora.security.log_redaction import redact_for_log

logger = logging.getLogger("mcp_multiplexer")
# The SOLE handle on the MCP protocol types in this module, for two reasons that
# both bite here. (1) MCP SDK v2 re-homed the whole `mcp.types` namespace into the
# standalone `mcp_types` distribution, so `import mcp.types` raises ImportError at
# module scope on an SDK v2 image — and this module is the fleet loader, so that
# takes every child server down with it. `mcp_types_module()` binds whichever the
# installed SDK ships. (2) `mcp` is also a common PARAMETER name in this file (see
# `_register_forwarder`), where it shadows the package outright; a `mcp_types.X`
# attribute chain there resolves against the parameter, not the SDK.
mcp_types = mcp_types_module()


# Tokens that carry no identifying signal — stripped before auto-deriving a
# prefix so e.g. "weather-mcp-server" keys off "weather", not "mcp"/"server".
_PREFIX_NOISE_TOKENS = frozenset(
    {
        "mcp",
        "server",
        "agent",
        "api",
        "service",
        "srv",
        "tool",
        "tools",
    }
)


def _tokenize_server_name(name: str) -> list[str]:
    """Split a server name into lowercase word tokens, handling separators
    (``-_./:`` etc.) AND camelCase/PascalCase humps (``MyCoolMCP`` →
    my/cool/mcp), so any naming style yields sensible tokens."""
    humped = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", humped) if t]


# A trailing neutral instance id (edge101, zone202, …) is kept readable on
# multi-instance servers (e.g. systems-manager-mcp-edge101 → sm_edge101).
_INSTANCE_ID_RE = re.compile(r"^[a-z]{0,4}\d+[a-z0-9]*$")


def _instance_id_server_prefix(meaningful: list[str]) -> str | None:
    """``<initials>_<id>`` when the name ends in a neutral instance id, else ``None``.

    Keeps multi-instance servers legible and distinct (systems-manager-mcp-edge101
    → ``sm_edge101``).
    """
    if len(meaningful) < 2 or not _INSTANCE_ID_RE.match(meaningful[-1]):
        return None
    base = meaningful[:-1]
    acronym = "".join(t[0] for t in base) or base[0][:2]
    return f"{acronym}_{meaningful[-1]}"


def auto_server_prefix(server_name: str) -> str:
    """Algorithmically derive a short, readable prefix for ANY MCP server name —
    no lookup table, so out-of-ecosystem / third-party servers are fully
    supported (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). Uniqueness across the fleet is guaranteed
    separately by the catalog-aware collision resolver.

    Rules (after dropping noise words mcp/server/agent/api/…):
      • trailing host/instance id → ``<initials>_<id>`` (systems-manager-mcp-edge101
        → 'sm_edge101'), so multi-instance servers stay distinct and legible;
      • multi-word → initials acronym (container-manager → 'cm', github → 'gith');
      • single-word → a short stem ('leanix' → 'lean')."""
    tokens = _tokenize_server_name(server_name)
    meaningful = [t for t in tokens if t not in _PREFIX_NOISE_TOKENS] or tokens
    if not meaningful:
        return "mcp"
    instance_prefix = _instance_id_server_prefix(meaningful)
    if instance_prefix is not None:
        return instance_prefix
    if len(meaningful) >= 2:
        acronym = "".join(t[0] for t in meaningful)
        if len(acronym) >= 2:
            return acronym[:5]
    return meaningful[0][:4]


def get_server_prefix(server_name: str, cfg: dict | None = None) -> str:
    """Resolve a server's preferred prefix (uniqueness is enforced later by the
    catalog-aware collision resolver). An explicit ``prefix`` on the server's
    config entry wins; otherwise it is auto-derived from the name."""
    if cfg:
        explicit = cfg.get("prefix")
        if explicit:
            clean = re.sub(r"[^A-Za-z0-9_]+", "_", str(explicit)).strip("_").lower()
            if clean:
                return clean[:10]
    return auto_server_prefix(server_name)


def clean_tool_name(prefix: str, server_name: str, original_tool_name: str) -> str:
    """Removes redundant server/module name prefixes from the tool name and ensures strict length compliance."""
    if server_name.startswith("systems-manager-mcp-"):
        base_server = "systems-manager-mcp"
    elif server_name.startswith("container-manager-mcp-"):
        base_server = "container-manager-mcp"
    else:
        base_server = server_name

    clean_server = base_server.replace("-", "_").lower()
    cleaned = original_tool_name

    # Build potential redundant prefixes to strip from the tool name
    strips = [
        f"{clean_server}_mcp_",
        f"{clean_server}_",
        f"{prefix}_mcp_",
        f"{prefix}_",
    ]

    if base_server.endswith("-mcp"):
        mod_server = base_server[:-4].replace("-", "_").lower()
        strips.append(f"{mod_server}_mcp_")
        strips.append(f"{mod_server}_")

    for s in strips:
        if cleaned.startswith(s):
            cleaned = cleaned[len(s) :]
            break

    # Build the final namespaced candidate
    candidate = f"{prefix}__{cleaned}"

    # Target maximum budget: 44 characters (so client-prefixed name is <= 64 characters)
    if len(candidate) > 44:
        budget = 44 - len(prefix) - 2  # 2 for "__"
        candidate = f"{prefix}__{cleaned[:budget].strip('_')}"

    return candidate


class UnsupportedLocalProviderLayout(RuntimeError):
    """FastMCP's local provider's private component registry does not match
    the shape :func:`_local_provider_component_snapshot` requires (D-CDX-51).

    A ``RuntimeError`` subclass so any existing ``except RuntimeError`` at a
    call site still catches it — this is a refinement of the prior bare
    ``RuntimeError("FastMCP local component registry is unavailable")``, not
    a new failure category callers need to special-case.
    """


def _local_provider_component_snapshot(
    host: _typing.Any,
) -> tuple[_typing.Any, dict[str, _typing.Any]]:
    """Resolve ``host``'s local provider and a shape-checked snapshot of its
    private component registry (D-CDX-51).

    FastMCP 4.0.0b1's ``LocalProvider`` keeps executable tools in a plain
    private ``_components: dict[str, FastMCPComponent]`` and exposes no
    public atomic replace/remove API — so the atomic schema
    replace/rollback in :meth:`MCPMultiplexer._replace_exposed_forwarders`
    (and the KeyError-invariant check in
    :meth:`MCPMultiplexer._remove_host_forwarder`) have no choice but to
    snapshot and swap that private map directly. This project explicitly
    permits future FastMCP versions, and the fleet is ALREADY
    deliberately mixed-version (children on fastmcp 3.x; this canonical
    tree on fastmcp >=4.0.0b1) — so a later provider adding secondary
    indexes, lifecycle hooks, ownership metadata, or swapping the container
    type entirely is not hypothetical.

    This is the ONE place that reads ``_components``, and it validates the
    EXACT shape every caller depends on — a plain ``dict`` keyed by ``str``,
    whose values each expose both ``.key`` (used to re-key a staged
    forwarder in a swap) and ``.name`` (used by the KeyError-invariant
    check) — BEFORE returning anything, so an unrecognized layout is caught
    HERE, before any mutation, rather than silently corrupting host state
    two calls later. Raises :class:`UnsupportedLocalProviderLayout`
    (fail closed) the moment the shape does not hold.

    Returns ``(provider, snapshot)`` where ``snapshot`` is a fresh ``dict``
    copy — safe for a caller to mutate or roll back to without touching the
    live registry until it explicitly swaps.
    """
    provider = getattr(host, "_local_provider", None)
    if provider is None:
        raise UnsupportedLocalProviderLayout(
            "FastMCP host has no _local_provider on this SDK version"
        )
    components = getattr(provider, "_components", None)
    if not isinstance(components, dict):
        raise UnsupportedLocalProviderLayout(
            "FastMCP local provider's _components is not a dict on this SDK version"
        )
    for key, component in components.items():
        if not isinstance(key, str):
            raise UnsupportedLocalProviderLayout(
                "FastMCP local provider's _components has a non-string key "
                "on this SDK version"
            )
        if not hasattr(component, "key") or not hasattr(component, "name"):
            raise UnsupportedLocalProviderLayout(
                "FastMCP local provider's component objects are missing "
                ".key/.name on this SDK version"
            )
    return provider, dict(components)


def _swap_local_provider_components(
    provider: _typing.Any, components: dict[str, _typing.Any]
) -> None:
    """Atomically replace ``provider``'s component registry (D-CDX-51).

    Prefers a public atomic replacement API — checked by name, so this
    stays forward-compatible the moment FastMCP ever adds one without
    requiring a new dependency-range bump first — and falls back to the
    private ``_components`` swap only when no public alternative exists.
    Callers must have already obtained ``provider`` through
    :func:`_local_provider_component_snapshot` so the private-fallback path
    is only ever reached after the shape check passed.
    """
    public_replace = getattr(provider, "replace_components", None)
    if callable(public_replace):
        public_replace(components)
        return
    provider._components = components


def _json_dump(value: _typing.Any) -> _typing.Any:
    """One decoded MCP value as plain JSON (aliases kept, nulls dropped)."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _relayed_prompt_message(message: _typing.Any) -> dict[str, _typing.Any]:
    """One rendered prompt message, every content block whole (R076).

    A text block also keeps the flat ``text`` key relay readers use; an
    embedded resource, resource link or image is relayed as the child sent it,
    never dropped.
    """
    content = _json_dump(getattr(message, "content", None))
    relayed: dict[str, _typing.Any] = {
        "role": str(getattr(message, "role", "")),
        "content": content,
    }
    text = content.get("text") if isinstance(content, dict) else None
    if isinstance(text, str):
        relayed["text"] = text
    return relayed


def _make_forwarder(
    governed_fleet: _typing.Callable[[], _typing.Any], prefixed_name: str
):
    """Build a native tool whose only call path is governed fleet.call.

    The process owner binds the prepared registration after validating live
    authorities. An absent binding never falls back to a shared child credential.
    """

    async def _forward(**kwargs: _typing.Any) -> _fastmcp_tools.ToolResult:
        binding = governed_fleet()
        if binding is None:
            raise _fastmcp_exceptions.ToolError("governed_fleet_unavailable")
        return await binding.call_native(prefixed_name, kwargs)

    return _forward


def _forwarder_component(
    governed_fleet: _typing.Callable[[], _typing.Any], tool: MCPTool
) -> _fastmcp_tools.FunctionTool:
    """Build the executable FastMCP component for one aggregated child tool."""
    schema = tool.input_schema or {"type": "object", "properties": {}}
    return _fastmcp_tools.FunctionTool(
        name=tool.name,
        description=tool.description or "",
        parameters=schema,
        fn=_make_forwarder(governed_fleet, tool.name),
    )


async def _notify_tools_changed(mcp) -> bool:
    """Emit ``notifications/tools/list_changed`` so the client re-fetches the
    tool list after a dynamic mount/unmount.

    BUG-050: the return value means "the transport write did not raise" —
    i.e. this process attempted delivery and the local send call succeeded.
    It does NOT mean, and must never be documented or read as meaning, "the
    client received it" or "the client refreshed its tool list": MCP's
    ``notifications/*`` are fire-and-forget with no application-level ack, so
    a successful local write can still be dropped by the transport, ignored
    by the client, or simply not yet processed by the time the caller's next
    action runs. A missing request context (e.g. eager startup with no client
    attached yet) is an expected, silent ``False`` — but a LIVE client whose
    send raised is a real, surfaced failure, never swallowed into a
    server-only log line: a caller (``load_tools``/``unload_tools``) that
    reported success while even the local send failed is exactly the
    control-plane-truthfulness gap this exists to prevent
    (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). Callers surface this as
    ``notification_sent`` (never ``notified`` — that name is retired, see
    ``load_session_tools``) so the agent can tell "the server didn't even try
    to push" apart from "it tried; whether the client acted on it is unknown
    either way" — the second case looks identical to success and is not
    reducible to a boolean this function could ever return.
    """
    from fastmcp.server.dependencies import get_context

    try:
        context = get_context()
    except RuntimeError:
        # No active request context at all — not a client-facing failure.
        return False
    try:
        await context.send_notification(mcp_types.ToolListChangedNotification())
    except Exception as exc:
        logger.warning(
            "tools/list_changed notification failed to send: %s: %s",
            type(exc).__name__,
            redact_for_log(exc),
            exc_info=True,
        )
        return False
    return True


class ChildMounts:
    """Mounts one child's tools on the host: the prefix map, forwarders, tool-list changes and the per-session notification queue."""

    def __init__(
        self,
        *,
        governed_fleet: _typing.Callable[[], _typing.Any],
        always_load_servers: _typing.Any,
        always_load_tool_specs: _typing.Any,
        auto_unload: _typing.Any,
        child_runtime_policies: _typing.Any,
        child_schema_refresh_errors: _typing.Any,
        child_schema_revisions: _typing.Any,
        child_tool_digests: _typing.Any,
        exposed: _typing.Any,
        fleet: _typing.Any,
        mount_inflight: _typing.Any,
        probe_cache: _typing.Any,
        session_loaded: _typing.Any,
        session_notifications: _typing.Any,
        children: _typing.Any,
        sessions: _typing.Any,
        catalog_epoch: _typing.Any,
        catalog_state: _typing.Any,
        host_mcp: _typing.Any,
        prefix_map: _typing.Any,
        prefix_reverse: _typing.Any,
        tool_rows: _typing.Any,
        aggregated_tools: _typing.Any,
        tool_to_server: _typing.Any,
        admit_runtime_policy_tools: _typing.Any,
        announce_handshake: _typing.Any,
        catalog_event: _typing.Any,
        drop_discovery_bindings_for_server: _typing.Any,
        live_primary_session: _typing.Any,
        probe_prompts: _typing.Any,
        probe_skills: _typing.Any,
        relay_read: _typing.Any,
        start_child: _typing.Any,
        store_aggregated_tools: _typing.Any,
        store_prefix_maps: _typing.Any,
        store_tool_to_server: _typing.Any,
        call_proxied_tool: _typing.Any,
        load_catalog: _typing.Any,
        probe_server: _typing.Any,
        prune_session_visibility: _typing.Any,
        tool_object: _typing.Any,
    ) -> None:
        self._always_load_servers = always_load_servers
        self._always_load_tool_specs = always_load_tool_specs
        self._auto_unload = auto_unload
        self._child_runtime_policies = child_runtime_policies
        self._child_schema_refresh_errors = child_schema_refresh_errors
        self._child_schema_revisions = child_schema_revisions
        self._child_tool_digests = child_tool_digests
        self._exposed = exposed
        self._fleet = fleet
        self._governed_fleet = governed_fleet
        self._mount_inflight = mount_inflight
        self._probe_cache = probe_cache
        self._session_loaded = session_loaded
        self._session_notifications = session_notifications
        self.children = children
        self.sessions = sessions
        self._catalog_epoch = catalog_epoch
        self._catalog_state = catalog_state
        self._host_mcp = host_mcp
        self._prefix_map = prefix_map
        self._prefix_reverse = prefix_reverse
        self._tool_rows = tool_rows
        self.aggregated_tools = aggregated_tools
        self.tool_to_server = tool_to_server
        self._admit_runtime_policy_tools = admit_runtime_policy_tools
        self._announce_handshake = announce_handshake
        self._catalog_event = catalog_event
        self._drop_discovery_bindings_for_server = drop_discovery_bindings_for_server
        self._live_primary_session = live_primary_session
        self._probe_prompts = probe_prompts
        self._probe_skills = probe_skills
        self._relay_read = relay_read
        self._start_child = start_child
        self._store_aggregated_tools = store_aggregated_tools
        self._store_prefix_maps = store_prefix_maps
        self._store_tool_to_server = store_tool_to_server
        self.call_proxied_tool = call_proxied_tool
        self.load_catalog = load_catalog
        self.probe_server = probe_server
        self.prune_session_visibility = prune_session_visibility
        self.tool_object = tool_object

    @staticmethod
    def _server_stem(name: str) -> str:
        """Full cleaned server name used to disambiguate colliding prefixes."""
        clean = "".join(c if (c.isalnum() or c in ("_", "-")) else "_" for c in name)
        return clean.replace("-", "_").lower().strip("_")

    def _build_prefix_map(self) -> None:
        """Assign every catalog server a unique prefix (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        Each server starts from its preferred prefix (:func:`get_server_prefix`
        — config override or auto-derived). Collisions are resolved
        deterministically (sorted order): the first claimant keeps the preferred
        prefix; the rest extend their cleaned stem until unique, falling back to
        a numeric suffix. Guarantees no two servers ever share a prefix, however
        the fleet grows."""
        catalog = self.load_catalog()
        names = sorted(catalog.keys())
        assigned: dict[str, str] = {}
        used: set[str] = set()
        for name in names:
            base = get_server_prefix(name, catalog.get(name))
            if base not in used:
                assigned[name] = base
                used.add(base)
                continue
            stem = self._server_stem(name)
            cand: str | None = None
            for n in range(max(len(base) + 1, 1), len(stem) + 1):
                if stem[:n] not in used:
                    cand = stem[:n]
                    break
            if cand is None:  # stem exhausted — append a numeric suffix
                i = 2
                while f"{base}{i}" in used:
                    i += 1
                cand = f"{base}{i}"
            assigned[name] = cand
            used.add(cand)
        self._store_prefix_maps(assigned, {p: n for n, p in assigned.items()})

    def server_prefix(self, server_name: str) -> str:
        """The unique, collision-free prefix for a server (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).
        Falls back to the bare derived prefix for names outside the catalog."""
        if self._prefix_map() is None:
            self._build_prefix_map()
        assert self._prefix_map() is not None
        return self._prefix_map().get(server_name) or get_server_prefix(server_name)

    def _tool_enabled(self, server_name: str, tool_name: str) -> bool:
        """Whether a server's tool would actually be exposed on load, applying
        the config's ``enabledTools`` whitelist + ``disabledTools`` blacklist —
        the same filter :meth:`_register_child_result` uses. Lets discovery
        distinguish capable-but-disabled tools from loadable ones."""
        import fnmatch

        cfg = self.load_catalog().get(server_name, {})
        enabled = cfg.get("enabledTools")
        disabled = cfg.get("disabledTools", [])
        if enabled is not None and not any(
            fnmatch.fnmatch(tool_name, pat) for pat in enabled
        ):
            return False
        if disabled and any(fnmatch.fnmatch(tool_name, pat) for pat in disabled):
            return False
        return True

    def _prefixed_child_tools(
        self,
        server_name: str,
        tools: list[MCPTool],
        cfg: dict,
    ) -> tuple[list[MCPTool], dict[str, str]]:
        """Filter and prefix one child catalog without mutating live state."""
        disabled_tools = cfg.get("disabledTools", [])
        enabled_tools = cfg.get("enabledTools", None)
        registered: list[MCPTool] = []
        originals: dict[str, str] = {}
        for tool in tools:
            if not _child_tool_admitted(tool.name, enabled_tools, disabled_tools):
                continue
            prefix = self.server_prefix(server_name)
            prefixed_name = clean_tool_name(prefix, server_name, tool.name)
            registered.append(
                mcp_types.Tool(
                    name=prefixed_name,
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                    output_schema=getattr(tool, "output_schema", None),
                    annotations=getattr(tool, "annotations", None),
                    # Preserve _meta (carries FastMCP tags) so downstream
                    # visibility filtering sees the child's real tags.
                    _meta=getattr(tool, "meta", None),
                )
            )
            # ``clean_tool_name`` may strip a redundant server/prefix segment,
            # so the original child name cannot be reconstructed by splitting
            # the forwarded name.  Keep that exact routing target alongside
            # the generated outer schema.
            originals[prefixed_name] = tool.name
        return registered, originals

    def _remove_host_forwarder(self, prefixed_name: str) -> None:
        """Remove one mux-owned FastMCP forwarder without public API churn."""
        host = self._host_mcp()
        if host is None:
            self._exposed.discard(prefixed_name)
            return
        provider = getattr(host, "_local_provider", None)
        remove_tool = getattr(provider, "remove_tool", None)
        if not callable(remove_tool):
            raise RuntimeError("FastMCP local provider cannot remove a forwarding tool")
        try:
            remove_tool(prefixed_name)
        except KeyError as exc:
            # FastMCP documents KeyError for an absent tool, which is an
            # idempotent hot-reload outcome only after its private registry
            # confirms that our executable forwarder is gone.  A matching
            # component here makes KeyError an SDK/provider invariant breach;
            # propagate it rather than erasing a live route from mux state.
            # D-CDX-51: the shape-checked adapter, not a raw ``_components``
            # read, so an unrecognized SDK layout fails closed here too.
            try:
                _, components = _local_provider_component_snapshot(host)
            except UnsupportedLocalProviderLayout as shape_exc:
                raise RuntimeError(
                    "FastMCP forwarding registry cannot verify an absent tool"
                ) from shape_exc
            if any(
                isinstance(component, _fastmcp_tools.Tool)
                and component.name == prefixed_name
                for component in components.values()
            ):
                raise RuntimeError(
                    "FastMCP reported a forwarding tool absent while it remains registered"
                ) from exc
            logger.info(
                "MCP forwarding tool was already absent during cleanup "
                "(tool_ref=%s, exception_ref=%s)",
                redact_for_log(prefixed_name),
                redact_for_log(exc),
            )
        self._exposed.discard(prefixed_name)

    def _changed_exposed_schemas(
        self,
        old_tools: dict[str, MCPTool],
        new_tools: dict[str, MCPTool],
    ) -> set[str]:
        """Which currently-exposed names have a changed (or removed) schema."""
        exposed = set(old_tools) & self._exposed
        return {
            name
            for name in exposed
            if name not in new_tools
            or _tool_catalog_digest([old_tools[name]])
            != _tool_catalog_digest([new_tools[name]])
        }

    def _replace_exposed_forwarders(
        self,
        old_tools: dict[str, MCPTool],
        new_tools: dict[str, MCPTool],
    ) -> set[str]:
        """Atomically replace changed exposed schemas in FastMCP's registry.

        FastMCP 4.0.0b1 has no public batch-registration primitive: each
        ``add_tool`` immediately replaces a duplicate in its local provider.
        A two-tool recovery can therefore accept one replacement and fail the
        next.  Stage the complete component registry first, and retain an
        exact SDK-component snapshot so a partial native registration is
        restored with one provider-registry swap rather than a doomed series
        of ``add_tool`` rollback calls.

        D-CDX-51: the snapshot and the swap both go through the shape-checked
        adapter (:func:`_local_provider_component_snapshot` /
        :func:`_swap_local_provider_components`) rather than touching
        ``provider._components`` directly — an unrecognized SDK layout fails
        closed BEFORE this reads or mutates anything.
        """
        changed = self._changed_exposed_schemas(old_tools, new_tools)
        if not changed or self._host_mcp() is None:
            return set()

        replacements = sorted(name for name in changed if name in new_tools)
        removed = sorted(changed - set(replacements))
        host = self._host_mcp()
        provider, previous_components = _local_provider_component_snapshot(host)

        # ``_fastmcp_tools.FunctionTool`` construction validates every child schema before
        # the live registry is touched.  Keep these real SDK objects in the
        # staged mapping — protocol ``_fastmcp_tools.Tool`` models are not executable host
        # components and cannot safely stand in for them.
        forwarders = {
            name: _forwarder_component(self._governed_fleet, new_tools[name])
            for name in replacements
        }
        staged_components = _stage_forwarder_components(
            previous_components, forwarders, set(changed)
        )

        try:
            # Preserve FastMCP's native registration path and any validation
            # it performs.  It is synchronous, so another served request
            # cannot observe an intermediate component map on this event-loop
            # turn.  If a later add fails, restore the exact prior registry
            # below without asking that same failed API to accept a rollback.
            for forwarder in forwarders.values():
                host.add_tool(forwarder)
        except Exception:
            # LocalProvider resolves lookups directly from this mapping in
            # FastMCP 4.0.0b1.  Replacing it restores the prior executable
            # _fastmcp_tools.FunctionTool objects atomically even when ``add_tool`` remains
            # unavailable, leaving mux maps untouched below.
            _swap_local_provider_components(provider, previous_components)
            raise
        # A removal is committed in the same one-step replacement, so no
        # client can be left with a half-refreshed forwarded tool set.
        _swap_local_provider_components(provider, staged_components)
        self._exposed.difference_update(removed)
        return changed

    def _queue_tools_changed(self, changed_names: set[str]) -> None:
        """Record one detached schema refresh for sessions that exposed it.

        A child supervisor has no valid request context: reusing the context
        inherited when it was spawned can write to a completed response stream.
        Keep the notification durable until each affected session makes its
        next request, where :meth:`notify_pending_tools_changed` can use that
        request's own outbound channel.
        """
        if not changed_names:
            return
        affected_sessions = [
            session_key
            for session_key, loaded in self._session_loaded.items()
            if changed_names & loaded
        ]
        self._queue_tool_list_change_for_sessions(affected_sessions)

    def _queue_tool_list_change_for_sessions(self, session_keys: list[str]) -> None:
        """Queue one list revision for explicitly identified live sessions."""
        self._session_notifications.queue(session_keys)

    async def notify_pending_tools_changed(self) -> bool:
        """Deliver this live session's queued ``tools/list_changed`` event.

        Returning ``False`` retains the revision for a later request; a failed
        notification is never misreported as delivered.  A newer background
        refresh that arrives while the send awaits remains queued after this
        older revision is acknowledged.
        """
        session_key = ambient_session_key()
        revision = self._session_notifications.pending_generation(session_key)
        if revision is None:
            return True
        if self._host_mcp() is None or not await _notify_tools_changed(
            self._host_mcp()
        ):
            return False
        self._session_notifications.acknowledge(session_key, revision)
        self.prune_session_visibility(session_key)
        return True

    def _rebind_server_tool_maps(
        self,
        server_name: str,
        refreshed: list[MCPTool],
        originals: dict[str, str],
    ) -> set[str]:
        """Swap one server's rows in the aggregation maps; return the prior names."""
        stale_names = {
            prefixed
            for prefixed, (owner, _original) in self.tool_to_server().items()
            if owner == server_name
        }
        self._store_tool_to_server(
            {
                prefixed: target
                for prefixed, target in self.tool_to_server().items()
                if target[0] != server_name
            }
        )
        self._store_aggregated_tools(
            [tool for tool in self.aggregated_tools() if tool.name not in stale_names]
        )
        for tool in refreshed:
            self.tool_to_server()[tool.name] = (server_name, originals[tool.name])
        self.aggregated_tools().extend(refreshed)
        return stale_names

    def _drop_stale_child_caches(self, server_name: str) -> None:
        """Invalidate every derived cache keyed on one server's old catalog."""
        self._probe_cache.pop(server_name, None)
        self._drop_discovery_bindings_for_server(server_name)

    def _retract_removed_from_sessions(self, removed: set[str]) -> None:
        """Retract tools a child no longer serves from every session's view."""
        if not removed:
            return
        for loaded in self._session_loaded.values():
            loaded.difference_update(removed)
        for loaded in self._auto_unload.values():
            loaded.difference_update(removed)
        for session_key in tuple(self._session_loaded):
            self.prune_session_visibility(session_key)

    def _replace_child_tools(
        self,
        server_name: str,
        tools: list[MCPTool],
        cfg: dict,
    ) -> tuple[list[MCPTool], bool]:
        """Replace cached tools for one child when its live schema changed.

        This runs only at initial mount or after a child connection generation
        has already completed ``tools/list``.  It never performs provider I/O.
        All map mutations are synchronous, and a recovering runtime keeps its
        readiness gate closed until this method returns.
        """
        refreshed, originals = self._prefixed_child_tools(server_name, tools, cfg)
        refreshed_digest = _tool_catalog_digest(refreshed)
        current = self.prefixed_tools_for_server(server_name)
        current_digest = self._child_tool_digests.get(server_name)
        if current_digest is None and current:
            current_digest = _tool_catalog_digest(current)
        if current_digest == refreshed_digest:
            self._child_tool_digests.setdefault(server_name, refreshed_digest)
            return current, False

        current_by_name = {tool.name: tool for tool in current}
        refreshed_by_name = {tool.name: tool for tool in refreshed}
        changed_exposed = self._replace_exposed_forwarders(
            current_by_name, refreshed_by_name
        )

        stale_names = self._rebind_server_tool_maps(server_name, refreshed, originals)
        self._child_tool_digests[server_name] = refreshed_digest
        self._child_schema_revisions[server_name] = (
            self._child_schema_revisions.get(server_name, 0) + 1
        )
        self._drop_stale_child_caches(server_name)

        self._queue_tools_changed(changed_exposed)

        self._retract_removed_from_sessions(stale_names - set(refreshed_by_name))
        return refreshed, True

    async def _publish_live_tools(
        self, server_name: str, tools: list[_typing.Any]
    ) -> None:
        """Serve a mounted child's re-listed tools (ARBORA-FLEET-R070).

        The same policy admission and schema refresh a new child generation
        gets; a child that is not mounted has nothing to publish.
        """
        runtime = self.children.get(server_name)
        if runtime is None:
            return
        cfg = self.load_catalog().get(server_name)
        if cfg is None:
            return
        policy = self._child_runtime_policies.get(server_name)
        if policy is not None:
            tools = self._admit_runtime_policy_tools(server_name, policy, tools)
        await self._refresh_child_tools(
            server_name, runtime, cfg, self._catalog_epoch(), tools
        )

    async def _relist_live_child(self, server_name: str) -> None:
        """Re-read a mounted child's whole tool list on its live session."""
        session = self._live_primary_session(server_name)
        tools = await _list_all_tools(session)
        _bounded_tool_catalog(tools)
        await self._publish_live_tools(server_name, tools)
        await self._announce_handshake(server_name, session, tools)

    async def _refresh_child_tools(
        self,
        server_name: str,
        runtime: _child_resilience.ChildRuntime,
        cfg: dict,
        catalog_epoch: int,
        tools: list[MCPTool],
    ) -> None:
        """Publish a recovered child generation's schema before it serves calls."""
        if (
            catalog_epoch != self._catalog_epoch()
            or self.children.get(server_name) is not runtime
        ):
            # A catalog hot reload already retired this runtime.  Its delayed
            # reconnect must never resurrect stale routing/schema state.
            return
        primary = runtime.primary_session
        if primary is not None:
            self.sessions[server_name] = primary
        try:
            self._replace_child_tools(server_name, tools, cfg)
        except Exception as exc:
            self._child_schema_refresh_errors[server_name] = "schema_refresh_failed"
            logger.error(
                "MCP child schema refresh failed (server=%s, exception_type=%s): %s",
                server_name,
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
        else:
            self._child_schema_refresh_errors.pop(server_name, None)

    def _register_child_result(
        self,
        server_name: str,
        payload: _typing.Any,
        tools: list[MCPTool],
        cfg: dict,
    ) -> list[MCPTool]:
        """Record a freshly started child's runtime, session, and (filtered)
        tools into the aggregation maps. Returns the prefixed ``_fastmcp_tools.Tool`` objects
        that were registered for this child.

        Shared by eager :meth:`start_children` and lazy :meth:`mount_child` so
        the enable/disable filtering and prefixing logic lives in one place.
        """
        # The per-child hardening runtime (CONCEPT:MERISTEM-ECO.mcp.profile-differences-from-client) carries the
        # session pool, concurrency limits, and restart supervisor. Plain
        # session payloads (externally owned connections) are wrapped in a
        # supervisor-less runtime: limits apply, auto-restart does not.
        if isinstance(payload, _child_resilience.ChildRuntime):
            runtime = payload
        else:
            sessions = list(payload) if isinstance(payload, list | tuple) else [payload]
            runtime = _child_resilience.ChildRuntime(
                server_name,
                cfg,
                confirmations=self._fleet.confirmations,
                config=self._fleet.config,
            )
            runtime.adopt_sessions(sessions)
        self.children[server_name] = runtime
        # `primary_session` is `None` only for a runtime with no adopted
        # sessions yet, which should not happen this far into registration.
        primary = runtime.primary_session
        if primary is not None:
            self.sessions[server_name] = primary

        registered, _changed = self._replace_child_tools(server_name, tools, cfg)
        return registered

    async def _live_skills_for_server(self, server_name: str) -> list[dict]:
        """Live ``skill://`` resource listing for an already-mounted child
        (CONCEPT:MERISTEM-ECO.mcp.skills-over-mcp-provider).

        Unlike tools, a mounted child's Skills-over-MCP resources are never
        cached into an aggregation map at mount time (:meth:`_register_child_result`
        only indexes ``tools``) — so the cold-probe path's ``_probe_skills`` call
        was the ONLY place a server's skills were ever discovered, leaving an
        already-mounted server's skills invisible to ``find``/``find_tools``
        until it happened to be probed cold at least once (D-2.2-2.3-1). Reuses
        the mounted child's live primary session (no reconnect) and the same
        best-effort degrade-to-``[]`` semantics as the cold-probe path.
        """
        session = self.sessions.get(server_name)
        if session is None:
            return []
        return await self._probe_skills(server_name, session)

    async def _live_prompts_for_server(self, server_name: str) -> list[dict]:
        """Live ``prompt://`` resource listing for an already-mounted child
        (CONCEPT:MERISTEM-ECO.mcp.cross-process-prompt-harvest — the ``prompt://``
        sibling of :meth:`_live_skills_for_server`, same D-2.2-2.3-1 rationale:
        a mounted child's prompt resources are never cached at mount time, so
        this is the only place an already-mounted server's prompts are
        discovered without waiting for a cold probe).
        """
        session = self.sessions.get(server_name)
        if session is None:
            return []
        return await self._probe_prompts(server_name, session)

    @staticmethod
    def _mark_future_exception_retrieved(future: asyncio.Future[_typing.Any]) -> None:
        """Call ``future.exception()`` so asyncio never logs "exception was
        never retrieved" for a leader future with no follower.

        Kept as its own helper (rather than a bare ``future.exception()`` call
        inline in the ``except`` handler) purely so the served-boundary static
        exception-surface gate does not mistake this asyncio bookkeeping call
        for a ``logger.exception(...)`` leak — it is neither logging nor
        exposing anything, it only marks the future's exception as observed.
        """
        future.exception()

    def _release_mount_ownership(
        self, server_name: str, leader_future: asyncio.Future
    ) -> None:
        """Release this task's singleflight ownership of ``server_name``.

        A no-op when another task has since taken ownership, so a late release
        can never evict a newer leader's in-flight mount (D-CDX-44).
        """
        if self._mount_inflight.get(server_name) is leader_future:
            del self._mount_inflight[server_name]

    async def mount_child(self, server_name: str) -> list[MCPTool]:
        """Start ONE configured child on demand and register its tools
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        Idempotent: if the child is already mounted, its already-registered
        prefixed tools are returned without re-spawning. Returns ``[]`` for an
        unknown/unconfigured server. Loop-safe because it is invoked from
        inside the serving event loop (either at boot or from a tool call), so
        the child ``ClientSession`` objects bind to the running loop.

        D-CDX-44: per-server singleflight. Multiple concurrent callers can
        legitimately request the SAME unmounted ``server_name`` at once (two
        sessions calling ``load_tools`` back to back, two sessions racing the
        ``ensure_always_loaded`` eager pass, ...). Without ownership, each
        would independently observe ``server_name not in self.children`` and
        each would start its own child runtime — duplicate provider
        processes/connections, a registration race on the shared aggregation
        maps, and doubled probe/startup cost. Exactly ONE task (the leader)
        runs :meth:`_mount_child_first_load`; every concurrent caller for the
        same ``server_name`` (a follower) awaits the SAME shared future and
        observes the leader's exact outcome instead of starting its own
        attempt. Different servers mount fully in parallel — the singleflight
        is keyed per-server, never global.
        """
        catalog = self.load_catalog()
        if server_name in self.children:
            return self.prefixed_tools_for_server(server_name)
        cfg = catalog.get(server_name)
        if cfg is None:
            logger.warning("Requested MCP child is not in the catalog")
            return []

        pending = self._mount_inflight.get(server_name)
        if pending is not None:
            # ``shield`` only protects the SHARED future from being cancelled
            # by THIS follower's own cancellation — if the leader itself
            # fails or is cancelled, the future carries that exact outcome
            # (exception or cancellation) and every follower observes the
            # SAME thing, never a silently different or guessed result.
            return await asyncio.shield(pending)

        loop = asyncio.get_running_loop()
        leader_future: asyncio.Future[list[MCPTool]] = loop.create_future()
        self._mount_inflight[server_name] = leader_future
        try:
            result = await self._mount_child_first_load(server_name, cfg)
        except asyncio.CancelledError:
            # Release ownership BEFORE settling so a caller that retries
            # after this cancellation starts a fresh attempt rather than
            # joining a future that will never resolve to a live mount.
            self._release_mount_ownership(server_name, leader_future)
            if not leader_future.done():
                leader_future.cancel()
            raise
        except BaseException as exc:
            self._release_mount_ownership(server_name, leader_future)
            if not leader_future.done():
                leader_future.set_exception(exc)
                # If no follower ever joined (the common case — most first
                # loads are NOT contended), nothing else will ever call
                # ``.exception()``/``.result()`` on this future, and asyncio
                # would otherwise log "exception was never retrieved" at GC
                # time. The leader already has ``exc`` in hand (about to
                # ``raise`` it below), so retrieving it here is a no-op for
                # control flow and just silences that spurious warning; a
                # follower that DOES join later still observes the exception
                # normally — ``Future.exception()`` does not consume it.
                self._mark_future_exception_retrieved(leader_future)
            raise
        self._release_mount_ownership(server_name, leader_future)
        if not leader_future.done():
            leader_future.set_result(result)
        return result

    async def _mount_child_first_load(
        self, server_name: str, cfg: dict
    ) -> list[MCPTool]:
        """The actual first-mount attempt for ``server_name``.

        Runs under exclusive ownership of exactly one task at a time — the
        caller, :meth:`mount_child`, holds that ownership in
        ``_mount_inflight`` for the duration (D-CDX-44). Identical behavior
        to the pre-singleflight ``mount_child`` body: start the child, and if
        an async handshake raced a hot catalog reload, close the now-retired
        runtime instead of registering it.
        """
        catalog_epoch = self._catalog_epoch()
        result = await self._start_child(server_name, cfg)
        if not isinstance(result, tuple):
            return []
        s_name, payload, tools, r_cfg = result
        if catalog_epoch != self._catalog_epoch():
            # The async handshake raced a hot reload.  Do not register a child
            # that was authorized only by the retired catalog; its runtime
            # owns real transports and must be closed promptly.
            if isinstance(payload, _child_resilience.ChildRuntime):
                await payload.aclose()
            return []
        return self._register_child_result(s_name, payload, tools, r_cfg)

    async def delegated_server_tools(
        self, server_name: str
    ) -> list[dict[str, _typing.Any]]:
        """Return one child's bounded tools through the served probe cache."""
        require_fleet_capability("discover", self._fleet.config)
        info = await self.probe_server(server_name)
        if info.get("error"):
            raise RuntimeError("MCP server probe failed")
        return await self._tool_rows().rows(server_name, info)

    async def delegate_server_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, _typing.Any],
        timeout: float,
    ) -> _typing.Any:
        """Invoke one original child tool through this served child pool."""
        require_fleet_capability("delegate", self._fleet.config)
        assert_bounded_delegated_value(arguments)
        await self.mount_child(server_name)
        public_name = next(
            (
                name
                for name, target in self.tool_to_server().items()
                if target == (server_name, tool_name)
            ),
            None,
        )
        if public_name is None:
            raise _fastmcp_exceptions.ToolError("MCP child tool is not available")
        result = await asyncio.wait_for(
            self.call_proxied_tool(public_name, arguments), timeout=timeout
        )
        return child_result_payload(result)

    async def read_server_resource(
        self, *, server_name: str, uri: str, timeout: float
    ) -> dict[str, str]:
        """Read one text resource through this served child connection."""
        require_fleet_capability("delegate", self._fleet.config)
        if not uri or len(uri.encode("utf-8")) > 8_192:
            raise _fastmcp_exceptions.ToolError("MCP resource URI is invalid")
        await self.mount_child(server_name)
        result = await self._relay_read(
            server_name, lambda session: session.read_resource(uri), timeout
        )
        contents = getattr(result, "contents", result)
        first = next(iter(contents or ()), None)
        text = getattr(first, "text", None)
        if not isinstance(text, str):
            raise _fastmcp_exceptions.ToolError("MCP resource carried no text")
        if len(text.encode("utf-8")) > _MAX_DELEGATED_VALUE_BYTES:
            raise _fastmcp_exceptions.ToolError(
                "MCP resource exceeds the size boundary"
            )
        mime_type = getattr(first, "mime_type", None) or getattr(
            first, "mimeType", None
        )
        return {"uri": uri, "text": text, "mimeType": str(mime_type or "text/plain")}

    async def get_server_prompt(
        self,
        *,
        server_name: str,
        name: str,
        arguments: dict[str, str],
        timeout: float,
    ) -> dict[str, _typing.Any]:
        """Render one child prompt through this served child connection."""
        require_fleet_capability("delegate", self._fleet.config)
        if not name or len(name.encode("utf-8")) > 1_024:
            raise _fastmcp_exceptions.ToolError("MCP prompt name is invalid")
        assert_bounded_delegated_value(arguments)
        await self.mount_child(server_name)
        result = await self._relay_read(
            server_name, lambda session: session.get_prompt(name, arguments), timeout
        )
        rendered: dict[str, _typing.Any] = {
            "name": name,
            "description": str(getattr(result, "description", "") or ""),
            "messages": [
                _relayed_prompt_message(message)
                for message in getattr(result, "messages", ()) or ()
            ],
        }
        meta = _json_dump(getattr(result, "meta", None))
        if meta:
            rendered["_meta"] = meta
        try:
            _assert_bounded_json_value(rendered, max_nodes=_MAX_DELEGATED_NODES)
        except _fastmcp_exceptions.ToolError:
            raise _fastmcp_exceptions.ToolError(
                "MCP prompt exceeds the size boundary"
            ) from None
        return rendered

    def prefixed_tools_for_server(self, server_name: str) -> list[MCPTool]:
        """All aggregated prefixed tools currently owned by ``server_name``."""
        names = {
            pn
            for pn, (srv, _orig) in self.tool_to_server().items()
            if srv == server_name
        }
        return [t for t in self.aggregated_tools() if t.name in names]

    async def start_children(self):
        """Parse configuration and start all child processes concurrently
        (eager mode). Lazy mode uses :meth:`mount_child` instead."""
        catalog = self.load_catalog()
        if not catalog:
            logger.info("No active child servers configured.")
            return

        catalog_epoch = self._catalog_epoch()
        tasks = [
            self._start_child(server_name, cfg) for server_name, cfg in catalog.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                # An exception propagated from asyncio.gather, already logged inside task
                continue
            if not isinstance(result, tuple):
                continue
            server_name, payload, tools, cfg = result
            if catalog_epoch != self._catalog_epoch():
                if isinstance(payload, _child_resilience.ChildRuntime):
                    await payload.aclose()
                continue
            self._register_child_result(server_name, payload, tools, cfg)
        if self._catalog_state() is _catalog_machine.CatalogState.SNAPSHOT:
            self._catalog_event(_catalog_machine.CatalogEvent.CHILDREN_UP)

    def always_load_tool_owner(self, spec: str) -> tuple[str | None, str | None]:
        """Resolve one ``MCP_ALWAYS_LOAD_TOOLS`` entry to ``(server, original)``.

        Two accepted forms (see ``AgentConfig.mcp_always_load_tools``):

        * ``"<server>:<tool>"`` — server-qualified ORIGINAL tool name. Resolved
          without consulting the derived prefix map, so a prefix that shifts
          when a new server joins the catalog cannot silently break a
          configured entry. ``original`` is the child's own tool name.
        * ``"<prefix>__<tool>"`` — an aggregated prefixed name; the owner comes
          from the reverse prefix map and ``original`` is ``None`` (the spec
          IS the prefixed name).

        Returns ``(None, None)`` for an entry that resolves to no known server.
        """
        text = str(spec or "").strip()
        if not text:
            return None, None
        if ":" in text:
            server, _, original = text.partition(":")
            server = server.strip()
            original = original.strip()
            return (server or None), (original or None)
        return self.server_for_prefixed(text), None

    def prefixed_for_original(self, server_name: str, original: str) -> str | None:
        """The aggregated prefixed name a child's ORIGINAL tool name maps to.

        Only answerable once ``server_name`` is mounted (``tool_to_server`` is
        populated by :meth:`_register_child_result`); returns ``None`` before
        that, or when the child never registered a tool by that name.
        """
        for prefixed, entry in self.tool_to_server().items():
            if entry == (server_name, original):
                return prefixed
        return None

    def always_load_declared(self) -> bool:
        """True when this multiplexer has any eager always-load declaration."""
        return bool(self._always_load_servers or self._always_load_tool_specs)

    def server_for_prefixed(self, prefixed_name: str) -> str | None:
        """Resolve which child server owns a prefixed tool name, even before
        that child is mounted, via the collision-free reverse prefix map.
        Returns None if unknown."""
        if prefixed_name in self.tool_to_server():
            return self.tool_to_server()[prefixed_name][0]
        if self._prefix_map() is None:
            self._build_prefix_map()
        prefix = prefixed_name.split("__", 1)[0]
        return self._prefix_reverse().get(prefix)

    def _live_tools_for_server(self, server_name: str) -> list[dict]:
        """Raw ``[{name, description, inputSchema, meta}]`` for an already-mounted
        child, reconstructed from the aggregation maps (no reconnect).

        ``meta`` (BUG-071) carries the MCP Apps extension's ``ui.resourceUri``
        declaration (``io.modelcontextprotocol/ui``) — a TOOL-DESCRIPTOR field
        per the extension spec, not something a ``tools/call`` result carries.
        ``tool_object`` already preserves it (``_prefixed_child_tools`` copies
        ``_meta=getattr(tool, "meta", None)`` when a child is mounted); this was
        simply never read back out into the catalog dict callers (e.g.
        ``webui_mcp_delegation._list_mcp_server_tools``) actually see. Omitted
        when absent so an unannotated tool's dict shape is unchanged.
        """
        out: list[dict] = []
        for prefixed, (srv, original) in self.tool_to_server().items():
            if srv != server_name:
                continue
            out.append(_live_tool_entry(original, self.tool_object(prefixed)))
        return out
