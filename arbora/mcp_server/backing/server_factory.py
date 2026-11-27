from __future__ import annotations

from arbora.api.errors import not_served

"""MCP Server Factory.

Handles CLI argument parsing for MCP servers, automated server initialization
with middleware stacks, and the ``create_mcp_server`` convenience constructor.

CONCEPT:MERISTEM-ECO.mcp.standardized-interfaces — MCP Standardized Interfaces
"""


import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from scion_sdk.utilities import to_boolean

from arbora.core.environment import read_env, read_reference, write_env
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.backing.server_auth import (
    _configure_auth,
)
from arbora.mcp_server.backing.server_filters import (
    _ALL_TRANSPORTS,
    _NETWORK_TRANSPORTS,
    DEFAULT_HOST,
    _apply_request_overrides,
    _apply_semantic_filter,
    _component_passes,
    _env_filter_defaults,
    _finalize_filter_sets,
    _split_csv,
)
from arbora.mcp_server.backing.server_parser import (
    _is_loopback_bind,
    _validate_network_exposure,
    create_mcp_parser,
)
from arbora.mcp_server.served_engine import engine_unbound, no_process_session
from arbora.observability.state import Metrics
from arbora.security.delegation_config import McpAuthConfig
from arbora.security.key_cache import JwksCache
from arbora.security.run_token import RunTokenSigner
from arbora.version import __version__

logger = logging.getLogger(__name__)


def _rate_limit_client_id(context: Any) -> str:
    """Per-caller bucket key for :class:`RateLimitingMiddleware`.

    Without this, ``RateLimitingMiddleware(get_client_id=None)`` buckets
    EVERY request from EVERY caller under the single literal key
    ``"global"`` (``fastmcp.server.middleware.rate_limiting
    .RateLimitingMiddleware._get_client_identifier``) — one shared
    20-token / 10-req/s budget for the WHOLE server, across every session
    and every caller, covering every request type (``initialize``,
    ``notifications/initialized``, ``tools/list``, ``tools/call`` — not
    just tool calls). This fleet routinely runs several concurrent MCP
    clients against arbora at once (multiple agent lanes, the harness,
    service-account bridges); a handful of them handshaking within the
    same second exhausts the shared bucket, and each rejected request
    surfaces as a JSON-RPC ``-32000 "Rate limit exceeded for client:
    global"`` error. A caller that does not check the ``error`` field
    before reading ``result.tools`` mistakes that for a genuinely empty
    ``tools/list`` — this module's own multiplexer already documents a
    fleet child tripping the identical "Rate limit exceeded for client:
    global" message under a bulk probe (see the skill-harvest backoff
    comment in ``multiplexer.py``), so this is a known, live failure
    shape for this exact middleware, not a hypothetical one.

    Keying per authenticated caller preserves the same PER-CALLER budget
    (a single runaway/abusive client is still throttled) while stopping
    unrelated legitimate callers from starving each other on a shared
    bucket none of them knows exists. Falls back to one anonymous bucket
    only for requests with no resolvable identity (stdio, or an
    unauthenticated HTTP caller) — those already share fate under the
    same trust boundary.
    """
    import hashlib

    from fastmcp.server.dependencies import get_access_token

    try:
        token = get_access_token()
    except (RuntimeError, LookupError):
        token = None
    if token is None:
        return "anonymous"
    from arbora.api.domain.mcp_server.backing.context_helpers import (
        token_caller_material,
    )

    raw = token_caller_material(token)
    if not raw.strip("\x00"):
        return "anonymous"
    return "caller_" + hashlib.blake2s(raw.encode("utf-8"), digest_size=16).hexdigest()


def _import_optional_middlewares() -> tuple[Any, Any, Any, Any, Any]:
    """(UserTokenMiddleware, JWTClaimsLoggingMiddleware, EntityLinkingMiddleware,
    ToolMetricsMiddleware, ActorContextMiddleware), each None if the optional
    middlewares package is unavailable."""
    try:
        from arbora.mcp_server.backing.middlewares import (
            ActorContextMiddleware,
            EntityLinkingMiddleware,
            JWTClaimsLoggingMiddleware,
            ToolMetricsMiddleware,
            UserTokenMiddleware,
        )
    except ImportError:
        return None, None, None, None, None
    return (
        UserTokenMiddleware,
        JWTClaimsLoggingMiddleware,
        EntityLinkingMiddleware,
        ToolMetricsMiddleware,
        ActorContextMiddleware,
    )


