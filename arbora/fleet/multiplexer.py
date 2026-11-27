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
import concurrent.futures
import contextlib
import json
import logging
import re
import secrets
import sys
import tempfile
import time
import typing as _typing
from pathlib import Path

import fastmcp.exceptions as _fastmcp_exceptions
import fastmcp.tools as _fastmcp_tools
from mcp.client.session import ClientSession

from arbora.core.settings import RuntimeConfig
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
from arbora.core.capability_contract import Capability
from arbora.fleet.local_skill_catalog import (
    LOCAL_SKILLS_SERVER,
    build_local_skill_catalog,
    missing_core_pack_names,
)
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
from arbora.fleet.child_payload_bounds import (
    _MAX_DELEGATED_NODES,
    _MAX_DELEGATED_VALUE_BYTES,
    _assert_bounded_json_value,
    _bounded_descriptor_catalog,
    _bounded_prompt_catalog,
    _bounded_skill_catalog,
    _bounded_tool_catalog,
    _child_tool_admitted,
    _list_all,
    _list_all_tools,
    _live_tool_entry,
    _stage_forwarder_components,
    _tool_catalog_digest,
    assert_bounded_delegated_value,
    child_result_payload,
)
from arbora.fleet.child_runtime_policy import (
    _close_runtime_child_policy,
    _prepare_runtime_child_policy,
    _runtime_materialized,
    _validate_externalized_child_secrets,
    attest_runtime_child_config,
)
from arbora.fleet.child_transport import (
    _CURRENT_DISCOVERY_BINDING,
    ChildTransport,
    _child_transport_values,
)
from arbora.fleet.fleet_capabilities import (
    require_fleet_capability,
)
from arbora.fleet.proxied_calls import (
    ProxiedCalls,
)
from arbora.fleet.session_keys import (
    ambient_session_key,
)
from arbora.fleet.task_forwarding import (
    TaskForwarding,
)

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

_SERVER_DISCOVERY_STOPWORDS = frozenset({"api", "mcp", "manager", "server", "service"})
# Fleet-wide concurrent-probe ceiling (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog), shared
# across overlapping ``probe_catalog`` calls via ``MCPMultiplexer._probe_semaphore``
# (one instance per multiplexer, NOT re-created per call — a per-call semaphore
# would give every new call its own fresh 16 slots regardless of how many probes
# an EARLIER call already left running, defeating the point of a shared cap).
# Measured (see docs/architecture/fleet-catalog-discovery-budget.md): a cold
# stdio child's own connect+handshake already costs ~2.7-2.9s on this hardware
# before any real work, so a 61-server fleet queued 16-wide needed 4 full waves
# to even ATTEMPT every server once — comfortably exceeding any interactive
# budget on its own, before counting genuinely slow/unreachable servers.
#
# LOWERED 32 -> 8 (BUG-PE-055). That reasoning optimised for "attempt every
# server soon" and ignored that each probe carries its OWN wall-clock timeout:
# raising concurrency past what one event loop can actually service makes every
# in-flight probe MISS that timeout, so the sweep attempts more servers and
# finishes fewer. Measured live 2026-08-25, same 66-server homelab fleet, same
# process, only this number varied (successful servers / tools written):
#
#     32 ->  4/66,   361 tools      8 -> 65/66, 7975 tools
#      6 -> 58/66,  6291 tools      4 -> 59/66, 7422 tools
#      3 -> 60/66,  9008 tools
#
# Servers that individually probe in 3.5-7.8s were uniformly failing their own
# 10-15s deadline at 32-wide — one loop decoding ~10k tool schemas cannot keep
# 32 probes inside their deadlines. 8 completes the fleet in ~55s, inside the
# connectors lane's 108s fleet-probe budget, with room to spare.
_PROBE_CONCURRENCY = 8


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


def _child_transport_is_remote(cfg: dict, config: RuntimeConfig) -> bool | None:
    """Whether one child speaks HTTP; ``None`` when its declaration is invalid.

    A child is remote (HTTP) when it declares a ``url`` or an http/sse
    ``transport``; otherwise it is a local stdio subprocess run via
    ``command``. Either kind loads transparently from the same config.
    """

    command, _, _, is_remote = _child_transport_values(cfg, cfg.get("command"), config)
    if is_remote is None:
        logger.error("MCP child transport declaration is invalid")
        return None
    if not command and not is_remote:
        logger.warning("MCP child has neither command nor URL; skipping")
        return None
    return is_remote


def _child_timeout_admissible(cfg: dict) -> bool:
    """Whether one child's declared call timeout is inside the safety boundary."""

    try:
        timeout = float(cfg.get("timeout", 300.0))
    except (TypeError, ValueError):
        timeout = 0.0
    if not 0.001 <= timeout <= 3_600.0:
        logger.error("MCP child timeout is outside the safety boundary")
        return False
    return True


def _child_pool_size(cfg: dict, is_remote: bool, config: RuntimeConfig) -> int | None:
    """Session-pool sizing (CONCEPT:MERISTEM-ECO.mcp.profile-differences-from-client).

    Remote children may hold N independent connections for parallel in-flight
    calls; stdio children are single-pipe and always keep exactly one session.
    ``None`` means the declaration is outside the safety boundary.
    """

    if not is_remote:
        return 1
    try:
        pool_size = int(cfg.get("pool_size") or config.mcp_child_pool_size)
    except (TypeError, ValueError):
        pool_size = 0
    if not 1 <= pool_size <= 64:
        logger.error("MCP child pool size is outside the safety boundary")
        return None
    return pool_size


def _child_launch_shape(cfg: dict, config: RuntimeConfig) -> tuple[bool, int] | None:
    """``(is_remote, pool_size)`` for one child, or ``None`` when any part of its
    declaration is outside a safety boundary (already logged by the gate that
    rejected it)."""

    is_remote = _child_transport_is_remote(cfg, config)
    if is_remote is None or not _child_timeout_admissible(cfg):
        return None
    pool_size = _child_pool_size(cfg, is_remote, config)
    if pool_size is None:
        return None
    return is_remote, pool_size


def _probe_timeout_seconds(cfg: dict, timeout: float | None) -> float:
    """This probe's own deadline in seconds; ``0.0`` when the declaration is
    unusable (the caller rejects anything outside its safety boundary)."""

    try:
        return float(
            timeout
            if timeout is not None
            else cfg.get("probe_timeout", cfg.get("timeout", 10.0))
        )
    except (TypeError, ValueError):
        return 0.0


async def _run_bounded_probe(
    probe: _typing.Any, probe_to: float
) -> tuple[dict, _typing.Any]:
    """Run one probe coroutine under its deadline into ``(info, binding)``.

    Every failure mode becomes an honest ``error`` entry rather than an
    exception: a probe of an unreachable server must not fail the sweep.
    """

    try:
        (
            tools,
            resources,
            resource_templates,
            native_prompts,
            skills,
            prompts,
            family_errors,
            binding,
        ) = await asyncio.wait_for(probe(), timeout=probe_to)
    except TimeoutError:
        return (
            {
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "native_prompts": [],
                "catalog_family_errors": {},
                "skills": [],
                "prompts": [],
                "error": f"timeout after {probe_to:g}s",
            },
            None,
        )
    except Exception as e:
        logger.warning("_run_bounded_probe failed", exc_info=True)
        return (
            {
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "native_prompts": [],
                "catalog_family_errors": {},
                "skills": [],
                "prompts": [],
                "error": _format_probe_error(e),
            },
            None,
        )
    return (
        {
            "tools": tools,
            "resources": resources,
            "resource_templates": resource_templates,
            "native_prompts": native_prompts,
            "catalog_family_errors": family_errors,
            "skills": skills,
            "prompts": prompts,
            "error": None,
        },
        binding,
    )


# Prefixes are derived 100% algorithmically (no per-server lookup table), so any
# MCP server — bundled or third-party — gets a sensible, unique prefix with zero
# code changes. Pin a specific one per server via the ``prefix`` key in its
# AgentComponent entry; uniqueness across the fleet is then guaranteed by the
# catalog-aware collision resolver (:meth:`MCPMultiplexer._build_prefix_map`).


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


def _format_probe_error(exc: BaseException) -> str:
    """Render leaf exception types **and their messages** from a probe failure.

    Reporting only ``type(exc).__name__`` (the prior behavior) collapses every
    distinct failure — a DNS error, a TLS failure, and a JWT issuer mismatch —
    into the same bare "RuntimeError", making `find_tools`/`load_tools`/
    `probe_catalog` undiagnosable from the caller's side. The leaf message is
    the actual signal (e.g. an auth server's "issuer mismatch (got X, expected
    Y)"), so it is included, truncated to a bounded length per leaf so one
    verbose exception can't blow out the aggregate catalog response."""
    leaves: list[str] = []

    def _walk(e: BaseException) -> None:
        subs = getattr(e, "exceptions", None)
        if isinstance(e, BaseExceptionGroup) or (
            subs and isinstance(subs, list | tuple)
        ):
            for sub in subs or []:
                _walk(sub)
        else:
            name = type(e).__name__
            msg = str(e).strip()
            leaves.append(f"{name}: {msg[:300]}" if msg else name)

    _walk(exc)
    # de-dup while preserving order
    seen = list(dict.fromkeys(leaves))
    return "; ".join(seen) if seen else type(exc).__name__


def _close_abandoned_provider_projection(
    task: concurrent.futures.Future[_typing.Any],
) -> None:
    """Erase a provider projection that completed after its caller left."""

    if task.cancelled():
        return
    try:
        projection = task.result()
    except Exception:
        logger.debug("Abandoned provider projection failed", exc_info=True)
        return
    projection.close()


