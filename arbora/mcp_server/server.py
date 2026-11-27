"""Serving lifecycle for the native arbora MCP application."""

from __future__ import annotations

import asyncio
import logging
import sys
from functools import partial
from typing import Any

from scion_sdk.mcp.network import build_network_serving_config

from arbora.access.action_policy import ActionPolicies
from arbora.browser_control.browser_state import BrowserState
from arbora.content import register_installed_content
from arbora.core.environment import read_env, write_env
from arbora.core.settings import RuntimeConfig
from arbora.fleet.state import FleetState
from arbora.identity.ports import BrowserBoundaryFactory, IdentityLifecycle
from arbora.mcp_server import runtime
from arbora.mcp_server.composition import (
    install_gateway_application,
    start_composed_services,
)
from arbora.mcp_server.host_caches import WarmupSlot
from arbora.mcp_server.host_context import (
    HostContext,
    bind_a2a_decision,
    bind_invocation_probes,
    bind_ranking,
)
from arbora.mcp_server.hosted_invocation import HostedInvocation
from arbora.observability.state import Telemetry
from arbora.security.key_cache import JwksCache
from arbora.security.process_authority import ProcessAuthority
from arbora.semantic_content import default_content_providers

logger = logging.getLogger(__name__)


def _changeset_seams(invocation: Any) -> Any:
    """The run ports' changeset seams, only when the host has the engine to stage through."""
    send = getattr(invocation, "engine_send", None)
    if invocation is None or send is None:
        return None
    from arbora.agent_host.changeset_seams import production_changeset_seams
    from arbora.agent_host.host_port_composition import run_of, run_principal_call

    return production_changeset_seams(
        run_principal_call(invocation.services), send, run_of
    )


def _register_semantic_content(mcp: Any) -> None:
    """Expose independently owned semantic artifacts through the SDK contract.

    ConnectorPack capture consumes these MCP resources and EG ``AttachPack``
    remains the sole authority that interprets/activates their SHACL content.
    Arbora deliberately performs no local shape parsing or attachment.
    """

    register_installed_content(mcp, default_content_providers())


def _configure_arbora_otel(telemetry: Telemetry) -> None:
    """Activate the canonical metadata-only OTLP pipeline when configured."""

    if not read_env("ENABLE_OTEL"):
        return
    try:
        from arbora.observability.custom_observability import setup_otel

        setup_otel(telemetry, service_name="arbora")
    except Exception as exc:  # observability cannot prevent serving
        logger.warning(
            "Arbora OTLP setup failed; trace export is disabled: %s",
            exc,
            exc_info=True,
        )


def _configure_telemetry_engine_otel(telemetry: Telemetry) -> None:
    """Eagerly start the standard-env-var OTLP trace pipeline (X2).

    CONCEPT:MERISTEM-OS.observability.telemetry-observability — independent of
    ``_configure_arbora_otel``'s ``ENABLE_OTEL``-gated Logfire/Langfuse
    pipeline: :func:`~arbora.observability.telemetry.configure_standard_otel`
    self-gates purely on the standard ``OTEL_EXPORTER_OTLP_ENDPOINT``/``OTEL_SERVICE_NAME``/
    ``OTEL_TRACES_EXPORTER`` vars (falling back to ``MEDULLA_OBS_ADDR``),
    so no new env var is introduced here. This is the ONE process-bootstrap
    call site that activates it for the arbora MCP server — never per-request.
    """
    try:
        from arbora.observability.telemetry import configure_standard_otel

        configured = configure_standard_otel(telemetry, service_name="arbora")
        logger.info(
            "Arbora OTLP trace export (standard env vars): %s",
            "enabled" if configured else "disabled",
        )
    except Exception as exc:  # observability cannot prevent serving
        logger.warning(
            "Arbora TelemetryEngine OTel setup failed: %s",
            exc,
            exc_info=True,
        )