def _append_optional_middlewares(
    middlewares: list[Any],
    server_name: str,
    optional_classes: tuple[Any, Any, Any, Any, Any],
    keys: JwksCache,
    delegation: McpAuthConfig,
    engine_provider: Callable[[], Any] = engine_unbound,
    process_session: Callable[[], Any] = no_process_session,
) -> None:
    (
        UserTokenMiddleware,
        JWTClaimsLoggingMiddleware,
        EntityLinkingMiddleware,
        ToolMetricsMiddleware,
        ActorContextMiddleware,
    ) = optional_classes

    # Scope every tool call to the caller's validated OIDC (Okta/Keycloak)
    # identity so servers can auto-load resources and inherit authz per-caller
    # (CONCEPT:MERISTEM-OS.identity.idp-agnostic-role-inheritance). No-op when the
    # request carries no validated token (loopback/stdio trust only) --
    # EXCEPT for arbora itself, which fails closed instead (BUG-036/GOC-15,
    # see ActorContextMiddleware/`_configure_middleware` docstrings).
    if ActorContextMiddleware is not None:
        middlewares.append(
            ActorContextMiddleware(
                keys=keys,
                require_verified_session=(server_name == "arbora"),
                engine_provider=engine_provider,
                process_session=process_session,
            )
        )

    # Per-tool Prometheus metrics (count/latency/error) for this server, scraped
    # from its own /metrics route (CONCEPT:MERISTEM-OS.observability.no-op-without-metrics). No-op without the metrics extra.
    if ToolMetricsMiddleware is not None:
        middlewares.append(ToolMetricsMiddleware())

    if JWTClaimsLoggingMiddleware is not None:
        pass  # Also do not add this as it logs to stdout

    if EntityLinkingMiddleware is not None:
        middlewares.append(EntityLinkingMiddleware())

    if delegation["enable_delegation"] and UserTokenMiddleware is not None:
        # Keep the privacy-safe error middleware outermost so a delegation
        # rejection cannot bypass the standardized exception surface.
        middlewares.insert(1, UserTokenMiddleware(config=delegation))


def _configure_eunomia_middleware(
    args: argparse.Namespace, config: RuntimeConfig
) -> Any | None:
    """None when Eunomia is not configured for this server; the built
    middleware when it is; exits the process on a configuration/build
    failure (never returns a sentinel for that case)."""
    if args.eunomia_type not in ["embedded", "remote"]:
        return None
    try:
        from arbora.mcp_server.backing.eunomia_principal import (
            create_eunomia_middleware,
        )

        require_verified = str(getattr(args, "auth_type", "none")) != "none"
        if args.eunomia_type == "remote":
            return create_eunomia_middleware(
                config=config,
                policy_file=None,
                use_remote_eunomia=True,
                eunomia_endpoint=args.eunomia_remote_url,
                api_key_ref=getattr(args, "eunomia_api_key_ref", None),
                require_verified_principal=require_verified,
            )
        policy_file = args.eunomia_policy_file or "mcp_policies.json"
        return create_eunomia_middleware(
            config=config,
            policy_file=policy_file,
            use_remote_eunomia=False,
            require_verified_principal=require_verified,
        )
    except Exception as exc:
        logger.error(
            "Failed to load Eunomia middleware (exception_type=%s)",
            type(exc).__name__,
            exc_info=True,
        )
        sys.exit(1)


def _configure_middleware(
    args: argparse.Namespace,
    keys: JwksCache,
    delegation: McpAuthConfig,
    *,
    config: RuntimeConfig,
    server_name: str = "",
    engine_provider: Callable[[], Any] = engine_unbound,
    process_session: Callable[[], Any] = no_process_session,
) -> list[Any]:
    """Build the standard middleware stack for an MCP server.

    ``server_name`` (the same ``name`` passed to :func:`create_mcp_server`)
    selects :class:`~arbora.mcp_server.backing.middlewares.ActorContextMiddleware`'s
    fail-closed mode (BUG-036/GOC-15): only the ``"arbora"`` server —
    the one MCP server in the fleet whose tools reach privileged Knowledge-
    Graph reads/writes — gets ``require_verified_session=True``. Every other
    fleet server (~60 independently-owned ``agents/*-mcp`` packages using this
    same factory) keeps the prior no-op-without-a-token behavior unchanged;
    making this fail closed fleet-wide is a separable, larger change that
    needs its own per-package audit, not a side effect of closing BUG-036.
    """
    from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
    from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware

    middlewares: list[Any] = [
        ErrorHandlingMiddleware(include_traceback=False, transform_errors=True),
        RateLimitingMiddleware(
            max_requests_per_second=10.0,
            burst_capacity=20,
            get_client_id=_rate_limit_client_id,
        ),
    ]
    _append_optional_middlewares(
        middlewares,
        server_name,
        _import_optional_middlewares(),
        keys,
        delegation,
        engine_provider,
        process_session,
    )

    eunomia_mw = _configure_eunomia_middleware(args, config)
    if eunomia_mw is not None:
        middlewares.append(eunomia_mw)

    return middlewares