def _provider_child_sandbox_environment(
    stack: contextlib.AsyncExitStack,
) -> dict[str, str]:
    """Return a private empty home/XDG tree for one provider child."""

    sandbox = tempfile.TemporaryDirectory(prefix="agent-provider-child-")
    stack.callback(sandbox.cleanup)
    root = Path(sandbox.name)
    roots = {
        "config": root / "config",
        "data": root / "data",
        "state": root / "state",
        "cache": root / "cache",
        "runtime": root / "runtime",
        "temp": root / "temp",
    }
    for path in (root, *roots.values()):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            logger.debug("Provider sandbox permission update failed", exc_info=True)
    return {
        "HOME": str(root),
        "USERPROFILE": str(root),
        "APPDATA": str(roots["config"]),
        "LOCALAPPDATA": str(roots["data"]),
        "XDG_CONFIG_HOME": str(roots["config"]),
        "XDG_DATA_HOME": str(roots["data"]),
        "XDG_STATE_HOME": str(roots["state"]),
        "XDG_CACHE_HOME": str(roots["cache"]),
        "XDG_RUNTIME_DIR": str(roots["runtime"]),
        "ARBORA_CONFIG_DIR": str(roots["config"]),
        "ARBORA_DATA_DIR": str(roots["data"]),
        "ARBORA_CACHE_DIR": str(roots["cache"]),
        "TEMP": str(roots["temp"]),
        "TMP": str(roots["temp"]),
    }


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