def _preflight_mcp_sdk_floor() -> None:
    """Fail startup loudly when the installed MCP SDK is below the declared floor.

    CONCEPT:MERISTEM-ECO.mcp.protocol-compat-bridge — closes D-OB-18.

    The category defect this exists for is that source-vs-installed divergence was
    INVISIBLE: arbora source targeting fastmcp 4 / mcp 2 ran for weeks on an image
    that shipped fastmcp 3.4.5 / mcp 1.29.0, and the only symptom was a single ERROR
    log line as `attach_fleet_loader` lost every fleet meta-tool to
    ``ImportError: cannot import name 'MCPError'``. A rebuilt image with no assertion
    just resets that clock, so the assertion runs here, at startup, as well as at
    image-build time.

    Enforcement is a hook, not a hardcoded policy: ``MCP_SDK_FLOOR_ENFORCE`` selects
    ``error`` (default — refuse to start, because a arbora that silently loses its
    fleet surface is worse than one that will not come up) or ``warn`` (log and
    continue, for an operator who is knowingly running a mismatched pair during a
    migration).
    """
    from arbora.fleet.protocol_compat import check_mcp_sdk_floor

    result = check_mcp_sdk_floor()
    if result["ok"] is True:
        logger.info("MCP SDK floor OK: %s", result["detail"])
        return
    if result["ok"] is None:
        logger.warning("MCP SDK floor check skipped: %s", result["detail"])
        return

    mode = str(read_env("MCP_SDK_FLOOR_ENFORCE") or "error").strip().lower()
    message = (
        f"installed MCP SDK does not satisfy the declared [mcp] floor: {result['detail']}. "
        "The runtime image and this source tree have diverged — rebuild the image "
        "(docker/arbora-unified.Dockerfile) so its dependency closure matches the "
        "source it serves. Set MCP_SDK_FLOOR_ENFORCE=warn to start anyway."
    )
    if mode == "warn":
        logger.error("arbora starting with a mismatched MCP SDK: %s", message)
        return
    raise RuntimeError(message)


def _attach_fleet_runtime(
    mcp: Any,
    fleet_catalog_reader: Any,
    fleet: FleetState,
    authority: ProcessAuthority,
) -> Any:
    """Attach the mandatory fleet surface or fail before serving."""

    try:
        from arbora.fleet.multiplexer import attach_fleet_loader

        return attach_fleet_loader(
            mcp,
            catalog_reader=fleet_catalog_reader,
            fleet=fleet,
            authority_scope=partial(runtime.verified_tool_session_scope, authority),
            signer=authority.signer,
        )
    except Exception as exc:
        raise RuntimeError(
            "arbora fleet loader attach failed: the intent tools could not be "
            "bound to the MCP fleet (find/act/manage fleet operations), so the "
            "served surface would silently lose fleet access."
        ) from exc


def _with_browser(
    factory: BrowserBoundaryFactory | None,
    browser: BrowserState,
    keys: JwksCache,
    settings: RuntimeConfig,
) -> BrowserBoundaryFactory | None:
    """The browser boundary factory handed the host's browser state, keys and settings."""
    if factory is None:
        return None
    return partial(factory, browser=browser, keys=keys, settings=settings)