# ── Stdio JSON-RPC purity ────────────────────────────────────────────────────
#
# There used to be a ``protect_stdio_jsonrpc()`` here: a permanent, process-wide
# monkeypatch of ``builtins.print``/``warnings.showwarning`` applied once and
# never undone (B-19). It intercepted every caller in the process to protect one
# file descriptor — global where the concern was local, unscoped, un-nestable,
# no teardown — and it is the confirmed root cause of a cross-file test-pollution
# incident: a test that drove the real stdio entrypoint left ``print`` silently
# redirected for the rest of the session, breaking ~23 unrelated tests in 11
# files with no plausible causal link.
#
# It also turned out to be solving an already-solved problem. On the stdio
# transport, ``mcp.run(transport="stdio")`` (FastMCP's ``run_stdio_async``, see
# ``fastmcp/server/mixins/transport.py``) enters ``mcp.server.stdio.stdio_server()``
# (vendored MCP SDK, ``mcp/server/stdio.py``), which ALREADY gives the protocol
# writer exclusive, fd-level ownership of real stdout for the scope of serving:
# it ``os.dup()``s fd 1 to a private descriptor for the JSON-RPC writer, then
# ``os.dup2()``s a duplicate of stderr onto fd 1 so every OTHER writer in the
# process — ``print()``, a bare ``sys.stdout.write()``, a C extension writing
# directly to the fd, a subprocess that inherits it — lands on stderr instead,
# and restores both descriptors in a ``finally`` on exit. Because the diversion
# is at the OS file-descriptor level, not a Python-object patch, it needs no
# teardown bookkeeping here: nothing in this codebase ever touches
# ``builtins.print`` or ``sys.stdout``, so there is nothing to save or restore,
# and a second concurrent ``stdio_server()`` in one process raises loudly
# instead of silently double-diverting (the SDK's own ``_claims`` registry).
# ``warnings.showwarning`` needed no interception either — its stdlib default
# already targets ``sys.stderr`` when no explicit ``file=`` is given.
#
# What this repo still owns: never introduce a ``print()`` in the served MCP
# surface in the first place (there is no runtime net for code that runs BEFORE
# ``mcp.run()`` claims the descriptor, e.g. engine bootstrap or a co-service
# thread started moments earlier). That is enforced statically, in the fast
# pre-commit tier, by ``scripts/check_no_stdout_writes.py`` (CONCEPT:MERISTEM-ECO.mcp.stdio-static-purity-gate) —
# catching the offender at authoring time instead of corrupting a frame at 3am.
# Diagnostics in served code route through the standard ``logging`` module
# (stderr by default), never ``print``.


def _resolve_network_allowed_hosts(
    args: argparse.Namespace, loopback: bool, normalize_host_authorities: Any
) -> list[str]:
    hosts = _split_csv(str(getattr(args, "allowed_hosts", "") or ""))
    if not hosts:
        if not loopback:
            raise RuntimeError("MCP_ALLOWED_HOSTS is required")
        port = int(getattr(args, "port", 8000))
        hosts = [
            f"localhost:{port}",
            f"127.0.0.1:{port}",
            f"[::1]:{port}",
            "testserver",
        ]
    try:
        return sorted(normalize_host_authorities(hosts))
    except ValueError:
        raise RuntimeError("MCP_ALLOWED_HOSTS must contain exact authorities") from None


def _build_network_base_middleware(
    hosts: list[str],
    origin_values: list[str],
    max_bytes: int,
    Middleware: Any,
    ExactHostAuthorityMiddleware: Any,
    OriginPolicyMiddleware: Any,
    BoundedRequestBodyMiddleware: Any,
) -> list[Any]:
    return [
        Middleware(ExactHostAuthorityMiddleware, allowed_hosts=hosts),
        Middleware(OriginPolicyMiddleware, allowed_origins=origin_values),
        Middleware(BoundedRequestBodyMiddleware, max_bytes=max_bytes),
    ]


def _maybe_prepend_trusted_proxy_middleware(
    args: argparse.Namespace,
    middleware: list[Any],
    parse_cidrs: Any,
    Middleware: Any,
    TrustedProxyPeerMiddleware: Any,
) -> None:
    if not bool(getattr(args, "tls_terminated", False)):
        return
    cidrs = _split_csv(str(getattr(args, "trusted_proxy_cidrs", "") or ""))
    parse_cidrs(cidrs)
    middleware.insert(0, Middleware(TrustedProxyPeerMiddleware, trusted_cidrs=cidrs))


