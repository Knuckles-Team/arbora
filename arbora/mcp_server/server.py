"""Serving lifecycle for the native arbora MCP application."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from agent_connector_sdk.mcp.content import register_connector_content
from agent_connector_sdk.mcp.network import build_network_serving_config

from arbora.core.settings import setting
from arbora.mcp_server import runtime
from arbora.mcp_server.composition import (
    install_gateway_application,
    start_composed_services,
)
from arbora.mcp_server.hosted_invocation import HostedInvocation
from arbora.semantic_content import default_content_providers

logger = logging.getLogger(__name__)


def _register_semantic_content(mcp: Any) -> None:
    """Expose independently owned semantic artifacts through the SDK contract.

    ConnectorPack capture consumes these MCP resources and EG ``AttachPack``
    remains the sole authority that interprets/activates their SHACL content.
    Arbora deliberately performs no local shape parsing or attachment.
    """

    for provider in default_content_providers():
        register_connector_content(mcp, provider())


def _configure_arbora_otel() -> None:
    """Activate the canonical metadata-only OTLP pipeline when configured."""

    if not setting("ENABLE_OTEL", False):
        return
    try:
        from arbora.observability.custom_observability import setup_otel

        setup_otel(service_name="arbora")
    except Exception as exc:  # noqa: BLE001 - observability cannot prevent serving
        logger.warning(
            "Arbora OTLP setup failed; trace export is disabled: %s",
            exc,
        )


def _configure_telemetry_engine_otel() -> None:
    """Eagerly start the standard-env-var OTLP trace pipeline (X2).

    CONCEPT:AU-OS.observability.telemetry-observability — independent of
    ``_configure_arbora_otel``'s ``ENABLE_OTEL``-gated Logfire/Langfuse
    pipeline: :func:`~arbora.observability.telemetry.configure_standard_otel`
    self-gates purely on the standard ``OTEL_EXPORTER_OTLP_ENDPOINT``/``OTEL_SERVICE_NAME``/
    ``OTEL_TRACES_EXPORTER`` vars (falling back to ``EPISTEMIC_GRAPH_OBS_ADDR``),
    so no new env var is introduced here. This is the ONE process-bootstrap
    call site that activates it for the arbora MCP server — never per-request.
    """
    try:
        from arbora.observability.telemetry import configure_standard_otel

        configured = configure_standard_otel(service_name="arbora")
        logger.info(
            "Arbora OTLP trace export (standard env vars): %s",
            "enabled" if configured else "disabled",
        )
    except Exception as exc:  # noqa: BLE001 - observability cannot prevent serving
        logger.warning(
            "Arbora TelemetryEngine OTel setup failed: %s",
            exc,
        )


def _preflight_mcp_sdk_floor() -> None:
    """Fail startup loudly when the installed MCP SDK is below the declared floor.

    CONCEPT:AU-ECO.mcp.protocol-compat-bridge — closes D-OB-18.

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

    mode = str(setting("MCP_SDK_FLOOR_ENFORCE", "error") or "error").strip().lower()
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


def _attach_fleet_runtime(mcp: Any, fleet_catalog_reader: Any) -> Any:
    """Attach the mandatory fleet surface or fail before serving."""

    try:
        from arbora.fleet.multiplexer import attach_fleet_loader

        return attach_fleet_loader(
            mcp,
            catalog_reader=fleet_catalog_reader,
            authority_scope=runtime.verified_tool_session_scope,
        )
    except Exception as exc:
        raise RuntimeError(
            "arbora fleet loader attach failed: the intent tools could not be "
            "bound to the MCP fleet (find/act/manage fleet operations), so the "
            "served surface would silently lose fleet access."
        ) from exc