def _served_invocation(
    *,
    host: HostContext,
    browser_boundary_factory: BrowserBoundaryFactory | None = None,
) -> tuple[HostedInvocation | None, Any]:
    """Compose the operation-invoke API when an engine endpoint is configured.

    The process authority is minted first because the invoke runtime's service
    principal and scopes are that authority's. Without ``GRAPH_SERVICE_ENDPOINTS``
    the process has no engine and mounts no operation-invoke API.
    """
    from arbora.mcp_server.task_plan import configured_task_plan

    task_plan_config = configured_task_plan(host.config)
    if not host.config.graph_service_endpoints:
        if task_plan_config is not None:
            raise ValueError("task planning requires graph service endpoints")
        return None, None
    from arbora.a2a.source_grant import EngineSourceGrants
    from arbora.agent_host.default_agent import (
        DefaultAgentDefinition,
        DefaultAgentRegistrar,
    )
    from arbora.core.agent_names import FLEET_AGENT_NAME
    from arbora.mcp_server.backing.server_factory import create_mcp_parser
    from arbora.mcp_server.current_plan_reader import NativeCurrentPlanReader
    from arbora.mcp_server.fleet_composition import LateFleetGateway
    from arbora.mcp_server.run_reader import NativeRunReader
    from arbora.mcp_server.served_invocation import served_invocation_from_settings
    from arbora.mcp_server.source_reader import NativeSourceReader
    from arbora.modality.fleet_port import NativeIntentFleet
    from arbora.workflow_worker import WorkflowRunStarter

    args, _unknown = create_mcp_parser().parse_known_args()
    session = runtime._mint_process_session(
        str(getattr(args, "transport", "stdio")), host.caches.jwks, host.config
    )
    from arbora.browser_control.browser_control_runtime import (
        dispatch_browser_operation,
    )

    fleet = LateFleetGateway()
    invocation = served_invocation_from_settings(
        config=host.config,
        keys=host.caches.jwks,
        process_session=lambda: session,
        engine_pool=host.engine.pool,
        action_policies=host.policy.action_policies,
        bindings={
            "a2a": host.runs.a2a,
            "default_agent": DefaultAgentRegistrar(
                DefaultAgentDefinition(agent_id=FLEET_AGENT_NAME)
            ),
            "engine": host.engine.served,
            "run_origins": host.runs.origins,
            "run_ports": host.runs.ports,
            "health": host.observability.health,
            "config": host.config,
            "fleet_gateway": fleet,
            "workflow_worker": WorkflowRunStarter(
                host.engine.served, NativeIntentFleet(), worker_ref="arbora-workflow"
            ),
            "fleet_virtual_catalog": host.fleet.virtual_catalog,
            "fleet_declarations": host.fleet.declarations,
            "browser_control": partial(dispatch_browser_operation, host.browser),
            "current_plan_reader": NativeCurrentPlanReader(),
            "source_reader": NativeSourceReader(),
            "source_grants": EngineSourceGrants(host.config.source_run_branch),
            "run_trace_reader": NativeRunReader(
                host.engine.served, origins=host.runs.origins, config=host.config
            ),
        },
        fleet_effect=fleet.effect,
        browser_boundary_factory=_with_browser(
            browser_boundary_factory, host.browser, host.caches.jwks, host.config
        ),
    )
    return invocation, session


def bind_credential_authority(fleet_mux: Any, invocation: Any) -> None:
    """Give the multiplexer's child call path the host's credential authority (R893)."""
    send = getattr(invocation, "engine_send", None)
    if send is None or fleet_mux is None:
        return
    import time

    from arbora.gateway.credential_engine import engine_authority

    fleet_mux.credential_authority = engine_authority(
        send, lambda: time.time_ns() // 1_000_000
    )


def _bind_served_fleet(
    invocation: HostedInvocation | None, fleet_mux: Any, reader: Any
) -> None:
    """Bind the governed fleet gateway once the multiplexer is attached."""
    if invocation is None:
        return
    from arbora.mcp_server.fleet_composition import (
        LateFleetGateway,
        bind_fleet_gateway,
    )

    bind_credential_authority(fleet_mux, invocation)
    late = getattr(invocation.services.runtime, "bindings", {}).get("fleet_gateway")
    if isinstance(late, LateFleetGateway):
        bind_fleet_gateway(
            late,
            mux=fleet_mux,
            reader=reader,
            services=invocation.services,
            engine_send=invocation.engine_send,
            feature_schema=invocation.decision_feature_schema,
            principal=invocation.decision_principal,
        )


def _register_resident_surface(
    mcp: Any, fleet_mux: Any, invocation: HostedInvocation | None
) -> None:
    """Attach the six verbs and four fleet tools once (ARBORA-OPS-R035.2).

    Runs after the multiplexer is attached and the fleet gateway is bound, so
    the live authorities exist. A failure aborts startup: a host that served
    a surface without them would silently lose the governed fleet.
    """
    if invocation is None:
        return
    from arbora.mcp_server.fleet_composition import LateFleetGateway, session_key
    from arbora.mcp_server.registration import FleetMCPBinding, register_mcp_tools

    late = getattr(invocation.services.runtime, "bindings", {}).get("fleet_gateway")
    projection = runtime.SERVED_PROJECTION
    if not isinstance(late, LateFleetGateway) or late.ops is None or projection is None:
        raise RuntimeError(
            "the resident MCP surface needs the bound fleet gateway and the "
            "served operation projection"
        )
    binding = FleetMCPBinding(projection, late.ops, session_key)
    asyncio.run(register_mcp_tools(mcp, fleet_mux, binding))