def _tenant_local_discovery_binding() -> _typing.Any | None:
    """Mint the non-OAuth discovery visibility contract from verified state.

    Local/stdio children have no provider grant to resolve.  Their discovery
    is still not caller-authorized: the process-owned multiplexer may expose a
    tenant-local snapshot only while a verified graph session is ambient.  Do
    not derive an OAuth-like digest from roles/scopes or accept catalog fields.
    """
    try:
        from arbora.fleet.discovery_authority import (
            TenantLocalDiscoveryBinding,
        )
        from arbora.security.session import current_session

        session = current_session()
        if session is None or not getattr(session.actor, "authenticated", False):
            return None
        tenant = str(session.tenant or "").strip()
        if not tenant:
            return None
        return TenantLocalDiscoveryBinding(tenant_id=tenant)
    except (ImportError, PermissionError, TypeError, ValueError):
        return None


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
        # CONCEPT:MERISTEM-KG.retrieval.unified-capability-contract — the one local
        # skill entry (arbora.fleet.local_skill_catalog), built lazily and
        # cached like the probe cache above: every skill resolved from a
        # provider installed in THIS process (arbora's own, meristem's
        # own, and any sibling package declaring the same
        # ``scion_sdk.connector`` declaration), filed under the
        # pseudo-server ``LOCAL_SKILLS_SERVER`` so it rides the SAME
        # find_tools/list_catalog path as a fleet-harvested skill.
        self._local_skill_probe_cache: dict[str, _typing.Any] | None = None
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
        # ONE semaphore for the whole instance's lifetime (not re-created per
        # ``probe_catalog`` call) so a probe still running from an earlier
        # (possibly already-returned) call continues to count against the same
        # concurrency cap as a newer call's probes, instead of every call
        # getting its own fresh set of slots.
        self._probe_semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
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
            publish_live_tools=lambda *a, **k: self._publish_live_tools(*a, **k),
        )
        #: Installs the callback that schedules onboarding after a handshake.
        self.set_handshake_listener = self.transport.set_handshake_listener
        self.tasks = TaskForwarding(
            fleet=self._fleet,
            signer=self._signer,
            children=self.children,
            catalog_epoch=lambda: self._catalog_epoch,
            load_catalog=lambda *a, **k: self.load_catalog(*a, **k),
            mount_child=lambda *a, **k: self.mount_child(*a, **k),
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
                self.notify_pending_tools_changed(*a, **k)
            ),
            tool_dispatchable=lambda *a, **k: self.tool_dispatchable(*a, **k),
        )
        fleet.multiplexers.add(self)

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
            await self._refresh_child_tools(
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
        self._prefix_map = assigned
        self._prefix_reverse = {p: n for n, p in assigned.items()}

    def server_prefix(self, server_name: str) -> str:
        """The unique, collision-free prefix for a server (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).
        Falls back to the bare derived prefix for names outside the catalog."""
        if self._prefix_map is None:
            self._build_prefix_map()
        assert self._prefix_map is not None
        return self._prefix_map.get(server_name) or get_server_prefix(server_name)

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
        host = self._host_mcp
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
        if not changed or self._host_mcp is None:
            return set()

        replacements = sorted(name for name in changed if name in new_tools)
        removed = sorted(changed - set(replacements))
        host = self._host_mcp
        provider, previous_components = _local_provider_component_snapshot(host)

        # ``_fastmcp_tools.FunctionTool`` construction validates every child schema before
        # the live registry is touched.  Keep these real SDK objects in the
        # staged mapping — protocol ``_fastmcp_tools.Tool`` models are not executable host
        # components and cannot safely stand in for them.
        forwarders = {
            name: _forwarder_component(self, new_tools[name]) for name in replacements
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
        if self._host_mcp is None or not await _notify_tools_changed(self._host_mcp):
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
            for prefixed, (owner, _original) in self.tool_to_server.items()
            if owner == server_name
        }
        self.tool_to_server = {
            prefixed: target
            for prefixed, target in self.tool_to_server.items()
            if target[0] != server_name
        }
        self.aggregated_tools = [
            tool for tool in self.aggregated_tools if tool.name not in stale_names
        ]
        for tool in refreshed:
            self.tool_to_server[tool.name] = (server_name, originals[tool.name])
        self.aggregated_tools.extend(refreshed)
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
            tools = self.transport._admit_runtime_policy_tools(
                server_name, policy, tools
            )
        await self._refresh_child_tools(
            server_name, runtime, cfg, self._catalog_epoch, tools
        )

    async def _relist_live_child(self, server_name: str) -> None:
        """Re-read a mounted child's whole tool list on its live session."""
        session = self._live_primary_session(server_name)
        tools = await _list_all_tools(session)
        _bounded_tool_catalog(tools)
        await self._publish_live_tools(server_name, tools)
        await self.transport._announce_handshake(server_name, session, tools)

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
            catalog_epoch != self._catalog_epoch
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
        catalog_epoch = self._catalog_epoch
        result = await self._start_child(server_name, cfg)
        if not isinstance(result, tuple):
            return []
        s_name, payload, tools, r_cfg = result
        if catalog_epoch != self._catalog_epoch:
            # The async handshake raced a hot reload.  Do not register a child
            # that was authorized only by the retired catalog; its runtime
            # owns real transports and must be closed promptly.
            if isinstance(payload, _child_resilience.ChildRuntime):
                await payload.aclose()
            return []
        return self._register_child_result(s_name, payload, tools, r_cfg)

    def bind_tool_rows(self, reader: ToolRowReader) -> None:
        """Bind the one reader every tool-row site goes through (ARBORA-FLEET-R073.2)."""
        self._tool_rows = reader

    async def delegated_server_tools(
        self, server_name: str
    ) -> list[dict[str, _typing.Any]]:
        """Return one child's bounded tools through the served probe cache."""
        require_fleet_capability("discover", self._fleet.config)
        info = await self.probe_server(server_name)
        if info.get("error"):
            raise RuntimeError("MCP server probe failed")
        return await self._tool_rows.rows(server_name, info)

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
                for name, target in self.tool_to_server.items()
                if target == (server_name, tool_name)
            ),
            None,
        )
        if public_name is None:
            raise _fastmcp_exceptions.ToolError("MCP child tool is not available")
        result = await asyncio.wait_for(
            self.calls.call_proxied_tool(public_name, arguments), timeout=timeout
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
            pn for pn, (srv, _orig) in self.tool_to_server.items() if srv == server_name
        }
        return [t for t in self.aggregated_tools if t.name in names]

    async def start_children(self):
        """Parse configuration and start all child processes concurrently
        (eager mode). Lazy mode uses :meth:`mount_child` instead."""
        catalog = self.load_catalog()
        if not catalog:
            logger.info("No active child servers configured.")
            return

        catalog_epoch = self._catalog_epoch
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
            if catalog_epoch != self._catalog_epoch:
                if isinstance(payload, _child_resilience.ChildRuntime):
                    await payload.aclose()
                continue
            self._register_child_result(server_name, payload, tools, cfg)
        if self._catalog_state is _catalog_machine.CatalogState.SNAPSHOT:
            self._catalog_event(_catalog_machine.CatalogEvent.CHILDREN_UP)

    # ------------------------------------------------------------------
    # Always-load (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)
    # ------------------------------------------------------------------

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
        return self._server_for_prefixed(text), None

    def prefixed_for_original(self, server_name: str, original: str) -> str | None:
        """The aggregated prefixed name a child's ORIGINAL tool name maps to.

        Only answerable once ``server_name`` is mounted (``tool_to_server`` is
        populated by :meth:`_register_child_result`); returns ``None`` before
        that, or when the child never registered a tool by that name.
        """
        for prefixed, entry in self.tool_to_server.items():
            if entry == (server_name, original):
                return prefixed
        return None

    def always_load_declared(self) -> bool:
        """True when this multiplexer has any eager always-load declaration."""
        return bool(self._always_load_servers or self._always_load_tool_specs)

    # ------------------------------------------------------------------
    # Dynamic tool gateway (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)
    # ------------------------------------------------------------------

    def _server_for_prefixed(self, prefixed_name: str) -> str | None:
        """Resolve which child server owns a prefixed tool name, even before
        that child is mounted, via the collision-free reverse prefix map.
        Returns None if unknown."""
        if prefixed_name in self.tool_to_server:
            return self.tool_to_server[prefixed_name][0]
        if self._prefix_map is None:
            self._build_prefix_map()
        prefix = prefixed_name.split("__", 1)[0]
        return self._prefix_reverse.get(prefix)

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
        for prefixed, (srv, original) in self.tool_to_server.items():
            if srv != server_name:
                continue
            out.append(_live_tool_entry(original, self.tool_object(prefixed)))
        return out

    def _cache_probe(self, server_name: str, info: dict) -> dict:
        """Stamp ``probed_at`` (epoch seconds) and store ONE probe result.

        Every write to ``self._probe_cache`` goes through here so age/staleness
        can always be computed truthfully later — ``time.time() - probed_at`` —
        instead of a caller having to guess whether a served result is fresh
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        NE-008 (GOC-85 Deliverable 3): an OAuth-gated server's probe result is
        NEVER written into this shared, server-name-only cache — the result is
        bound to whichever principal happened to probe it, and this cache has
        no principal dimension. ``probed_at`` is still stamped on the returned
        ``info`` so the immediate caller (:meth:`probe_server`) can report a
        normal, honest result for THIS call; it is simply never reused for a
        later, possibly different, caller."""
        # A fresh probe replaces any prior private authority for this server,
        # including a failed OAuth probe.  A stale grant must never be reused
        # for a later catalog snapshot.
        self._drop_discovery_bindings_for_server(server_name)
        info["probed_at"] = time.time()
        cfg = self.load_catalog().get(server_name)
        if cfg is not None and oauth_gated(cfg):
            return info
        self._probe_cache[server_name] = self._tool_rows.resident(info)
        return info

    def _record_discovery_binding(
        self, server_name: str, info: dict[str, _typing.Any], binding: _typing.Any
    ) -> None:
        """Retain one typed binding for one exact probe result object.

        This is an internal hand-off only.  It intentionally accepts neither
        catalog metadata nor a caller-provided authority value; callers can
        only obtain a binding by completing a verified probe path above.
        Keeping the original object alongside its id makes id reuse harmless.
        """
        from arbora.fleet.discovery_authority import (
            OAuthGrantBinding,
            TenantLocalDiscoveryBinding,
        )

        if not isinstance(info, dict) or not isinstance(
            binding, (OAuthGrantBinding, TenantLocalDiscoveryBinding)
        ):
            return
        self._drop_discovery_bindings_for_server(server_name)
        self._discovery_binding_sidechannel[id(info)] = (server_name, info, binding)
        if isinstance(binding, TenantLocalDiscoveryBinding):
            self._local_discovery_cache_authority[id(info)] = (
                server_name,
                info,
                binding,
            )
        while len(self._discovery_binding_sidechannel) > 256:
            oldest = next(iter(self._discovery_binding_sidechannel))
            self._discovery_binding_sidechannel.pop(oldest, None)

    def _drop_discovery_bindings_for_server(self, server_name: str) -> None:
        """Forget private authority for a replaced/invalidated server probe."""
        for key, (recorded_server, _info, _binding) in tuple(
            self._discovery_binding_sidechannel.items()
        ):
            if recorded_server == server_name:
                self._discovery_binding_sidechannel.pop(key, None)
        for key, (recorded_server, _info, _binding) in tuple(
            self._local_discovery_cache_authority.items()
        ):
            if recorded_server == server_name:
                self._local_discovery_cache_authority.pop(key, None)

    def _rebound_cache_discovery_binding(
        self, server_name: _typing.Any, info: _typing.Any
    ) -> _typing.Any | None:
        """Re-mint local authority for an exact process-owned cached probe object.

        Non-OAuth results may be served from the process-owned cache after
        their prior side-channel record was consumed. Re-mint only for the
        exact cached object and a successful local probe that previously
        recorded verified provenance; copied/caller-shaped dictionaries never
        match.
        """
        cached_authority = self._local_discovery_cache_authority.get(id(info))
        if (
            cached_authority is None
            or cached_authority[0] != str(server_name)
            or cached_authority[1] is not info
        ):
            return None
        binding = _tenant_local_discovery_binding()
        if binding is None or binding.tenant_id != cached_authority[2].tenant_id:
            return None
        return binding

    def _take_discovery_bindings(
        self, catalog: _collections_abc.Mapping[str, _typing.Any]
    ) -> dict[str, _typing.Any]:
        """Consume private bindings for exact probe objects in ``catalog``.

        ``catalog`` is used only to identify which completed probe results the
        internal caller is syncing.  A copied or caller-shaped dictionary does
        not match the retained object identity, and any ``_discovery_binding``
        key in public metadata is ignored completely.
        """
        if not isinstance(catalog, _collections_abc.Mapping):
            return {}
        bindings: dict[str, _typing.Any] = {}
        for server_name, info in catalog.items():
            record = self._discovery_binding_sidechannel.get(id(info))
            if record is None:
                rebound = self._rebound_cache_discovery_binding(server_name, info)
                if rebound is not None:
                    bindings[str(server_name)] = rebound
                continue
            recorded_server, recorded_info, binding = record
            if recorded_server != str(server_name) or recorded_info is not info:
                continue
            bindings[str(server_name)] = binding
            self._discovery_binding_sidechannel.pop(id(info), None)
        return bindings

    def _bind_local_discovery_bindings(
        self, catalog: _collections_abc.Mapping[str, _typing.Any]
    ) -> None:
        """Bind exact local probe objects from the caller's verified context.

        ``source_sync`` may run the async multiplexer in a worker thread when
        its caller already owns an event loop; context variables do not cross
        that thread.  This explicit hand-off mints local authority back on the
        verified caller thread, but only for successful objects that are still
        the exact process-owned cache value.  It cannot authorize a copied or
        failed catalog result.
        """
        if not isinstance(catalog, _collections_abc.Mapping):
            return
        for server_name, info in catalog.items():
            if self._discovery_binding_sidechannel.get(id(info)) is not None:
                continue
            cached = self._probe_cache.get(str(server_name))
            cfg = self.load_catalog().get(str(server_name))
            if (
                cached is info
                and isinstance(cfg, _collections_abc.Mapping)
                and not oauth_gated(cfg)
                and isinstance(info, dict)
                and info.get("error") is None
            ):
                binding = _tenant_local_discovery_binding()
                if binding is not None:
                    self._record_discovery_binding(str(server_name), info, binding)

    def _probe_ttl(self) -> float:
        """The configured freshness window for a cached probe result
        (``MCP_CATALOG_PROBE_TTL`` / ``AgentConfig.mcp_catalog_probe_ttl``).
        Local import, same reason every other read of ``config`` in this
        module is local (avoid a module-load-time circular import with
        ``core.config``) (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness)."""
        return self._fleet.config.mcp_catalog_probe_ttl

    def _probe_cache_hit(
        self, server_name: str, now: float | None = None
    ) -> dict | None:
        """Return the cached probe for ``server_name`` iff it is still inside
        the TTL, else ``None`` — the ONE place that decides whether a cached
        entry may be served as-is, so :meth:`probe_server` (single-server
        short-circuit) and :meth:`probe_catalog` (whole-fleet re-probe
        targeting) can never disagree about what counts as fresh
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness).

        NE-008: always a miss for an OAuth-gated server (see
        :meth:`_cache_probe`) — there is never a cached entry to serve, so
        every call for one of these servers re-probes live, bound to
        whichever principal is asking THIS time."""
        cfg = self.load_catalog().get(server_name)
        if cfg is not None and oauth_gated(cfg):
            return None
        info = self._probe_cache.get(server_name)
        if info is None:
            return None
        if now is None:
            now = time.time()
        if (now - info.get("probed_at", now)) > self._probe_ttl():
            return None
        return info

    @staticmethod
    def _probe_age_and_staleness(
        info: dict, now: float, ttl: float
    ) -> tuple[float, bool]:
        """Honest ``(age_s, is_stale)`` for a served probe-cache entry
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness), computed
        from wall-clock age against ``ttl`` — never a bare echo of the
        narrow ``stale`` flag :meth:`probe_catalog` sets only when a result
        is recycled mid-call (that flag stays ``False`` forever on a
        normally-settled cache entry no matter how old, which is exactly how
        an 80-hour-old entry was once served as ``stale: false``). Still ORs
        in that narrower flag, since a just-recycled result is honestly "not
        this round's live answer" even inside the TTL window."""
        age_s = round(now - info.get("probed_at", now), 3)
        return age_s, bool(info.get("stale")) or age_s > ttl

    async def _live_child_probe(self, server_name: str) -> dict:
        """The probe answer for an ALREADY-MOUNTED child — read from its live
        session rather than paying a fresh connect."""
        session = self._live_primary_session(server_name)
        (
            resources,
            templates,
            native_prompts,
            skills,
            prompts,
            family_errors,
        ) = await self._probe_protocol_families(server_name, session)
        info: dict[str, _typing.Any] = {
            "tools": self._live_tools_for_server(server_name),
            "resources": resources,
            "resource_templates": templates,
            "native_prompts": native_prompts,
            "catalog_family_errors": family_errors,
            "skills": skills,
            "prompts": prompts,
            "error": None,
        }
        result = self._cache_probe(server_name, info)
        self._record_discovery_binding(
            server_name, result, _tenant_local_discovery_binding()
        )
        return result

    async def _relay_read(
        self,
        server_name: str,
        read: _typing.Callable[[_typing.Any], _typing.Awaitable[_typing.Any]],
        timeout: float,
    ) -> _typing.Any:
        """Run one idempotent relay read on the child's live primary session.

        ARBORA-FLEET-R040.2: a child that restarted no longer knows the session.
        That failure tears the generation down, waits a bounded time for the
        supervisor to reconnect, and retries the read once on the new session.
        Any other failure, or a second dead session, propagates unchanged.
        """
        session = self._live_primary_session(server_name)
        try:
            return await asyncio.wait_for(read(session), timeout=timeout)
        except Exception as exc:
            runtime = self.children.get(server_name)
            if runtime is None or not _child_resilience.is_transient_child_death(exc):
                raise
            if not await runtime.recover("relay_read_dead_session"):
                raise
            logger.info(
                "MCP child %s session re-established for a relay read", server_name
            )
        session = self._live_primary_session(server_name)
        return await asyncio.wait_for(read(session), timeout=timeout)

    def _live_primary_session(self, server_name: str) -> _typing.Any:
        session = self.sessions.get(server_name)
        if session is None:
            raise RuntimeError("mounted child has no live primary session")
        return session

    async def probe_server(
        self, server_name: str, force: bool = False, timeout: float | None = None
    ) -> dict:
        """Probe ONE catalog server for its tool list: connect → list_tools →
        release (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). Returns (and caches) ``{"tools": [...],
        "error": str|None, "probed_at": float}``. An already-mounted child reuses
        its live tools instead of reconnecting; an unreachable server records its
        error string (so find_tools/load_tools can report *why* it is
        unavailable) rather than raising.

        A cache hit is honored only inside the TTL
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness,
        :meth:`_probe_cache_hit`) — an entry aged past
        ``mcp_catalog_probe_ttl`` is treated as a miss and re-probed here,
        same as ``force=True``, so a cache entry can never go stale forever."""
        if not force:
            cached = self._probe_cache_hit(server_name)
            if cached is not None:
                return cached

        if server_name in self.children:
            if force:
                await self._relist_live_child(server_name)
            return await self._live_child_probe(server_name)

        cfg = self.load_catalog().get(server_name)
        if cfg is None:
            info: dict[str, _typing.Any] = {"tools": [], "error": "not in catalog"}
            return self._cache_probe(server_name, info)

        probe_to = _probe_timeout_seconds(cfg, timeout)
        if not 0.001 <= probe_to <= 300.0:
            info = {"tools": [], "error": "invalid probe timeout"}
            return self._cache_probe(server_name, info)

        async def _probe() -> tuple[
            list[dict],
            list[dict],
            list[dict],
            list[dict],
            list[dict],
            list[dict],
            dict[str, str],
            _typing.Any | None,
        ]:
            # Enter AND exit the transports within this single coroutine so the
            # anyio cancel scopes are not crossed between tasks. ``wait_for``
            # runs this whole coroutine as one task, so the stack is opened and
            # closed in the same task even on timeout-cancellation. (Wrapping
            # only the connect in wait_for would exit the scope in a different
            # task — "Attempted to exit cancel scope in a different task".)
            runtime_cfg, runtime_policy = _prepare_runtime_child_policy(
                cfg, self._fleet.config
            )
            binding_token = _CURRENT_DISCOVERY_BINDING.set(None)
            try:
                async with contextlib.AsyncExitStack() as stack:
                    session = await self.transport._open_one_session(
                        server_name, runtime_cfg, stack
                    )
                    tools = await _list_all_tools(session)
                    _bounded_tool_catalog(tools)
                    if force:
                        # An explicit refresh also re-onboards a changed
                        # catalog into the engine (ARBORA-FLEET-R070).
                        await self.transport._announce_handshake(
                            server_name, session, tools
                        )
                    if runtime_policy is not None:
                        tools = self.transport._admit_runtime_policy_tools(
                            server_name,
                            runtime_policy,
                            tools,
                        )
                    (
                        resources,
                        templates,
                        native_prompts,
                        skills,
                        prompts,
                        family_errors,
                    ) = await self._probe_protocol_families(
                        server_name,
                        session,
                    )
                    discovery_binding = _CURRENT_DISCOVERY_BINDING.get()
                    if discovery_binding is None:
                        discovery_binding = _tenant_local_discovery_binding()
                    return (
                        _bounded_tool_catalog(tools),
                        resources,
                        templates,
                        native_prompts,
                        skills,
                        prompts,
                        family_errors,
                        discovery_binding,
                    )
            finally:
                _CURRENT_DISCOVERY_BINDING.reset(binding_token)
                if runtime_policy is not None:
                    _close_runtime_child_policy(runtime_policy)
                    self._child_policy_admitted_tools.pop(server_name, None)

        info, discovery_binding = await _run_bounded_probe(_probe, probe_to)
        result = self._cache_probe(server_name, info)
        if discovery_binding is not None and info.get("error") is None:
            self._record_discovery_binding(server_name, result, discovery_binding)
        return result

    @staticmethod
    def _optional_method_missing(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("method not found", "not supported", "no such method")
        )

    async def _probe_protocol_families(
        self,
        server_name: str,
        session: _typing.Any,
    ) -> tuple[
        list[dict],
        list[dict],
        list[dict],
        list[dict],
        list[dict],
        dict[str, str],
    ]:
        """Read all native discovery families once from one child generation."""
        errors: dict[str, str] = {}
        try:
            raw_resources = await _list_all(session, "list_resources", "resources")
        except Exception as exc:
            logging.getLogger(__name__).debug("optional protocol method", exc_info=True)
            if not self._optional_method_missing(exc):
                errors["resources"] = type(exc).__name__
            raw_resources = []
        try:
            resources = _bounded_descriptor_catalog(
                raw_resources, key="uri", family="resource"
            )
            skills = _bounded_skill_catalog(raw_resources)
            prompt_resources = _bounded_prompt_catalog(raw_resources)
        except RuntimeError as exc:
            errors["resources"] = type(exc).__name__
            resources, skills, prompt_resources = [], [], []

        try:
            raw_templates = await _list_all(
                session, "list_resource_templates", "resource_templates"
            )
            templates = _bounded_descriptor_catalog(
                raw_templates,
                key="uriTemplate",
                family="resource-template",
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("optional protocol method", exc_info=True)
            if not self._optional_method_missing(exc):
                errors["resource_templates"] = type(exc).__name__
            templates = []

        try:
            native_prompts = _bounded_descriptor_catalog(
                await _list_all(session, "list_prompts", "prompts"),
                key="name",
                family="prompt",
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("optional protocol method", exc_info=True)
            if not self._optional_method_missing(exc):
                errors["prompts"] = type(exc).__name__
            native_prompts = []

        self._fill_bodies_from_local_catalog(skills, "instructions")
        self._fill_bodies_from_local_catalog(prompt_resources, "body")
        return resources, templates, native_prompts, skills, prompt_resources, errors

    async def _probe_resource_family(
        self,
        server_name: str,
        session: _typing.Any,
        *,
        catalog_builder: _typing.Callable[[_typing.Any], list[dict]],
        kind: str,
        body_field: str,
    ) -> list[dict]:
        """Best-effort ``resources/list``-backed enumeration for one probed
        session, shared by ``_probe_skills`` and ``_probe_prompts``: both
        resource families degrade to an empty list, never a failed probe,
        when the session doesn't support resource discovery at all, or when
        this server's catalog for the family is malformed. Each entry's body
        comes from the resident local catalog only
        (``_fill_bodies_from_local_catalog``).
        """
        try:
            resources = await _list_all(session, "list_resources", "resources")
        except Exception as exc:  # resources/list is an OPTIONAL
            # MCP method; a server that doesn't implement it must still
            # contribute the tools/skills its probe already returned. The
            # cause IS logged so a real transport failure stays diagnosable.
            logger.debug(
                "Server %s does not support %s resource discovery: %s: %s",
                server_name,
                kind,
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
            return []
        try:
            entries = catalog_builder(resources)
        except Exception as exc:  # a malformed catalog from one
            # server must not fail the tool probe that already succeeded.
            # The cause IS logged.
            logger.warning(
                "Server %s returned an invalid %s resource catalog: %s: %s",
                server_name,
                kind,
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
            return []
        self._fill_bodies_from_local_catalog(entries, body_field)
        return entries

    async def _probe_skills(
        self,
        server_name: str,
        session: _typing.Any,
    ) -> list[dict]:
        """Best-effort ``skill://`` resource enumeration for one probed session
        (CONCEPT:MERISTEM-ECO.mcp.skills-over-mcp-provider).

        Skills-over-MCP is only a fastmcp-4 server capability (draft MCP
        SEP-2640) — a fastmcp-3 or pre-skills server has no ``skill://``
        resources, and some MCP servers do not implement ``resources/list`` at
        all. Either case degrades to an empty list rather than failing the
        tool probe that already succeeded above.
        """
        return await self._probe_resource_family(
            server_name,
            session,
            catalog_builder=_bounded_skill_catalog,
            kind="skill",
            body_field="instructions",
        )

    def _fill_bodies_from_local_catalog(
        self, entries: list[dict], body_field: str
    ) -> None:
        """Fill each entry's body from the resident local-skill catalog.

        ARBORA-FLEET-R006.3.2: the catalog
        (``arbora.fleet.local_skill_catalog.build_local_skill_catalog``) is the
        only source of a skill or prompt body. A name it does not admit keeps
        no body; nothing reads a body back over the wire from the child.
        """
        try:
            local_entries = self._local_skill_probe_info().get("skills") or []
        except AttributeError:
            # A bare ``MCPMultiplexer.__new__(MCPMultiplexer)`` test fixture
            # never ran ``__init__`` and so has no ``_local_skill_probe_cache``
            # to build from; resolve afresh rather than crash.
            local_entries, _problems = build_local_skill_catalog()
        local_bodies = {
            item["name"]: item["instructions"]
            for item in local_entries
            if item.get("name") and item.get("instructions")
        }
        for entry in entries:
            local_body = local_bodies.get(entry.get("name"))
            if local_body is not None:
                entry[body_field] = local_body

    async def _probe_prompts(
        self,
        server_name: str,
        session: _typing.Any,
    ) -> list[dict]:
        """Best-effort ``prompt://`` resource enumeration for one probed
        session (CONCEPT:MERISTEM-ECO.mcp.cross-process-prompt-harvest).

        Prompts-over-MCP is served by every server built through
        ``server_factory.build_server`` (``_register_prompt_providers``), but
        a raw MCP child outside that factory — or one with no
        ``prompts/`` directory — has no ``prompt://`` resources; either case
        degrades to an empty list rather than failing the tool probe that
        already succeeded. A server that also doesn't implement
        ``resources/list`` at all degrades the same way.
        """
        return await self._probe_resource_family(
            server_name,
            session,
            catalog_builder=_bounded_prompt_catalog,
            kind="prompt",
            body_field="body",
        )

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

    def _settle_probe_task(self, server: str, task: asyncio.Task) -> None:
        """``add_done_callback`` for a background probe: release its in-flight
        slot. ``probe_server`` itself catches every failure mode it knows about
        and always caches a result, so an exception surfacing here is a
        genuinely unexpected bug, not a child's fault — it is logged with its
        cause, never swallowed, rather than left to asyncio's "exception was
        never retrieved" warning."""
        # Only retract the join slot if it is still OURS. A forced probe for the
        # same server runs as its OWN untracked task; popping unconditionally
        # would evict a DIFFERENT, still-running non-forced probe from the join
        # map, so a later call would spawn a duplicate of a probe already in
        # flight and ``aclose`` would no longer know to cancel it.
        if self._probe_inflight.get(server) is task:
            self._probe_inflight.pop(server, None)
        self._probe_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Unexpected exception from background probe of %s: %s: %s",
                server,
                type(exc).__name__,
                redact_for_log(exc),
            )

    def _ensure_probing(
        self, server: str, *, force: bool, timeout: float | None
    ) -> asyncio.Task:
        """Return the in-flight probe task for ``server``, joining one an
        overlapping call already started instead of spawning a duplicate.

        A forced probe (explicit re-probe, e.g. boot ingestion) always starts
        fresh and is never JOINED by a later caller — ``force`` means "I
        specifically want a new answer now," which a stale in-flight task
        cannot give. It is still registered in ``_probe_tasks`` so
        :meth:`aclose` can cancel it: "not shareable" must not mean "able to
        outlive the multiplexer".

        NE-008 (GOC-85 Deliverable 3): an OAuth-gated server ALWAYS runs as an
        untracked, un-joined task too, for the same reason a forced probe
        does — joining an in-flight task here would mean a SECOND principal's
        call returns the FIRST principal's probe result (the in-flight task's
        coroutine captured whichever actor was ambient when it was created via
        ``asyncio.create_task``), exactly the cross-principal catalog leak
        Deliverable 3 rules out. Never registered into ``_probe_inflight``
        either, so there is nothing for a later call to join."""
        cfg = self.load_catalog().get(server)
        oauth_gated_target = cfg is not None and oauth_gated(cfg)
        if not force and not oauth_gated_target:
            existing = self._probe_inflight.get(server)
            if existing is not None and not existing.done():
                return existing

        async def _run() -> dict:
            async with self._probe_semaphore:
                return await self.probe_server(server, force=force, timeout=timeout)

        task = asyncio.create_task(_run())
        self._probe_tasks.add(task)
        if not force and not oauth_gated_target:
            self._probe_inflight[server] = task
        # `server` is this call's own parameter (not a mutating loop variable), so a
        # plain closure captures it correctly without the default-arg-capture trick —
        # which also lets mypy infer the callback's type against
        # ``Task.add_done_callback``'s single-argument signature.
        task.add_done_callback(lambda t: self._settle_probe_task(server, t))
        return task

    def _probe_targets(
        self,
        catalog: dict[str, dict],
        servers: list[str] | tuple[str, ...] | None,
        force: bool,
    ) -> list[str]:
        """Which catalog servers this call must (re-)probe.

        A cached entry aged past ``mcp_catalog_probe_ttl`` is targeted exactly
        like an uncached one (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness).
        """
        candidates = (
            catalog if servers is None else (s for s in servers if s in catalog)
        )
        return [
            server
            for server in candidates
            if force or self._probe_cache_hit(server) is None
        ]

    def _spawn_probe_tasks(
        self,
        targets: list[str],
        force: bool,
        timeout: float | None,
        priority: _resource_priority.PriorityClass | None,
    ) -> dict[asyncio.Task, str]:
        """Start (or join) one probe per target under this call's priority scope."""
        scope = (
            _resource_priority.priority_scope(priority)
            if priority is not None
            else contextlib.nullcontext()
        )
        with scope:
            return {
                self._ensure_probing(server, force=force, timeout=timeout): server
                for server in targets
            }

    def _settled_probe_results(
        self,
        tasks: dict[asyncio.Task, str],
        pending: frozenset[asyncio.Task] | set[asyncio.Task] = frozenset(),
    ) -> dict[str, dict]:
        """Fold every SETTLED probe task's answer over the cached fleet view.

        A task that is still running, was cancelled, or raised contributes
        nothing — one unreachable server can never take the sweep down with it.
        """
        result = dict(self._probe_cache)
        for task, server in tasks.items():
            if task in pending or task.cancelled() or not task.done():
                continue
            try:
                info = task.result()
            except BaseException:
                logging.getLogger(__name__).debug(
                    "one server never fails the sweep", exc_info=True
                )
                continue
            if isinstance(info, dict):
                result[server] = info
        return result

    def _unsettled_probe_entry(self, server: str, now: float, budget: float) -> dict:
        """The honest answer for a server still probing when the budget expired.

        Reachable two ways: an explicit force re-probe, or a TTL-expired cache
        entry (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness)
        re-targeted by ``_probe_targets`` — either way, serve the last known
        answer rather than a bare "unavailable", labelled so the caller knows
        it is not this round's live result. With no prior answer at all, say
        so explicitly rather than implying the server is unreachable.
        """
        prior = self._probe_cache.get(server)
        if prior is None:
            return {
                "tools": [],
                "error": (
                    f"still probing after {budget:g}s (no result yet) — "
                    "the probe continues in the background; call again shortly"
                ),
                "pending": True,
            }
        stale = dict(prior)
        stale["stale"] = True
        stale["age_s"] = round(now - prior.get("probed_at", now), 3)
        return stale

    async def probe_catalog(
        self,
        force: bool = False,
        timeout: float | None = None,
        budget: float | None = None,
        servers: list[str] | tuple[str, ...] | None = None,
        priority: _resource_priority.PriorityClass | None = None,
    ) -> dict[str, dict]:
        """Probe every catalog server concurrently (bounded) and cache the
        result, so find_tools can rank the whole fleet's real tools.

        ``budget`` bounds how long THIS CALL waits for an answer. It does
        **not** bound how long an individual probe is allowed to run — that
        is each server's own ``probe_timeout``/``timeout``, honored
        independently by :meth:`probe_server`. A server still probing when
        the budget expires is NEVER cancelled: cancelling it would also
        cancel its own ``self._probe_cache`` write, which is exactly what
        used to make an unreachable server pay its full connect cost again
        on every subsequent call, forever, instead of the cache the tool's
        own contract promises ("the first call probes the fleet; later
        calls are cached"). It keeps running toward its own deadline in the
        background — ``_probe_inflight`` lets a later call join that SAME
        probe instead of duplicating it — and each pending server gets an
        honest, individual answer here: a prior (possibly ``stale``,
        age-labelled) result if one exists, or an explicit ``pending``
        marker on its first-ever attempt. Reachable servers are never
        blocked by an unreachable one (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        A long-lived multiplexer (arbora's fleet loader) relies on
        :meth:`aclose` to cancel whatever is still in-flight at real
        shutdown. A short-lived caller wrapped in ``asyncio.run(...)``
        (e.g. ``source_sync._sync_fleet`` via ``_run_async``) gets the same
        guarantee for free — ``asyncio.run`` cancels every task still on its
        loop when the coroutine it ran returns, this multiplexer's included
        — so no caller needs to await these background probes itself.

        ``priority`` tags this call's probe tasks with a
        :class:`_resource_priority.PriorityClass` (ORCH-1.98) for their whole background
        lifetime — pass ``_resource_priority.PriorityClass.BACKGROUND_INGESTION`` for a broad
        catalog sweep so it yields shared-resource contention to
        interactive/orchestration work; leave unset to inherit the
        caller's ambient priority (the default — appropriate for a small,
        caller-named set of servers on the interactive path).

        ``servers`` narrows a latency-sensitive first stage without
        creating a second probe path.

        A cached entry aged past ``mcp_catalog_probe_ttl`` is targeted for
        re-probe exactly like an uncached one
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness) — the fleet
        cache never goes stale forever. The re-probe runs through the same
        ``_ensure_probing``/budget machinery as any other target, so it
        NEVER blocks this call past its own ``budget``: a caller that hits
        the deadline before the refresh lands still gets the last-known
        answer, honestly labelled ``stale`` with its real ``age_s``, while
        the refresh keeps running in the background for the next call."""
        catalog = self.load_catalog()
        targets = self._probe_targets(catalog, servers, force)
        if not targets:
            return self._probe_cache

        if budget is not None and not 0.001 <= budget <= 300.0:
            raise ValueError("catalog probe budget is outside the safety boundary")

        tasks = self._spawn_probe_tasks(targets, force, timeout, priority)

        if budget is None:
            await asyncio.gather(*tasks, return_exceptions=True)
            return self._settled_probe_results(tasks)

        _done, pending = await asyncio.wait(tasks, timeout=budget)
        result = self._settled_probe_results(tasks, pending)
        if pending:
            now = time.time()
            for task in pending:
                server = tasks[task]
                result[server] = self._unsettled_probe_entry(server, now, budget)
        return result

    def _server_level_fallback(self) -> list[dict]:
        """When the KG yields no tool-level index (cold/absent KG), still let
        the caller act: surface mountable servers so they can ``load_tools`` by
        server name."""
        out: list[dict] = []
        for server in self.load_catalog():
            out.append(
                {
                    "server": server,
                    "tool": "*",
                    "prefixed_name": None,
                    "description": (
                        f"All tools for '{server}'. KG tool-level discovery "
                        "is unavailable; load the whole server by name."
                    ),
                    "score": 0.0,
                    "mountable": True,
                    "mounted": server in self.children,
                }
            )
        return out

    @staticmethod
    def _priority_catalog_servers(
        query: str, catalog: _collections_abc.Mapping[str, _typing.Any]
    ) -> list[str]:
        """Return high-confidence server-name matches for staged discovery."""

        query_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        ranked: list[tuple[float, int, str]] = []
        for server in catalog:
            identity_tokens = set(re.findall(r"[a-z0-9]+", server.lower()))
            identity_tokens -= _SERVER_DISCOVERY_STOPWORDS
            if not identity_tokens:
                continue
            overlap = query_tokens & identity_tokens
            coverage = len(overlap) / len(identity_tokens)
            if overlap and coverage >= 0.5:
                ranked.append((coverage, len(overlap), server))
        ranked.sort(reverse=True)
        return [server for _coverage, _overlap, server in ranked]

    async def _discovery_probe(
        self,
        query: str,
        catalog: dict[str, dict],
        discovery_timeout: float,
        deadline: float,
    ) -> dict:
        """The probe view backing one ``find_tools`` call, within its budget.

        A latency-sensitive first stage probes only the query-relevant servers.
        The broad fleet-wide sweep that follows (every server, not just those)
        is a background warm-up, not itself the interactive answer — tag it
        BACKGROUND_INGESTION (ORCH-1.98) so it yields shared-resource
        contention to interactive/orchestration work instead of competing with
        it, reusing the ONE existing priority gate rather than inventing a
        second (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).
        """
        loop = asyncio.get_running_loop()
        probe: dict = {}
        priority_servers = self._priority_catalog_servers(query, catalog)
        if priority_servers:
            probe = await self.probe_catalog(
                budget=discovery_timeout,
                servers=priority_servers,
            )
        remaining = deadline - loop.time()
        if remaining >= 0.001:
            probe = await self.probe_catalog(
                budget=remaining,
                priority=_resource_priority.PriorityClass.BACKGROUND_INGESTION,
            )
        return probe

    def _catalog_tool_probe_info(self) -> dict[str, dict[str, _typing.Any]]:
        """Per-server tool descriptors already admitted into the EG catalog
        snapshot (ARBORA-FLEET-R031), read with NO live probe.

        Fleet onboarding (``arbora/fleet/onboarding.py``) imports each
        admitted server's content pack — its tools included — into the EG
        catalog before any child is ever mounted
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). This reads that
        already-imported ``kind == "tool"`` provenance straight off
        ``self._fleet_catalog`` (installed by :meth:`refresh_engine_catalog`)
        so a server whose live probe cannot answer in time still has
        descriptors ``discover_tools`` can rank — the live probe stays the
        freshness refresh, never the only source. Every row is stamped
        ``stale: True`` and ages from the snapshot's own load time, never
        faked as a live answer.
        """
        catalog = self._fleet_catalog
        if catalog is None:
            return {}
        probed_at = self._fleet_catalog_loaded_at or 0.0
        out: dict[str, dict[str, _typing.Any]] = {}
        for server in catalog.servers:
            tools = [
                {"name": item.entry.upstream_name, "description": item.entry.summary}
                for item in server.provides
                if item.entry.kind == "tool" and item.entry.upstream_name
            ]
            if not tools:
                continue
            out[server.component.server_name] = {
                "tools": tools,
                "skills": [],
                "error": None,
                "stale": True,
                "probed_at": probed_at,
                "source": "catalog_snapshot",
            }
        return out

    def _local_skill_probe_info(self) -> dict[str, _typing.Any]:
        """Build (once) and cache the local-skill pseudo-server probe entry.

        "Local" means every skill ``arbora.core.providers.
        resolve_skill_provider_dirs()`` resolves in THIS process — arbora's
        own skills, meristem's own, and any sibling package installed
        alongside arbora that declares ``scion_sdk.connector``
        (CONCEPT:MERISTEM-KG.retrieval.unified-capability-contract). An
        individually unreadable/malformed skill, and a core-pack name no
        installed provider resolves, are reported loudly here (never fatal,
        never silent) the one time this cache is built.
        """
        if self._local_skill_probe_cache is not None:
            return self._local_skill_probe_cache
        entries, problems = build_local_skill_catalog()
        for problem in problems:
            logger.warning("Local skill catalog: %s", problem)
        for missing in missing_core_pack_names(entries):
            logger.warning(
                "Core-pack skill %r is not provided by any installed package",
                missing,
            )
        info: dict[str, _typing.Any] = {"tools": [], "skills": entries, "error": None}
        self._local_skill_probe_cache = info
        return info

    @staticmethod
    def _fleet_skills_without_local_duplicates(
        server: str, info: dict, local_names: set[str]
    ) -> list[dict] | None:
        """One server's skills with a local-duplicate name dropped, logged.

        Returns ``None`` when nothing collided, so the caller can keep the
        original entry untouched rather than rebuild an identical one.
        """
        skills = info.get("skills") or []
        kept = [s for s in skills if s.get("name") not in local_names]
        if len(kept) == len(skills):
            return None
        for dropped in skills:
            if dropped.get("name") in local_names:
                logger.info(
                    "Fleet-harvested skill %r from %r shadowed by a local "
                    "skill of the same name",
                    dropped.get("name"),
                    server,
                )
        return kept

    def _dedup_fleet_skills_against_local(
        self, probe: dict, local_names: set[str]
    ) -> dict:
        """Drop a fleet-harvested skill whose name collides with a local one.

        A local skill (served from a package installed in THIS process) takes
        precedence over the same name surfacing from a live-probed child: it
        is this process's own authoritative resolution and costs no child
        connection to read. Never mutates a cached probe entry in place — a
        filtered copy is returned only for a server whose skills actually
        collide, so ``self._probe_cache`` stays exactly what was harvested.
        """
        if not local_names:
            return probe
        deduped = dict(probe)
        for server, info in probe.items():
            if server == LOCAL_SKILLS_SERVER:
                continue
            kept = self._fleet_skills_without_local_duplicates(
                server, info, local_names
            )
            if kept is not None:
                deduped[server] = {**info, "skills": kept}
        return deduped

    def _results_or_fallback(self, ranked: list[dict], probe: dict) -> list[dict]:
        """The ranked rows, or every mountable server when nothing matched.

        Nothing matched but a reachable server exists, so the caller can still load
        by server; when every server errored the results stay empty and
        ``unavailable`` tells the story.
        """
        if ranked or all(info.get("error") for info in probe.values()):
            return ranked
        return self._server_level_fallback()

    async def _probe_rows(
        self, probe: dict, catalog: dict[str, dict], loaded: set[str]
    ) -> tuple[list[dict], dict[str, str]]:
        """Every loadable tool and skill row of the probe, and the servers that erred.

        One ranked capability space (CONCEPT:MERISTEM-KG.retrieval.unified-capability-contract):
        fleet tools AND fleet-served skill:// resources are rows of one list, each
        carrying a ``bind`` dict, the exact kwargs `graph_orchestrate` needs to run it.
        ARBORA-FLEET-R031: a server whose live probe errored (a timeout is the common
        case) is not thereby invisible: its already-admitted catalog snapshot (never a
        probe) is the fallback source, honestly marked stale.
        """
        now, ttl = time.time(), self._probe_ttl()
        fallbacks = self._catalog_tool_probe_info()
        rows: list[dict] = []
        unavailable: dict[str, str] = {}
        for server, info in probe.items():
            if info.get("error"):
                unavailable[server] = info["error"]
                info = fallbacks.get(server)
            if info:
                rows.extend(
                    await self._discovery_rows(server, info, catalog, loaded, now, ttl)
                )
        return rows, unavailable

    async def _discovery_rows(
        self,
        server: str,
        info: dict,
        catalog: dict[str, dict],
        loaded: set[str],
        now: float,
        ttl: float,
    ) -> list[dict]:
        """Every loadable tool AND skill row of one probed server, unordered.

        Truthful freshness for every surfaced tool/skill (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog):
        a row from a probe that ran seconds or minutes ago carries its real age,
        and ``is_stale`` is computed from that age against the TTL
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness). find_tools
        surfaces only loadable (enabled) tools; disabled-but-capable tools stay
        visible through list_catalog.
        """
        probe_age, is_stale = self._probe_age_and_staleness(info, now, ttl)
        common = {
            "mountable": server in catalog,
            "age_s": probe_age,
            "stale": is_stale,
        }
        rows: list[dict] = []
        for entry in await self._tool_rows.rows(server, info):
            tool = entry["name"]
            if not self._tool_enabled(server, tool):
                continue
            desc = entry.get("description", "")
            prefixed = clean_tool_name(self.server_prefix(server), server, tool)
            capability = Capability(
                kind="tool",
                id=f"tool_{server}_{tool}",
                name=tool,
                description=desc,
                score=0.0,
                server=server,
                source="fleet_probe",
            )
            rows.append(
                {
                    "kind": "tool",
                    "server": server,
                    "tool": tool,
                    "prefixed_name": prefixed,
                    "description": desc,
                    "mounted": prefixed in loaded,
                    "bind": capability.to_binding(),
                    **common,
                }
            )
        for entry in info.get("skills", []) or []:
            skill = entry.get("name")
            if not skill:
                continue
            desc = entry.get("description", "")
            capability = Capability(
                kind="skill",
                id=f"skill_{server}_{skill}",
                name=skill,
                description=desc,
                score=0.0,
                server=server,
                source="fleet_probe",
            )
            rows.append(
                {
                    "kind": "skill",
                    "server": server,
                    "skill": skill,
                    "uri": entry.get("uri", ""),
                    "description": desc,
                    "mounted": False,
                    "bind": capability.to_binding(),
                    **common,
                }
            )
        return rows

    async def _ordered_discovery_rows(
        self, query: str, rows: list[dict], top_k: int
    ) -> list[dict]:
        """The rows in the ranking port's order, the first ``top_k`` of them.

        ARBORA-FLEET-R083.3: a ranking is medulla's choice; the lexical floor answers
        when the engine cannot. The floor drops a row no routing term matches, as the
        retired scorer did; an engine order is kept whole, since the engine owns the
        semantic match. Each row's ``score`` is the floor's evidence for it.
        """
        from arbora.api.domain.fleet.lexical_floor import evidence
        from arbora.api.domain.fleet.ranking import RankCandidate

        candidates = [
            RankCandidate(
                f"{row['kind']}:{row['server']}:{row.get('tool') or row.get('skill')}",
                str(row.get("tool") or row.get("skill")),
                str(row["description"]),
                row["kind"],
            )
            for row in rows
        ]
        outcome = await self.fleet.ranking.aordered(
            "catalog.operation_rank", query, candidates
        )
        shown = evidence(query, ((c.id, c.name, c.description) for c in candidates))
        by_id = dict(zip((c.id for c in candidates), rows, strict=True))
        ordered: list[dict] = []
        for candidate_id in outcome.order:
            relevance = shown[candidate_id][0]
            if outcome.source == "floor" and relevance <= 0:
                continue
            ordered.append({**by_id[candidate_id], "score": round(relevance, 4)})
        return ordered[:top_k]

    async def discover_tools(
        self, query: str, top_k: int | None = None, loaded: set[str] | None = None
    ) -> dict:
        """Rank candidate tools across the whole fleet for an NL ``query``
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog), without exposing or holding any child.

        Backbone is the self-catalog (:meth:`probe_catalog` — each server's real
        tools, learned by a cached connect→list→release probe), ranked by token
        overlap; KG semantic-search scores are blended in when the KG is warm.
        The local-skill pseudo-server (:meth:`_local_skill_probe_info`) is
        folded in the SAME way, so a skill served from a package installed in
        this process ranks alongside fleet tools and fleet-harvested skills.
        Returns ``{"results": [...], "unavailable": {server: error}}`` so the
        caller can both pick tools and see which servers couldn't be reached.
        """
        if not top_k or top_k <= 0:
            top_k = self._fleet.config.mcp_dynamic_top_k
        catalog = self.load_catalog()
        discovery_timeout = self._fleet.config.mcp_dynamic_discovery_timeout
        deadline = asyncio.get_running_loop().time() + discovery_timeout
        fleet_probe = await self._discovery_probe(
            query, catalog, discovery_timeout, deadline
        )
        local_info = self._local_skill_probe_info()
        local_names = {s["name"] for s in local_info.get("skills", [])}
        probe = self._dedup_fleet_skills_against_local(fleet_probe, local_names)
        probe = {**probe, LOCAL_SKILLS_SERVER: local_info}
        rows, unavailable = await self._probe_rows(
            probe, catalog, loaded if loaded is not None else self._exposed
        )
        ranked = await self._ordered_discovery_rows(query, rows, top_k)
        # The server-level fallback (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)
        # answers "is any FLEET server mountable", so its trigger check uses
        # ``fleet_probe`` — never the merged ``probe`` — or the local-skill
        # pseudo-server's own always-``error: None`` entry would make every
        # fleet server look reachable even when none of them actually are.
        return {
            "results": self._results_or_fallback(ranked, fleet_probe),
            "unavailable": unavailable,
        }

    async def _catalog_detail_tools(
        self, server: str, prefix: str, info: dict
    ) -> list[dict]:
        """One drilled-down server's client-visible tool rows."""
        tools = await self._tool_rows.rows(server, info, schemas=True)
        return [
            {
                "prefixed_name": prefixed_name,
                "tool": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema") or t.get("input_schema") or {},
                "enabled": self._tool_enabled(server, t["name"]),
                # Session-scoped dispatch truth (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog):
                # derived from the SAME predicate the dispatch gate
                # (SessionVisibilityMiddleware) enforces, so this can
                # never claim a tool is usable when a call would
                # actually be rejected.
                "mounted": self.tool_dispatchable(prefixed_name),
            }
            for t in tools
            for prefixed_name in (clean_tool_name(prefix, server, t["name"]),)
        ]

    async def _catalog_server_detail(self, server: str, include_tools: bool) -> dict:
        """Drill into ONE catalog server, probing only that server."""
        info = await self.probe_server(server)
        prefix = self.server_prefix(server)
        age_s, is_stale = self._probe_age_and_staleness(
            info, time.time(), self._probe_ttl()
        )
        result = {
            "server": server,
            "prefix": prefix,
            # Process-level fact only: the child is spawned. It does NOT
            # mean any of its tools are callable by the CALLING session —
            # that per-tool truth is the "mounted" field inside "tools"
            # below, and it is the ONLY field a caller should read to
            # decide whether it can dispatch a specific tool right now.
            "process_running": server in self.children,
            "probed": True,
            "available": info.get("error") is None,
            "error": info.get("error"),
            "age_s": age_s,
            # CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness — a
            # drill-down calls probe_server directly, whose own cache hit
            # is already TTL-gated, so this is normally fresh; still
            # reported honestly rather than assumed.
            "stale": is_stale,
        }
        if include_tools:
            result["tools"] = await self._catalog_detail_tools(server, prefix, info)
        return result

    async def _catalog_fleet_probe(self, include_tools: bool) -> dict:
        """The probe view backing a whole-fleet listing.

        A whole-fleet browse is a background sweep, not a targeted interactive
        lookup: bound it by the same interactive discovery budget as find_tools
        (a server that never answers must not hang this call indefinitely) and
        tag it BACKGROUND_INGESTION so it yields to interactive/orchestration
        work (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). A metadata-only
        listing never probes a child at all. The local-skill pseudo-server
        (:meth:`_local_skill_probe_info`) is always folded in, so a plain
        listing shows it without needing a ``find_tools`` query — the
        always-offered core pack is a subset of exactly this set.
        """
        local_info = self._local_skill_probe_info()
        if not include_tools:
            return {**dict(self._probe_cache), LOCAL_SKILLS_SERVER: local_info}
        probe = await self.probe_catalog(
            budget=self._fleet.config.mcp_dynamic_discovery_timeout,
            priority=_resource_priority.PriorityClass.BACKGROUND_INGESTION,
        )
        return {**probe, LOCAL_SKILLS_SERVER: local_info}

    def _catalog_tool_partition(
        self, name: str, prefix: str, tool_entries: list[dict]
    ) -> tuple[list[str], list[str]]:
        """Split one server's probed tools into (enabled, disabled) prefixed names."""
        enabled_names: list[str] = []
        disabled_names: list[str] = []
        for t in tool_entries:
            pn = clean_tool_name(prefix, name, t["name"])
            target = (
                enabled_names if self._tool_enabled(name, t["name"]) else disabled_names
            )
            target.append(pn)
        return enabled_names, disabled_names

    def _stamp_catalog_freshness(
        self, entry: dict, info: dict, now: float, ttl: float
    ) -> None:
        """Stamp honest probe freshness onto one fleet-listing entry.

        CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness — staleness is
        computed from age vs TTL, never a bare echo of the narrow in-flight
        ``stale`` flag. This is also the ``include_tools=False`` metadata-only
        path, which reads ``self._probe_cache`` directly and never goes through
        ``probe_catalog``'s re-probe targeting at all — the exact path where an
        entry could otherwise sit at ``stale: false`` no matter how old.
        """
        entry["pending"] = bool(info.get("pending"))
        if "probed_at" not in info:
            entry["stale"] = bool(info.get("stale"))
            return
        age_s, is_stale = self._probe_age_and_staleness(info, now, ttl)
        entry["age_s"] = age_s
        entry["stale"] = is_stale

    async def _catalog_fleet_entry(
        self,
        name: str,
        info: dict,
        probed: bool,
        now: float,
        ttl: float,
        include_tools: bool,
    ) -> dict:
        """One server's row in a whole-fleet listing."""
        prefix = self.server_prefix(name)
        tool_entries = await self._tool_rows.rows(name, info)
        enabled_names, disabled_names = self._catalog_tool_partition(
            name, prefix, tool_entries
        )
        entry = {
            "server": name,
            "prefix": prefix,
            "tool_count": len(tool_entries),
            "enabled_count": len(enabled_names),
            # Process-level fact only (the child is spawned) — NOT a claim
            # that any tool is callable by the caller's own session. See
            # "dispatchable_tools" for the truthful, session-scoped answer.
            "process_running": name in self.children,
            "probed": probed,
            "available": info.get("error") is None if probed else None,
        }
        if probed:
            self._stamp_catalog_freshness(entry, info, now, ttl)
        if info.get("error"):
            entry["error"] = info["error"]
        if include_tools:
            entry["tools"] = enabled_names
            if disabled_names:
                entry["disabled_tools"] = disabled_names
            # Session-scoped dispatch truth (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog):
            # the subset of `tools` the CALLING session could actually
            # invoke right now, derived from the same predicate the
            # dispatch gate enforces (MCPMultiplexer.tool_dispatchable).
            entry["dispatchable_tools"] = [
                pn for pn in enabled_names if self.tool_dispatchable(pn)
            ]
        return entry

    async def list_catalog(self, server: str = "", include_tools: bool = True) -> dict:
        """Browse configured fleet metadata without unnecessary child starts.

        A server drill-down probes only that server.  A metadata-only fleet
        listing (``include_tools=False``) never probes a child and reports any
        already-cached reachability data.  Only an explicit whole-fleet tool
        listing probes the whole fleet.

        With ``server`` set, drills into one server and returns its full tool
        list with descriptions.
        """
        catalog = self.load_catalog()

        if server:
            if server not in catalog:
                return {"error": f"'{server}' is not in the catalog"}
            return await self._catalog_server_detail(server, include_tools)

        probe = await self._catalog_fleet_probe(include_tools)

        now = time.time()
        ttl = self._probe_ttl()
        servers: list[dict] = [
            await self._catalog_fleet_entry(
                name, probe.get(name) or {}, name in probe, now, ttl, include_tools
            )
            for name in catalog
        ]
        return {
            "catalog_authority": "medulla",
            "total_servers": len(servers),
            "total_tools": sum(s["tool_count"] for s in servers),
            "servers_running": sorted(self.children.keys()),
            "unavailable": [s["server"] for s in servers if s["available"] is False],
            "servers": servers,
        }

    def _split_requested_tool_owners(
        self, requested_tools: list[str], servers: list[str] | None
    ) -> tuple[set[str], list[str]]:
        """(servers that must be mounted, requested names with no owning server)."""
        target_servers: set[str] = set(servers or [])
        unresolved_tools: list[str] = []
        for prefixed in requested_tools:
            owner = self._server_for_prefixed(prefixed)
            if owner:
                target_servers.add(owner)
            else:
                unresolved_tools.append(prefixed)
        return target_servers, unresolved_tools

    async def _mount_target_servers(
        self, target_servers: set[str], failed: dict[str, str]
    ) -> list[str]:
        """Mount every target; record a human-readable reason for each that didn't."""
        mounted: list[str] = []
        for server in sorted(target_servers):
            await self.mount_child(server)
            if server in self.children:
                mounted.append(server)
                continue
            # Mount failed — surface *why* via a targeted probe (cached).
            info = await self.probe_server(server)
            failed[server] = info.get("error") or "could not mount (unreachable?)"
        return mounted

    def _condensed_server_surface(self, mounted: list[str]) -> set[str]:
        """The prefixed names a SERVER-level load exposes.

        CONCEPT:MERISTEM-ECO.multiplexer.condensed-server-load — only the condensed
        action surface; verbose 1:1 tools stay loadable by EXPLICIT name so
        ``load_tools(servers=[X])`` never floods a session's context with X's
        whole granular surface. Mirrors the always-on mount's verbose-hold.
        """
        wanted: set[str] = set()
        for server in mounted:
            for t in self.prefixed_tools_for_server(server):
                if _tool_is_verbose(t):
                    continue
                wanted.add(t.name)
        return wanted

    def _explain_unregistered_tools(
        self, requested_tools: list[str], failed: dict[str, str]
    ) -> None:
        """Record every requested tool whose owning server mounted but that never
        actually registered (disabled by config, or dropped by the child's
        runtime admission policy). It must not vanish silently — it belongs in
        ``failed``, not in a phantom ``newly_exposed``/``mounted`` claim."""
        for prefixed in requested_tools:
            if prefixed in self.tool_to_server or prefixed in failed:
                continue
            owner = self._server_for_prefixed(prefixed)
            if owner is not None and owner in failed:
                continue  # already explained by the server-level failure
            failed[prefixed] = (
                "tool is not registered by its owning server "
                "(disabled by config or rejected by its runtime policy)"
            )

    async def resolve_and_mount(
        self,
        tools: list[str] | None = None,
        servers: list[str] | None = None,
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Mount whatever children are needed to satisfy a ``load_tools``
        request and compute the set of prefixed names to expose.

        Returns ``(mounted_servers, prefixed_names_to_expose, failed)`` where
        ``failed`` maps each server OR each explicitly requested tool name that
        could not be resolved to a human-readable reason (e.g. an unreachable
        remote server, or a tool the owning server never actually registered —
        disabled by config, renamed, or dropped by its own admission policy).
        A requested tool that does not end up resolvable is NEVER silently
        left out of both ``to_expose`` and ``failed`` — the caller must be
        told exactly which of its requested names didn't make it, so the
        reported state can never claim a tool is loadable when the dispatcher
        cannot actually reach it. Does NOT touch FastMCP — registration of
        the live tools (and the list_changed notification) is the caller's
        job so this stays unit-testable.
        """
        requested_tools = list(tools or [])
        target_servers, unresolved_tools = self._split_requested_tool_owners(
            requested_tools, servers
        )
        failed: dict[str, str] = {
            name: "tool is not present in the fleet catalog"
            for name in unresolved_tools
        }
        mounted = await self._mount_target_servers(target_servers, failed)
        wanted = (
            set(requested_tools)
            if requested_tools
            else self._condensed_server_surface(mounted)
        )
        to_expose = [
            name
            for name in sorted(wanted)
            if name in self.tool_to_server and name not in self._exposed
        ]
        self._explain_unregistered_tools(requested_tools, failed)
        return mounted, to_expose, failed

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
        lane-skill-harvest.md``) — reaches :meth:`_server_for_prefixed`,
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
        ``_local_gated``/``_server_for_prefixed`` all resolve nothing for
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
        # ``_server_for_prefixed`` resolves ownership from the catalog's
        # collision-free prefix map, which works even BEFORE the owning child
        # is mounted — so this branch also catches a probed-but-never-mounted
        # fleet tool, not just an already-mounted one.
        if self._server_for_prefixed(prefixed_name) is not None:
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
            wanted.update(t.name for t in self.prefixed_tools_for_server(server))
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
            fn=_make_forwarder(mux, name),
        )
        mcp.add_tool(tool)
        attached = await mcp.get_tool(name)
        if attached is None or getattr(attached, "fn", None) is not tool.fn:
            raise RuntimeError("native fleet tool attachment was incomplete")

    return mount


def _make_forwarder(mux: MCPMultiplexer, prefixed_name: str):
    """Build a native tool whose only call path is governed fleet.call.

    The process owner binds the prepared registration after validating live
    authorities. An absent binding never falls back to a shared child credential.
    """

    async def _forward(**kwargs: _typing.Any) -> _fastmcp_tools.ToolResult:
        binding = getattr(mux, "_governed_fleet", None)
        if binding is None:
            raise _fastmcp_exceptions.ToolError("governed_fleet_unavailable")
        return await binding.call_native(prefixed_name, kwargs)

    return _forward


def _tool_is_verbose(tool: MCPTool) -> bool:
    """Whether a child tool is tagged ``verbose`` (FastMCP propagates tags in
    ``_meta``). Verbose 1:1 tools (e.g. arbora's granular per-action surface)
    are kept in the catalog but NOT auto-exposed by an always-on child — they
    load on demand via ``find_tools``/``load_tools`` to conserve context
    (CONCEPT:MERISTEM-ECO.multiplexer.condensed-server-load)."""
    meta = getattr(tool, "meta", None)
    if not isinstance(meta, dict):
        return False
    tags = (meta.get("fastmcp") or {}).get("tags") or []
    return "verbose" in tags


def _forwarder_component(
    mux: MCPMultiplexer, tool: MCPTool
) -> _fastmcp_tools.FunctionTool:
    """Build the executable FastMCP component for one aggregated child tool."""
    schema = tool.input_schema or {"type": "object", "properties": {}}
    return _fastmcp_tools.FunctionTool(
        name=tool.name,
        description=tool.description or "",
        parameters=schema,
        fn=_make_forwarder(mux, tool.name),
    )


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
    mcp.add_tool(_forwarder_component(mux, tool))
    mux._exposed.add(tool.name)
    return True


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


def provider_tools(mcp: _typing.Any) -> dict[str, _typing.Any]:
    """Return the FastMCP host's registered local tools."""
    components = getattr(getattr(mcp, "_local_provider", None), "_components", None)
    if not isinstance(components, dict):
        return {}
    return {
        value.name: value
        for key, value in components.items()
        if str(key).startswith("tool:") and getattr(value, "name", None)
    }


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