def _served_invocation() -> tuple[HostedInvocation | None, Any]:
    """Compose the operation-invoke API when an engine endpoint is configured.

    The process authority is minted first because the invoke runtime's service
    principal and scopes are that authority's. Without ``GRAPH_SERVICE_ENDPOINTS``
    the process has no engine and mounts no operation-invoke API.
    """
    from arbora.core.settings import runtime_config

    if not runtime_config.graph_service_endpoints:
        return None, None
    from arbora.a2a import mcp as a2a_mcp
    from arbora.mcp_server.backing.server_factory import create_mcp_parser
    from arbora.mcp_server.current_plan_reader import NativeCurrentPlanReader
    from arbora.mcp_server.fleet_composition import LateFleetGateway
    from arbora.mcp_server.served_invocation import served_invocation_from_settings
    from arbora.mcp_server.source_reader import NativeSourceReader

    args, _unknown = create_mcp_parser().parse_known_args()
    session = runtime._mint_process_session(str(getattr(args, "transport", "stdio")))
    fleet = LateFleetGateway()
    invocation = served_invocation_from_settings(
        process_session=lambda: session,
        bindings={
            "a2a": a2a_mcp._service(),
            "fleet_gateway": fleet,
            "current_plan_reader": NativeCurrentPlanReader(),
            "source_reader": NativeSourceReader(),
        },
        fleet_effect=fleet.effect,
    )
    return invocation, session


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

    late = getattr(invocation.services.runtime, "bindings", {}).get("fleet_gateway")
    if isinstance(late, LateFleetGateway):
        bind_fleet_gateway(
            late, mux=fleet_mux, reader=reader, services=invocation.services
        )