def _resolve_network_connection_bounds() -> tuple[int, int]:
    max_connections = int(read_env("MCP_MAX_CONNECTIONS"))
    backlog = int(read_env("MCP_LISTEN_BACKLOG"))
    if not 1 <= max_connections <= 10_000 or not 1 <= backlog <= 65_535:
        raise RuntimeError("MCP listener resource bounds are invalid")
    return max_connections, backlog


def _build_uvicorn_config(
    args: argparse.Namespace, max_connections: int, backlog: int
) -> dict[str, Any]:
    uvicorn_config: dict[str, Any] = {
        # Never trust Forwarded/X-Forwarded-* from the network. The direct peer
        # address remains authoritative for the trusted-ingress CIDR gate.
        "proxy_headers": False,
        "timeout_keep_alive": 5,
        "timeout_graceful_shutdown": 15,
        "limit_concurrency": max_connections,
        "backlog": backlog,
        "h11_max_incomplete_event_size": 65_536,
    }
    certfile = str(getattr(args, "tls_certfile", "") or "").strip()
    keyfile = str(getattr(args, "tls_keyfile", "") or "").strip()
    if certfile and keyfile:
        uvicorn_config.update(ssl_certfile=certfile, ssl_keyfile=keyfile)
    return uvicorn_config


def mcp_network_run_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """Return the one hardened FastMCP/Uvicorn network-serving configuration."""
    from starlette.middleware import Middleware

    from arbora.security.http_boundary import (
        BoundedRequestBodyMiddleware,
        ExactHostAuthorityMiddleware,
        OriginPolicyMiddleware,
        TrustedProxyPeerMiddleware,
        normalize_host_authorities,
        normalize_origins,
        parse_cidrs,
    )

    transport = str(getattr(args, "transport", "stdio") or "stdio").lower()
    if transport not in _NETWORK_TRANSPORTS:
        return {}
    host = str(getattr(args, "host", "") or "")
    loopback = _is_loopback_bind(host)
    hosts = _resolve_network_allowed_hosts(args, loopback, normalize_host_authorities)

    origin_values = _split_csv(str(getattr(args, "allowed_origins", "") or ""))
    normalize_origins(origin_values)  # validate before the listener starts

    max_bytes = int(getattr(args, "max_request_bytes", 4 * 1024 * 1024))
    middleware = _build_network_base_middleware(
        hosts,
        origin_values,
        max_bytes,
        Middleware,
        ExactHostAuthorityMiddleware,
        OriginPolicyMiddleware,
        BoundedRequestBodyMiddleware,
    )
    _maybe_prepend_trusted_proxy_middleware(
        args, middleware, parse_cidrs, Middleware, TrustedProxyPeerMiddleware
    )

    max_connections, backlog = _resolve_network_connection_bounds()
    uvicorn_config = _build_uvicorn_config(args, max_connections, backlog)
    return {"middleware": middleware, "uvicorn_config": uvicorn_config}


# ── Fleet server registry self-registration (CONCEPT:MEDULLA-KG.sharding.server-registry, W2.5) ──
#
# Every server built via ``create_mcp_server`` self-registers a REAL, queryable
# ``:Server`` graph node with the engine at startup and renews its lease on a
# heartbeat cadence for the server's whole lifetime — wired here (the ONE
# factory ~62 fleet MCP servers already call), not per-server, so it is native
# by default with zero per-server opt-in. Killing the process simply stops the
# heartbeats; the engine's own stale-lease reaper expires the row once the
# lease lapses (`medulla` `src/server/registry_reaper.rs`).
#
# The au config-file sync (`knowledge_graph.core.engine_ingestion.
# ingest_mcp_server`) remains a RECONCILER over the SAME `:Server` shape — it
# repairs a hand-broken or never-self-registered row — but this is now the
# PRIMARY, live-identity writer.
_FLEET_REGISTRATION_DEFAULT_TTL_SECS = 300
_FLEET_REGISTRATION_MIN_TTL_SECS = 60