def _start_catalog_warmup(
    slot: WarmupSlot,
    engine: Any,
    session: Any,
    reader: Any,
    fleet_mux: Any,
    action_policies: ActionPolicies,
    authority: ProcessAuthority,
    gateway: Any,
    feature_schema: Any = None,
) -> Any:
    """Compose the catalog authorities, then onboard the fleet, off boot.

    ``feature_schema`` is the served invocation's verified-pin lookup; the
    definition admission reads it at boot.

    Both run under the process session on the warm-up thread, which does not
    inherit the boot thread's context (ARBORA-FLEET-R078).
    """
    from arbora.mcp_server.catalog_composition import compose_catalog_authorities
    from arbora.mcp_server.catalog_warmup import start_catalog_warmup
    from arbora.security.actor_context import use_actor
    from arbora.security.session import use_session

    async def compose() -> None:
        from arbora.epistemic import temporary_client_scope

        # The warm-up loop runs beside the serving loop. Its engine
        # transports are scoped to it, so they never claim the serving loop's
        # pool clients (the onboarding thread does the same, R041.18).
        with use_actor(session.actor), use_session(session):
            async with temporary_client_scope():
                await compose_catalog_authorities(
                    gateway=gateway,
                    engine=engine,
                    session=session,
                    deferred_fleet=reader,
                    multiplexer=fleet_mux,
                )

    def onboard() -> None:
        # Register, attest and import each child catalog, then renew leases.
        # A fault logs and retries; it never blocks or stops serving.
        from arbora.fleet.onboarding import start_fleet_onboarding

        with use_actor(session.actor), use_session(session):
            start_fleet_onboarding(
                gateway=gateway,
                engine=engine,
                session=session,
                multiplexer=fleet_mux,
                virtual_catalog=fleet_mux.fleet.virtual_catalog,
                declarations=fleet_mux.fleet.declarations,
                action_policies=action_policies,
                feature_schema=feature_schema,
            )

    def reestablish() -> None:
        # Renew the process lease in place (the captured session is kept), so
        # the repeated composition runs under a live engine session.
        from arbora.mcp_server.bootstrap import _refresh_process_authority

        _refresh_process_authority(authority, session)

    return start_catalog_warmup(slot, compose, after=onboard, reestablish=reestablish)


def _require_browser_identity_factory(
    config: RuntimeConfig, browser_boundary_factory: Any, invocation: Any
) -> None:
    """Refuse an EG browser deployment that arrives without its identity factory."""
    if (
        config.enable_web_ui
        and config.webui_auth_mode == "eg"
        and browser_boundary_factory is None
        and invocation is None
    ):
        raise RuntimeError("EG browser requires the process-root identity factory")