def mcp_server(*, invocation: HostedInvocation | None = None) -> None:
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
    from arbora.mcp_server.backing.server_factory import create_mcp_parser
    from arbora.security.anonymous import validate_startup

    startup_args, _unknown = create_mcp_parser().parse_known_args()
    validate_startup(startup_args)
    early_session = None
    if invocation is None:
        invocation, early_session = _served_invocation()
    if invocation is None:
        install_gateway_application()
    else:
        install_gateway_application(invocation)
    from arbora.host_bindings import (
        bind_health_probes,
        bind_messaging_ports,
        bind_run_ports,
    )

    bind_messaging_ports()
    bind_run_ports()
    bind_health_probes()
    _preflight_mcp_sdk_floor()
    _configure_arbora_otel()
    _configure_telemetry_engine_otel()
    os.environ["IS_KG_SERVER"] = "true"
    if invocation is None:
        args, mcp, middlewares = runtime._build_server()
    else:
        args, mcp, middlewares = runtime._build_server(invocation=invocation)
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
    # CONCEPT:AU-ECO.mcp.fleet-meta-tools-always-on
    # Inject arbora's own embedding model so fleet tool search ranks by
    # query↔description MEANING (semantic), not just literal token overlap.
    fleet_mux = _attach_fleet_runtime(mcp, fleet_catalog_reader)
    # The harness's thin client host fails closed until arbora registers
    # its native toolset, fleet catalog and session keepalive bindings.
    from arbora.mcp_server.harness_host import (
        register_harness_host,
        unregister_harness_host,
    )

    register_harness_host(fleet_mux)
    _bind_served_fleet(invocation, fleet_mux, fleet_catalog_reader)

    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8000))
    network_serving = build_network_serving_config(args)

    bootstrap_session = early_session or runtime._mint_process_session(transport)
    runtime._PROCESS_SESSION = bootstrap_session if transport == "stdio" else None
    runtime.set_process_session(runtime._PROCESS_SESSION)
    runtime._start_process_authority_supervisor(bootstrap_session)
    # Readiness probes the live fleet/goal authority, which is a real graph read
    # and therefore needs a bound session. `runtime._PROCESS_SESSION` is deliberately
    # None on network transports (it is a stdio fallback, and must not become a
    # way for a request path to pick up identity it never authenticated), so
    # readiness gets its own narrowly-scoped handle on the process authority.
    # Without this the probe measured its own missing identity instead of the
    # authority, reported the goal store `unavailable`, and held /health/ready
    # at 503 forever on every served deployment.
    readiness_authority_owner = runtime._set_readiness_authority(bootstrap_session)

    co_service_supervisor = None
    try:
        logger.info("Starting arbora MCP server (transport=%s)", transport)

        from arbora.security.request_identity import (
            apply_served_security_profile,
        )

        # Network transports serve many clients at once: enforce server-validated
        # identity + tenant scoping, or fail loud (CONCEPT:AU-OS.identity.authenticated-identity-enforcement). No-op for stdio.
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
        from arbora.api.contracts.session import use_session
        from arbora.core.settings import runtime_config as config
        from arbora.security.actor_context import use_actor

        with use_actor(bootstrap_session.actor), use_session(bootstrap_session):
            runtime._start_engine_bootstrap(bootstrap_session)

            # A fresh store has no tenant or `__control__` graph; leases, queues
            # and workers read them, so create them before anything else runs.
            from arbora.deployment.semantic_provisioning import ensure_base_graphs

            base_graphs = asyncio.run(
                ensure_base_graphs(
                    engine=runtime._get_engine(), session=bootstrap_session
                )
            )
            logger.info("Arbora base graphs ensured: %s", base_graphs)

            from arbora.mcp_server.catalog_composition import (
                compose_catalog_authorities,
            )

            asyncio.run(
                compose_catalog_authorities(
                    engine=runtime._get_engine(),
                    session=bootstrap_session,
                    deferred_fleet=fleet_catalog_reader,
                    multiplexer=fleet_mux,
                )
            )

            # Onboard the configured fleet in the background: register, attest
            # and import each child catalog, then renew leases. A fault logs
            # and retries; it never blocks or stops serving.
            from arbora.fleet.onboarding import start_fleet_onboarding

            start_fleet_onboarding(
                engine=runtime._get_engine(),
                session=bootstrap_session,
                multiplexer=fleet_mux,
            )

            from arbora.mcp_server.agent_control import register_graph_rlm

            graph_compute = runtime._get_engine().graph_compute

            def client_for_session(session: Any) -> Any:
                # The session's own graph: AU graph views refuse any other.
                return graph_compute.for_graph(str(session.graph)).async_client

            from arbora.mcp_server.decide_wiring import install_decide_consumers

            installed = install_decide_consumers(
                client_for_session,
                bootstrap_session,
                runtime._get_engine(),
            )
            logger.info("Decide consumers installed: %s", installed)

            from arbora.mcp_server.backing.arbora_surface import backing_server

            # An ``act`` operation on the backing server, not a listed tool.
            register_graph_rlm(
                backing_server(mcp),
                client_for_session=client_for_session,
            )

            # Self-composing co-services, phase 2: messaging now that a real engine
            # exists. Credentials keep outbound sending available, but the explicit
            # MESSAGING_INTAKE_ENABLED deployment intent (false by default) is the
            # only way this request container may enter the shared native lease
            # boundary. When ENABLE_WEB_UI is true, the packaged agent-webui is
            # started in-process by this same supervisor as a separately bound,
            # independently restartable co-service.
            co_service_supervisor = start_composed_services(
                bootstrap_session,
                runtime._get_engine(),
                messaging_intake_enabled=config.messaging_intake_enabled,
                served_network=transport == "streamable-http",
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
                **network_run_kwargs(network_serving.fastmcp_run_kwargs()),
            )
        else:
            raise ValueError("arbora transport must be 'stdio' or 'streamable-http'")
    finally:
        unregister_harness_host()
        if co_service_supervisor is not None:
            co_service_supervisor.stop_all()
        runtime._PROCESS_SESSION = None
        runtime.set_process_session(None)
        runtime._stop_process_authority_supervisor()
        runtime._release_readiness_authority(readiness_authority_owner)
        # Best-effort teardown of any lazily-mounted fleet children.
        if fleet_mux is not None:
            try:
                asyncio.run(fleet_mux.aclose())
            except Exception as exc:  # noqa: BLE001 — best-effort teardown of a lazily-mounted fleet child at process exit
                logger.warning("fleet loader close failed: %s", exc)