def _fleet_registration_ttl_secs() -> int:
    """Positive lease TTL, seconds (``MCP_FLEET_REGISTRATION_TTL_SECS`` overrides).

    Heartbeats renew at roughly a third of the TTL (see
    :func:`_register_and_heartbeat_forever`), so a transient miss or two never
    expires a healthy server.
    """
    try:
        value = int(
            read_env(
                "MCP_FLEET_REGISTRATION_TTL_SECS", _FLEET_REGISTRATION_DEFAULT_TTL_SECS
            )
        )
    except (TypeError, ValueError):
        return _FLEET_REGISTRATION_DEFAULT_TTL_SECS
    return (
        value
        if value >= _FLEET_REGISTRATION_MIN_TTL_SECS
        else (_FLEET_REGISTRATION_DEFAULT_TTL_SECS)
    )


def _fleet_registration_endpoint_reference(args: argparse.Namespace, name: str) -> str:
    """A bounded, privacy-safe reference to how this server is reached.

    Never a raw credentialed URL — an opaque marker the engine and the au
    reconciler both treat as a reference, not a dereferenceable address (the
    same convention au's ``persistence_reference``/``mcp-ref://`` values use).
    """
    transport = str(getattr(args, "transport", "stdio") or "stdio")
    if transport in _NETWORK_TRANSPORTS:
        host = str(getattr(args, "host", "") or DEFAULT_HOST)
        port = getattr(args, "port", 0) or 0
        return f"{transport}://{host}:{port}"
    return f"stdio://{name}"