def mcp_server(
    *,
    invocation: HostedInvocation | None = None,
    browser_boundary_factory: BrowserBoundaryFactory | None = None,
    identity_lifecycle: IdentityLifecycle | None = None,
) -> None:
    """``arbora`` MCP server entry point (registered as console_scripts).

    Thin FastMCP wrapper following the standard ``mcp_server.py`` template: it
    serves ONLY the MCP tool surface, over ``stdio`` or ``streamable-http``,
    selected by the standard ``--transport/--host/--port`` args
    from :func:`create_mcp_server`. The REST API (``/graph/*``, ``/sessions``,
    ``/goals``, ``/tools``) is centralized in the API gateway
    (:mod:`arbora.gateway`) — see :func:`_mount_rest_routes`.
    """
    from arbora.console import print_usage_or_version

    if print_usage_or_version(sys.argv[1:]):
        return
    from arbora.core.settings import load_config

    load_config()  # resolve settings through the one shared XDG config.json
    from arbora.core.environment_manifest import enforce_startup
    from arbora.mcp_server.backing.server_factory import create_mcp_parser
    from arbora.mcp_server.serving_options import parse_serving_options
    from arbora.security.anonymous import validate_startup

    enforce_startup()  # refuses before any listener, child process or engine call
    startup_args = parse_serving_options(create_mcp_parser)
    validate_startup(startup_args)
    early_session = None
    from arbora.mcp_server.host_context import build_host_context

    host_context = build_host_context()
    _require_browser_identity_factory(
        host_context.config, browser_boundary_factory, invocation
    )
    from arbora.security.error_surface import bind_errors

    bind_errors(host_context.observability.errors)
    if invocation is None:
        invocation, early_session = (
            _served_invocation(
                host=host_context, browser_boundary_factory=browser_boundary_factory
            )
            if browser_boundary_factory is not None
            else _served_invocation(host=host_context)
        )
    bind_a2a_decision(host_context, invocation)
    bind_ranking(host_context, invocation)
    install_gateway_application(
        invocation,
        gateway=host_context.gateway,
        fleet=host_context.fleet,
        approvals=host_context.policy.approvals,
        engine_provider=host_context.engine.served,
        caches=host_context.caches,
    )
    from arbora.host_bindings import bind_health_probes, bind_messaging_ports

    bind_messaging_ports(
        host_context.engine.served,
        host_context.caches.tools,
        host_context.observability.signals.runtime,
    )
    from arbora.a2a.chain_composition import run_plan_binding
    from arbora.agent_host.port_backends import HostSeams
    from arbora.agent_host.sandbox_boundary import boundary_verified

    chain = getattr(invocation, "approval_chain", None)
    run_composition = {
        "services": invocation.services if invocation is not None else None,
        "seams": HostSeams(
            changeset=_changeset_seams(invocation),
            question_schema=getattr(invocation, "ranking_feature_schema", None),
            sandbox_verified=boundary_verified,
            approval_chain=chain,
            plan_for=run_plan_binding(invocation.services)
            if invocation is not None and chain is not None
            else None,
            action_policy=lambda: host_context.policy.action_policies.for_engine(
                host_context.engine.served()
            ),
        ),
    }
    bind_health_probes(
        host_context.observability.health,
        host_context.engine.served,
        host_context.gateway,
    )
    bind_invocation_probes(host_context, invocation)
    _preflight_mcp_sdk_floor()
    telemetry = host_context.observability.telemetry
    _configure_arbora_otel(telemetry)
    _configure_telemetry_engine_otel(telemetry)
    write_env("IS_KG_SERVER", "true")
    if invocation is None:
        args, mcp, middlewares = runtime._build_server(host=host_context)
    else:
        args, mcp, middlewares = runtime._build_server(
            invocation=invocation, host=host_context
        )
    from arbora.security.anonymous import validate_startup

    validate_startup(args)
    _register_semantic_content(mcp)
    from arbora.fleet.catalog_reader import DeferredFleetCatalogReader

    fleet_catalog_reader = DeferredFleetCatalogReader()

    # Apply the middleware stack assembled by the factory.
    for middleware in middlewares:
        mcp.add_middleware(middleware)

    # Fold in the MCP fleet (retires the standalone mcp-multiplexer): the
    # intent tools reach it — find discovers, act calls, manage mounts — so no
    # tool is added to the served list. Attached AFTER the factory middlewares
    # so identity/auth is already applied. A failure here is NOT survivable: the
    # previous `except Exception: logger.error(...)` downgraded it to a log line
    # and served a silently wrong surface. Fail loud, preserving __cause__.
    # CONCEPT:MERISTEM-ECO.mcp.fleet-meta-tools-always-on
    # Inject arbora's own embedding model so fleet tool search ranks by
    # query↔description MEANING (semantic), not just literal token overlap.
    fleet_mux = _attach_fleet_runtime(
        mcp, fleet_catalog_reader, host_context.fleet, host_context.authority
    )
    # The harness's thin client host fails closed until arbora registers
    # its native toolset, fleet catalog and session keepalive bindings.
    from arbora.mcp_server.harness_ports import bind_host_ports_of

    bind_host_ports_of(host_context, multiplexer=fleet_mux, **run_composition)
    _bind_served_fleet(invocation, fleet_mux, fleet_catalog_reader)
    from arbora.identity.ports import bind_actor_exchange

    bind_actor_exchange(host_context.fleet)
    from arbora.fleet.credential_reuse import bind_credential_reuse

    bind_credential_reuse(host_context.fleet, invocation)
    _register_resident_surface(mcp, fleet_mux, invocation)

    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8000))
    network_serving = build_network_serving_config(args)

    bootstrap_session = early_session or runtime._mint_process_session(
        transport, host_context.caches.jwks, host_context.config
    )
    host_context.authority.bind_session(
        bootstrap_session if transport == "stdio" else None
    )
    health = host_context.observability.health
    runtime._start_process_authority_supervisor(
        host_context.authority, bootstrap_session, health
    )
    if identity_lifecycle is not None:
        # ARBORA-IDENTITY-R030.4: arbora reconciles its own OAuth clients.
        identity_lifecycle.start(health.identity)
    # Readiness probes the live fleet/goal authority, which is a real graph read
    # and therefore needs a bound session. the authority's session slot is deliberately
    # None on network transports (it is a stdio fallback, and must not become a
    # way for a request path to pick up identity it never authenticated), so
    # readiness gets its own narrowly-scoped handle on the process authority.
    # Without this the probe measured its own missing identity instead of the
    # authority, reported the goal store `unavailable`, and held /health/ready
    # at 503 forever on every served deployment.
    readiness_authority_owner = runtime._set_readiness_authority(
        health, bootstrap_session
    )

    co_service_supervisor = None
    catalog_warmup = None
    background_host = None
    try:
        from arbora.mcp_server.serving_options import log_startup

        log_startup(logger, transport, runtime.SERVED_PROJECTION, fleet_mux)

        from arbora.security.request_identity import (
            apply_served_security_profile,
        )

        # Network transports serve many clients at once: enforce server-validated
        # identity + tenant scoping, or fail loud (CONCEPT:MERISTEM-OS.identity.authenticated-identity-enforcement). No-op for stdio.
        apply_served_security_profile(
            transport,
            transport_auth_configured=(
                str(getattr(args, "auth_type", "none") or "none").lower() != "none"
            ),
        )

        # Stdout purity on the stdio transport needs no call here: it is owned
        # fd-level by the MCP SDK's own ``stdio_server()`` for the scope of the
        # later stdio-serve call below (see the "Stdio JSON-RPC purity" note in
        # server_factory.py) — that covers every co-service thread started
        # below too, since they share this process's file-descriptor table for
        # as long as serving blocks. The residual window before that call
        # claims fd 1 (engine bootstrap, co-service startup, this function
        # itself) is covered by the static "no print() in the served package"
        # gate (``scripts/check_no_stdout_writes.py``), not a runtime patch.
        # No-op for network transports either way (they don't own stdout as a
        # protocol channel).

        # Bind the minted process session (+ its verified actor) as ambient
        # authority before engine bootstrap. An explicit client role remains a
        # hard serving-plane boundary; this entrypoint never promotes itself to
        # the host that owns maintenance, workers, or autonomous loops.
        config = host_context.config
        from arbora.security.actor_context import use_actor
        from arbora.security.session import use_session

        with use_actor(bootstrap_session.actor), use_session(bootstrap_session):
            runtime._start_engine_bootstrap(
                host_context.engine.served(), bootstrap_session, host_context.authority
            )

            # A fresh store has no tenant or `__control__` graph; leases, queues
            # and workers read them, so create them before anything else runs.
            from arbora.semantic_provisioning import ensure_base_graphs

            base_graphs = asyncio.run(
                ensure_base_graphs(
                    engine=host_context.engine.served(), session=bootstrap_session
                )
            )
            logger.info("Arbora base graphs ensured: %s", base_graphs)

            # Versioned data migrations of persisted records, under the process
            # authority, before any reader (the action-policy ledger) runs.
            from arbora.data_migrations import run_data_migrations

            run_data_migrations(host_context.engine.served())

            # ARBORA-FLEET-R078: the catalog authorities compose on a
            # background thread, so arbora binds and serves at once; a slow or
            # failing engine call is retried there and never exits the process.
            # Fleet onboarding starts once the catalog is ready.
            catalog_warmup = _start_catalog_warmup(
                host_context.caches.warmup,
                host_context.engine.served(),
                bootstrap_session,
                fleet_catalog_reader,
                fleet_mux,
                host_context.policy.action_policies,
                host_context.authority,
                host_context.gateway,
                getattr(invocation, "decision_feature_schema", None),
            )

            from arbora.mcp_server.agent_control import register_graph_rlm

            graph_compute = host_context.engine.served().graph_compute

            def client_for_session(session: Any) -> Any:
                # The session's own graph: AU graph views refuse any other.
                return graph_compute.for_graph(str(session.graph)).async_client

            from arbora.mcp_server.decide_wiring import install_decide_consumers

            installed = install_decide_consumers(
                client_for_session,
                bootstrap_session,
                host_context.engine.served(),
            )
            logger.info("Decide consumers installed: %s", installed)

            # The host's own schedulers start with the process (ARBORA-OPS-R958).
            if invocation is not None and invocation.background_services:
                from arbora.mcp_server.background import BackgroundHost

                background_host = BackgroundHost(
                    invocation.background_services,
                    host_context.observability.errors,
                )
                background_host.start()

            # Process-owned publications run here, under the process
            # authority, before any request is served (ARBORA-RELEASE-R003.4.4).
            if invocation is not None and invocation.process_startup is not None:
                asyncio.run(invocation.process_startup())

            from arbora.mcp_server.backing.arbora_surface import backing_server

            # An ``act`` operation on the backing server, not a listed tool.
            register_graph_rlm(
                backing_server(mcp),
                host_context.caches.tools,
                client_for_session=client_for_session,
                origins=host_context.runs.origins,
            )

            # Self-composing co-services, phase 2: messaging now that a real engine
            # exists. Credentials keep outbound sending available, but the explicit
            # MESSAGING_INTAKE_ENABLED deployment intent (false by default) is the
            # only way this request container may enter the shared native lease
            # boundary. When ENABLE_WEB_UI is true, the packaged overstory is
            # started in-process by this same supervisor as a separately bound,
            # independently restartable co-service.
            co_service_supervisor = start_composed_services(
                bootstrap_session,
                host_context.engine.served(),
                action_policies=host_context.policy.action_policies,
                tools=host_context.caches.tools,
                browser=host_context.browser,
                telemetry=host_context.observability.telemetry,
                host=host_context,
                settings=config,
                messaging_intake_enabled=config.messaging_intake_enabled,
                served_network=transport == "streamable-http",
                session_boundary=invocation.browser_boundary if invocation else None,
            )

        if transport == "stdio":
            mcp.run(transport="stdio")
        elif transport == "streamable-http":
            if network_serving is None:
                raise RuntimeError("network serving configuration is required")
            from arbora.security.anonymous import network_run_kwargs

            mcp.run(
                transport="streamable-http",
                host=host,
                port=port,
                **network_run_kwargs(
                    network_serving.fastmcp_run_kwargs(), host_context.caches.jwks
                ),
            )
        else:
            raise ValueError("arbora transport must be 'stdio' or 'streamable-http'")
    finally:
        if catalog_warmup is not None:
            catalog_warmup.stop()
        if background_host is not None:
            background_host.stop()
        if co_service_supervisor is not None:
            co_service_supervisor.stop_all()
        host_context.authority.bind_session(None)
        runtime._stop_process_authority_supervisor(
            host_context.authority, health, close_lease=True
        )
        if identity_lifecycle is not None:
            identity_lifecycle.stop(health.identity)
        runtime._release_readiness_authority(health, readiness_authority_owner)
        # Best-effort teardown of any lazily-mounted fleet children.
        if fleet_mux is not None:
            try:
                asyncio.run(fleet_mux.aclose())
            except Exception as exc:  # best-effort teardown of a lazily-mounted fleet child at process exit
                logger.warning("fleet loader close failed: %s", exc, exc_info=True)
