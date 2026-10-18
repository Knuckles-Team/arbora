"""Arbora composition adapters for gateway and supervised co-services."""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from arbora.gateway.ports import configure_gateway_application
from arbora.mcp_server import runtime
from arbora.mcp_server.hosted_invocation import HostedInvocation
from arbora.mcp_server.routes import mount_rest_routes
from arbora.messaging.intake import configured_platforms, start_messaging_intake
from arbora.webui_host import run_web_ui

logger = logging.getLogger(__name__)

_MAX_RESTARTS = 5
_RESTART_WINDOW_SECONDS = 300.0
_MAX_BACKOFF_SECONDS = 30.0


@dataclasses.dataclass(frozen=True)
class CompositionPlan:
    """Arbora-owned decision about optional co-services."""

    messaging_platforms: tuple[str, ...] = ()
    web_ui_enabled: bool = False
    messaging_intake_enabled: bool = False
    #: Configured platforms the operator enabled for inbound intake.
    intake_platforms: tuple[str, ...] = ()

    @property
    def messaging_configured(self) -> bool:
        return bool(self.messaging_platforms)

    @property
    def messaging_intake_configured(self) -> bool:
        return bool(self.intake_platforms) and self.messaging_intake_enabled

    def co_service_names(self) -> tuple[str, ...]:
        """Names of co-services this composition would bring up (any backend)."""
        names: list[str] = []
        if self.messaging_intake_configured:
            names.append("messaging")
        if self.web_ui_enabled:
            names.append("agent-webui")
        return tuple(names)