async def _register_and_heartbeat_forever(name: str, url: str, ttl_secs: int) -> None:
    """Self-register, then renew ``name``'s lease forever on a cadence well
    inside ``ttl_secs`` (CONCEPT:MEDULLA-KG.sharding.server-registry, W2.5).

    Best-effort: an unreachable engine (a ``tiny`` profile with no graph
    access yet, a dev/test process, a cold start racing the engine's own
    boot) never crashes or blocks the server — a failed attempt is logged at
    debug level and retried on the SAME cadence, self-healing the moment the
    engine becomes reachable.
    """
    interval = max(1, ttl_secs // 3)
    {
        "transport": str(read_env("TRANSPORT") or "stdio"),
        "pid": os.getpid(),
    }
    while True:
        exc = not_served("knowledge_graph.core.graph_compute", "MEDULLA-PLACEMENT-R611")
        logger.debug(
            "Fleet self-registration attempt failed, will retry: %s",
            type(exc).__name__,
        )
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _lifespan_extension_scope(extension: Callable[[Any], Any] | None, app: Any):
    """Run an optional public serving extension around the transport body."""

    if extension is None:
        yield
        return
    async with extension(app):
        yield


def _start_fleet_registration_heartbeat(
    args: argparse.Namespace, name: str
) -> asyncio.Task[None] | None:
    """Start this process's registry heartbeat when fleet registration is enabled."""

    if not to_boolean(read_env("MCP_FLEET_REGISTRATION")):
        return None
    url = _fleet_registration_endpoint_reference(args, name)
    ttl_secs = _fleet_registration_ttl_secs()
    return asyncio.create_task(_register_and_heartbeat_forever(name, url, ttl_secs))


async def _stop_fleet_registration_heartbeat(
    task: asyncio.Task[None] | None,
) -> None:
    """Cancel and await a registry heartbeat during transport teardown."""

    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _shutdown_fleet_engine(manage_engine_shutdown: bool) -> None:
    """Drain and close the process-owned engine when the factory owns shutdown."""

    if not manage_engine_shutdown:
        return
    exc = not_served("knowledge_graph.core.graph_compute", "MEDULLA-PLACEMENT-R611")
    logger.error(
        "Arbora transport drain failed; continuity is not claimed (%s)",
        type(exc).__name__,
    )


def _fleet_registration_lifespan_factory(
    args: argparse.Namespace,
    name: str,
    *,
    lifespan_extension: Callable[[Any], Any] | None = None,
    manage_engine_shutdown: bool = True,
):
    """Build the ``FastMCP(..., lifespan=...)`` ASGI lifespan that starts/stops
    the self-registration heartbeat task (CONCEPT:MEDULLA-KG.sharding.server-registry, W2.5).

    A closure (not a bare module-level lifespan) so it captures THIS server's
    own ``args``/``name`` without stashing state on the app object. Opt out
    with ``MCP_FLEET_REGISTRATION=false`` for a run that genuinely has no
    engine access (e.g. an isolated unit-test harness).
    """

    @contextlib.asynccontextmanager
    async def _fleet_registration_lifespan(_app: Any):
        task = _start_fleet_registration_heartbeat(args, name)
        try:
            # The extension is nested inside the factory-owned heartbeat
            # context.  Its teardown therefore runs before the base transport
            # drain, which is the only safe order for a public composition
            # that owns co-services and fleet children.
            async with _lifespan_extension_scope(lifespan_extension, _app):
                yield
        finally:
            await _stop_fleet_registration_heartbeat(task)
            # Arbora owns one process transport shared by all graph-scoped
            # views.  A pod/process drain must stop new admissions, wait only
            # the configured bounded interval for in-flight calls, and then
            # close the transport.  A timeout is visible and is never
            # presented as session continuity; a restarted pod must mint a
            # fresh GraphSession and re-read ClusterMembers.
            await _shutdown_fleet_engine(manage_engine_shutdown)

    return _fleet_registration_lifespan


def _handle_help_and_port_validation(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    if hasattr(args, "help") and args.help:
        parser.print_help()
        sys.exit(0)
    if args.port < 0 or args.port > 65535:
        logger.error(f"Error: Port {args.port} is out of valid range (0-65535).")
        sys.exit(1)


def _is_remote_network(args: argparse.Namespace) -> bool:
    return str(
        getattr(args, "transport", "stdio") or "stdio"
    ).lower() in _NETWORK_TRANSPORTS and not _is_loopback_bind(
        getattr(args, "host", "")
    )


def _resolve_metrics_token() -> str | None:
    metrics_ref = str(read_env("MCP_METRICS_TOKEN_REF") or "").strip()
    if not metrics_ref:
        return None
    try:
        if metrics_ref.startswith("env://"):
            metrics_token = str(read_reference(metrics_ref[len("env://") :], "") or "")
        else:
            from arbora.security.secrets import create_secrets_client

            metrics_token = str(create_secrets_client().resolve_ref(metrics_ref) or "")
        if not 32 <= len(metrics_token) <= 4_096 or any(
            character in metrics_token for character in "\r\n\x00"
        ):
            return None
        return metrics_token
    except Exception:
        logger.warning("metrics token reference unresolved", exc_info=True)
        return None


def _mount_tasks_extension_if_available(
    mcp: Any,
    name: str,
    config: RuntimeConfig,
    engine_provider: Callable[[], Any] = engine_unbound,
    signer: RunTokenSigner | None = None,
) -> None:
    # CONCEPT:MERISTEM-ECO.mcp.tasks-workitem-bridge -- mount the native WorkItem-backed
    # Tasks extension (arbora/mcp_server/backing/tasks_extension/py), NOT
    # fastmcp_tasks.extension.TasksExtension: that package's engine is
    # hard-wired to Docket/Redis, a second job system this codebase's one
    # WorkItem state machine (MERISTEM-P1-1) forbids duplicating. This makes the
    # same WorkItem-backed contract available to any MCP protocol version
    # this server negotiates over its one `/mcp` endpoint, including
    # 2026-07-28 (BUG-069: the isolated mcp_v2_gateway sidecar that used to
    # carry an equivalent projection over its own hop was retired once the
    # `mcp<2` isolation premise it depended on expired -- see
    # docs/architecture/mcp-2026-protocol-surface.md).
    #
    # `fastmcp.server.extensions` (what the Tasks extension mounts through)
    # is fastmcp-4-only; the fleet is EXPLICITLY mixed-version (D-SH-3: child
    # images still ship fastmcp 3.4.4, hostPath-mounted over this same
    # working tree -- D-W2C2-2). `tasks_extension` itself already guards the
    # import, degrading `TASKS_EXTENSION_AVAILABLE` to False and logging the
    # cause; this only needs to skip mounting so a fastmcp-3 image still
    # builds a working server minus tasks/get, tasks/update, tasks/cancel.
    from arbora.mcp_server.backing.tasks_extension import (
        TASKS_EXTENSION_AVAILABLE,
        WorkItemTasksExtension,
    )

    if TASKS_EXTENSION_AVAILABLE:
        # The owning server identity is part of the native Tasks response
        # metadata.  Arbora's multiplexer uses it to route follow-up
        # tasks/get/update/cancel requests without introducing another task
        # registry or scheduler.
        mcp.add_extension(
            WorkItemTasksExtension(
                server_id=name,
                engine_provider=engine_provider,
                signer=signer,
                config=config,
            )
        )


def _register_operational_routes(
    mcp: Any, remote_network: bool, metrics_token: str | None, metrics: Metrics
) -> None:
    """Health is a generic readiness result. Metrics are local-only unless a
    remote listener has a runtime-resolved bearer token; otherwise that
    route is absent. Wrapped defensively so older FastMCP builds without
    custom_route still produce a working server."""
    try:
        # render_metrics_with_engine() merges the Python gateway's own
        # Prometheus exposition with the embedded medulla engine's
        # (fetched from its loopback-only listener — the engine refuses any
        # non-loopback auxiliary listener outright, so this process is the
        # only thing that CAN re-serve it). This is the live /metrics path
        # for the collapsed single-container arbora pod: this FastMCP
        # process never mounts the REST gateway's own /metrics
        # (arbora.gateway.graph_api.register_graph_routes), so this
        # custom_route is the only exposition actually served.
        from starlette.requests import Request as _Request
        from starlette.responses import JSONResponse as _JSONResponse
        from starlette.responses import Response as _Response

        from arbora.observability.gateway_metrics import (
            render_metrics_with_engine as _render_metrics_with_engine,
        )

        if not remote_network or metrics_token is not None:

            @mcp.custom_route("/metrics", methods=["GET"])
            async def _metrics_route(request: _Request) -> _Response:
                if metrics_token is not None:
                    import secrets as _secrets

                    values = [
                        value.decode("latin-1")
                        for key, value in request.scope.get("headers", ())
                        if key.lower() == b"authorization"
                    ]
                    expected = f"Bearer {metrics_token}"
                    if len(values) != 1 or not _secrets.compare_digest(
                        values[0], expected
                    ):
                        return _Response(
                            status_code=401,
                            headers={
                                "WWW-Authenticate": "Bearer",
                                "Cache-Control": "no-store",
                            },
                        )
                body, content_type = await _render_metrics_with_engine(metrics)
                return _Response(
                    content=body,
                    media_type=content_type,
                    headers={"Cache-Control": "no-store"},
                )

        @mcp.custom_route("/health", methods=["GET"])
        async def _health_route(request: _Request) -> _JSONResponse:  # noqa: ARG001
            return _JSONResponse(
                {"status": "ok"}, headers={"Cache-Control": "no-store"}
            )

    except Exception as _route_exc:
        logger.warning(
            "Could not register metrics and health routes (exception_type=%s)",
            type(_route_exc).__name__,
            exc_info=True,
        )


def create_mcp_server(
    name: str = "MCP Server",
    version: str = __version__,
    instructions: str = "",
    command_args: list[str] | None = None,
    transport_choices: tuple[str, ...] = _ALL_TRANSPORTS,
    *,
    keys: JwksCache,
    delegation: McpAuthConfig,
    metrics: Metrics,
    config: RuntimeConfig,
    lifespan_extension: Callable[[Any], Any] | None = None,
    manage_engine_shutdown: bool = True,
    engine_provider: Callable[[], Any] = engine_unbound,
    process_session: Callable[[], Any] = no_process_session,
    token_signer: RunTokenSigner | None = None,
):
    """Initialize a FastMCP server with a standard middleware and auth stack.

    This helper consolidates the steps of creating a parser, configuring
    authentication providers (JWT, OIDC, etc.), and assembling standard
    middleware (Logging, Timing, Rate Limiting). It handles CLI flag
    parsing and will exit the process if help is requested or configuration
    is invalid.

    Args:
        name: The human-readable name of the MCP server.
        version: Semantic version string for the server.
        instructions: System instructions specific to this MCP server's
            tools, providing context for the LLM.
        command_args: Optional list of CLI arguments (default: sys.argv).
        transport_choices: Current transports exposed by this server.
        metrics: The host's scrape samplers, read by the ``/metrics`` route.
        config: The host's runtime settings, read by the extensions it mounts.
        lifespan_extension: Optional application lifespan nested inside the
            factory-owned registration context.
        manage_engine_shutdown: Whether this factory context owns the active
            engine drain/close.  Public composition surfaces set this false
            and perform the gated finalization themselves.

    Returns:
        A tuple containing:
            - args: The parsed argparse.Namespace object.
            - mcp: The initialized FastMCP server instance.
            - middlewares: A list of configured middleware instances.

    """
    import logging

    from fastmcp import FastMCP

    # Force all logging to stderr to prevent JSON-RPC corruption over stdio
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)

    parser = create_mcp_parser(transport_choices=transport_choices)
    args, _ = parser.parse_known_args(command_args)
    if args.transport not in transport_choices:
        parser.error(
            f"transport {args.transport!r} is not supported by this server; "
            f"choose from {', '.join(transport_choices)}"
        )

    # NOTE: stdout purity on the stdio transport needs no code here (or anywhere in
    # this module) — see the module docstring above ``mcp_network_run_kwargs`` for
    # why. Building a server does not dedicate the process to stdio in the first
    # place; the transport that does is claimed, fd-level, by the MCP SDK itself.

    _handle_help_and_port_validation(args, parser)

    _validate_network_exposure(args)

    auth = _configure_auth(args, delegation, config)
    middlewares = _configure_middleware(
        args,
        keys,
        delegation,
        config=config,
        server_name=name,
        engine_provider=engine_provider,
        process_session=process_session,
    )

    remote_network = _is_remote_network(args)
    metrics_token = _resolve_metrics_token()

    write_env("FASTMCP_LOG_LEVEL", "CRITICAL")
    mcp = FastMCP(
        name,
        version=version,
        auth=auth,
        instructions=instructions,
        lifespan=_fleet_registration_lifespan_factory(
            args,
            name,
            lifespan_extension=lifespan_extension,
            manage_engine_shutdown=manage_engine_shutdown,
        ),
        # `tasks=` only sets the DEFAULT task-mode for individual `@mcp.tool()`
        # registrations (fastmcp.utilities.tasks.TaskConfig) -- it does not by
        # itself mount the `io.modelcontextprotocol/tasks` extension's
        # `tasks/get`/`tasks/update`/`tasks/cancel` methods (that requires a
        # `ServerExtension`, added below). Kept False: no tool here is
        # registered with `task=True`, so there is nothing for a per-tool
        # default to apply to yet.
        tasks=False,
    )
    _mount_tasks_extension_if_available(
        mcp, name, config, engine_provider, token_signer
    )
    _register_operational_routes(mcp, remote_network, metrics_token, metrics)

    # Inject dynamic visibility transform for dynamic tag/tool filtering
    try:
        from fastmcp.server.transforms import Transform

        class DynamicVisibilityTransform(Transform):
            """Enforces environment-variable and header-based tag and tool filters dynamically across all components."""

            def _filter_components(self, components):
                (
                    enabled_tools_list,
                    disabled_tools_list,
                    enabled_tags_list,
                    disabled_tags_list,
                ) = _env_filter_defaults()

                # 1.5. Override/Append with parsed CLI args if specified
                cli_tools = getattr(args, "tools", None)
                if cli_tools:
                    enabled_tools_list = _split_csv(cli_tools)
                cli_disabled_tools = getattr(args, "disabled_tools", None)
                if cli_disabled_tools:
                    disabled_tools_list = _split_csv(cli_disabled_tools)

                # 2. Extract request query parameters and headers if HTTP/SSE transport is active
                query_filter = None
                (
                    enabled_tools_list,
                    disabled_tools_list,
                    enabled_tags_list,
                    disabled_tags_list,
                    query_filter,
                    reject_all,
                ) = _apply_request_overrides(
                    enabled_tools_list,
                    disabled_tools_list,
                    enabled_tags_list,
                    disabled_tags_list,
                    query_filter,
                )

                if query_filter and not reject_all:
                    enabled_tools_list, reject_all = _apply_semantic_filter(
                        query_filter, enabled_tools_list, name
                    )

                if reject_all:
                    return []

                # 3. Convert lists to sets, then filter each component.
                enabled_tags, disabled_tags, enabled_names, disabled_names = (
                    _finalize_filter_sets(
                        enabled_tags_list,
                        disabled_tags_list,
                        enabled_tools_list,
                        disabled_tools_list,
                    )
                )
                return [
                    c
                    for c in components
                    if _component_passes(
                        c, enabled_names, disabled_names, enabled_tags, disabled_tags
                    )
                ]

            async def list_tools(self, tools):
                return self._filter_components(tools)

            async def get_tool(self, name, call_next, *, version=None):
                tool = await call_next(name, version=version)
                if tool is None:
                    return None
                filtered = self._filter_components([tool])
                return filtered[0] if filtered else None

            async def list_resources(self, resources):
                return self._filter_components(resources)

            async def get_resource(self, uri, call_next, *, version=None):
                res = await call_next(uri, version=version)
                if res is None:
                    return None
                filtered = self._filter_components([res])
                return filtered[0] if filtered else None

            async def list_resource_templates(self, templates):
                return self._filter_components(templates)

            async def get_resource_template(self, uri, call_next, *, version=None):
                tmpl = await call_next(uri, version=version)
                if tmpl is None:
                    return None
                filtered = self._filter_components([tmpl])
                return filtered[0] if filtered else None

            async def list_prompts(self, prompts):
                return self._filter_components(prompts)

            async def get_prompt(self, name, call_next, *, version=None):
                prompt = await call_next(name, version=version)
                if prompt is None:
                    return None
                filtered = self._filter_components([prompt])
                return filtered[0] if filtered else None

        mcp.add_transform(DynamicVisibilityTransform())
    except Exception as exc:
        logger.warning(
            "Could not register dynamic visibility transform (exception_type=%s)",
            type(exc).__name__,
            exc_info=True,
        )

    return args, mcp, middlewares