def _explicitly_true(value: Any) -> bool:
    """True only for an explicit true value; the string ``"false"`` is false."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def detect_composition(
    engine: Any = None,
    *,
    messaging_intake_enabled: bool | None = None,
    served_network: bool = False,
) -> CompositionPlan:
    """Read optional co-service intent without starting background work.

    Single Telegram/messaging owner (root cause of the ``getUpdates`` 409
    conflict loop): only the gateway daemon process (``KG_DAEMON_ROLE=host``,
    ``arbora.gateway.daemon``) may ever start inbound polling. Every other
    entry point — the main MCP server, CLI, one-shot scripts — runs as
    ``KG_DAEMON_ROLE=client`` and stays send-only regardless of the
    ``MESSAGING_INTAKE_ENABLED`` deployment flag. A present ``TELEGRAM_BOT_TOKEN``
    alone must never decide pollership, so this role check is ANDed with the
    explicit flag rather than substituting for it.

    The served network host (``served_network``: the ``streamable-http``
    deployment container) is the channel gateway too (ARBORA-MESSAGING-R007):
    with the explicit flag it may enter intake, and the engine's per-channel
    WorkItem lease still admits exactly one poller per channel identity.
    """
    from agent_connector_sdk.config import csv_values

    from arbora.core.settings import runtime_config as config
    from arbora.core.settings import setting

    is_gateway_daemon_role = str(setting("KG_DAEMON_ROLE", "client")).strip() == "host"
    platforms = configured_platforms(engine)
    # Intake is enabled per platform: credentials alone never open a poller.
    enabled = set(csv_values(setting("MESSAGING_INTAKE_PLATFORMS", "")))
    return CompositionPlan(
        messaging_platforms=platforms,
        intake_platforms=tuple(p for p in platforms if p in enabled),
        web_ui_enabled=bool(getattr(config, "enable_web_ui", False)),
        messaging_intake_enabled=_explicitly_true(messaging_intake_enabled)
        and (is_gateway_daemon_role or served_network),
    )


class CoServiceSupervisor:
    """Own optional Arbora co-services under the caller's verified context."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._services: dict[str, tuple[threading.Event, threading.Thread]] = {}

    def start_service(
        self,
        name: str,
        run: Callable[[threading.Event], None],
        session: Any,
    ) -> None:
        """Start one bounded-restart service in a copy of the ambient context."""
        if session is None:
            raise PermissionError("verified co-service session is required")
        context = contextvars.copy_context()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=context.run,
            args=(self._run_supervised, name, run, stop_event),
            name=f"CoService-{name}",
            daemon=True,
        )
        with self._lock:
            if name in self._services:
                raise RuntimeError(f"co-service {name!r} is already running")
            self._services[name] = (stop_event, thread)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._services.pop(name, None)
            raise
        logger.info("co-service %s started", name)

    def _run_supervised(
        self,
        name: str,
        run: Callable[[threading.Event], None],
        stop_event: threading.Event,
    ) -> None:
        restarts: list[float] = []
        while not stop_event.is_set():
            self._run_once(name, run, stop_event)
            if stop_event.is_set():
                return
            now = time.monotonic()
            restarts = [
                then for then in restarts if now - then < _RESTART_WINDOW_SECONDS
            ]
            restarts.append(now)
            if len(restarts) > _MAX_RESTARTS:
                logger.error("co-service %s exceeded its restart bound", name)
                return
            stop_event.wait(min(2.0 ** len(restarts), _MAX_BACKOFF_SECONDS))

    @staticmethod
    def _run_once(
        name: str,
        run: Callable[[threading.Event], None],
        stop_event: threading.Event,
    ) -> None:
        try:
            run(stop_event)
        except Exception:
            logger.exception("co-service %s crashed", name)
        else:
            if not stop_event.is_set():
                logger.error("co-service %s exited without a stop request", name)

    def stop_all(self, timeout: float = 10.0) -> bool:
        """Signal all services and join within one shared shutdown deadline."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            services = list(self._services.items())
        for _name, (stop_event, _thread) in services:
            stop_event.set()
        for name, service in services:
            self._join_service(name, service, max(0.0, deadline - time.monotonic()))
        return not self.running()

    def stop_service(self, name: str, timeout: float = 5.0) -> bool:
        """Stop one caller-owned service without stopping borrowed siblings."""
        with self._lock:
            service = self._services.get(name)
        if service is None:
            return True
        service[0].set()
        return self._join_service(name, service, max(0.0, timeout))

    def _join_service(
        self,
        name: str,
        service: tuple[threading.Event, threading.Thread],
        timeout: float,
    ) -> bool:
        thread = service[1]
        if thread is threading.current_thread():
            return False
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.error(
                "co-service %s did not stop before the shutdown deadline", name
            )
            return False
        with self._lock:
            # A concurrent stop/start must not lose the replacement's handle.
            if self._services.get(name) is service:
                self._services.pop(name)
        return True

    def running(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                name
                for name, (_event, thread) in self._services.items()
                if thread.is_alive()
            )


class NativeGatewayApplication:
    """Concrete application behavior behind the gateway transport port."""

    def __init__(self, invocation: HostedInvocation | None = None) -> None:
        self._invocation = invocation

    async def execute_tool(self, tool: str, /, **kwargs: Any) -> Any:
        return await runtime._execute_tool(tool, **kwargs)

    def engine(self) -> Any:
        return runtime._get_engine()

    def ensure_tools_registered(self) -> None:
        runtime.ensure_tools_registered()

    def mount_rest_routes(
        self,
        app: Any,
        *,
        prefix: str,
        services: Any = None,
        visibility: Any = None,
        authenticator: Any = None,
        invoke: Any = None,
    ) -> None:
        """Mount an explicitly composed API only after contract admission.

        The existing process still supplies no API dependencies. Its legacy
        action surface is unchanged; no partial API composition falls back to it.
        """
        dependencies = (services, visibility, authenticator, invoke)
        if self._invocation is not None:
            if any(value is not None for value in dependencies):
                raise ValueError("cannot replace composed invocation dependencies")
            self._invocation.mount_http(app, prefix=prefix)
            return
        if all(value is None for value in dependencies):
            mount_rest_routes(app, prefix=prefix)
            return
        from arbora.api.registry.contract_admission import validate_contract_admission
        from arbora.api.registry.eg_binding import EgContractError

        if any(value is None for value in dependencies):
            raise EgContractError(
                "API composition requires services/auth/visibility/invoke"
            )
        from arbora.api.http.app import create_api_application

        validate_contract_admission(services.registry)
        child = create_api_application(
            services=services,
            visibility=visibility,
            authenticator=authenticator,
            invoke=invoke,
        )
        # Gateway callers already pass their /api prefix; an empty prefix uses
        # the public API root rather than mounting versioned routes at /v1.
        app.mount((prefix.rstrip("/") or "/api") + "/v1", child)

    def remote_oauth_grant_bindings(self, actor: Any) -> Sequence[Any]:
        from arbora.fleet.multiplexer import (
            current_remote_oauth_grant_bindings,
        )

        return current_remote_oauth_grant_bindings(actor)


def install_gateway_application(
    invocation: HostedInvocation | None = None,
) -> NativeGatewayApplication:
    """Install the native application adapter at the process composition root."""

    application = NativeGatewayApplication(invocation)
    configure_gateway_application(application)
    return application


def start_composed_services(
    session: Any,
    engine: Any,
    *,
    messaging_intake_enabled: bool | None = None,
    served_network: bool = False,
) -> CoServiceSupervisor:
    """Start Arbora messaging intake and the arbora-owned WebUI host."""

    plan = detect_composition(
        engine,
        messaging_intake_enabled=messaging_intake_enabled,
        served_network=served_network,
    )
    supervisor = CoServiceSupervisor()
    if plan.messaging_intake_configured:
        start_messaging_intake(supervisor, engine, session, plan.intake_platforms)
    elif plan.messaging_configured:
        logger.info("messaging credentials are present but inbound intake is disabled")
    if plan.web_ui_enabled:
        supervisor.start_service("agent-webui", run_web_ui, session)
    return supervisor
