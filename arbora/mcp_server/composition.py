"""Arbora composition adapters for gateway and supervised co-services."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import dataclasses
import logging
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions
from arbora.fleet.state import FleetState
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
_DEFAULT_PROBE_TIMEOUT_S = 2.0
_MAX_PORT = 65535


class EngineRefusal(RuntimeError):
    """A typed refusal of the engine lifecycle; ``code`` is the stable name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


class EngineEndpointError(EngineRefusal, ValueError):
    """The engine endpoint text is empty or malformed."""


@dataclasses.dataclass(frozen=True)
class EngineEndpoint:
    """Where one engine listens: a Unix socket path or a ``host:port`` address."""

    kind: Literal["unix", "tcp"]
    address: str

    def client_kwargs(self) -> dict[str, str]:
        """The keyword argument the engine client connects with."""
        return {"socket_path" if self.kind == "unix" else "tcp_addr": self.address}


def _malformed(text: str, why: str) -> EngineEndpointError:
    return EngineEndpointError(
        "ENGINE_ENDPOINT_MALFORMED", f"engine endpoint {text!r} is malformed: {why}"
    )


def parse_engine_endpoint(text: Any) -> EngineEndpoint:
    """Parse one engine endpoint.

    Accepts ``unix:///abs/path``, an absolute path, ``tcp://host:port`` and
    ``host:port``. Raises :class:`EngineEndpointError` with
    ``ENGINE_ENDPOINT_EMPTY`` or ``ENGINE_ENDPOINT_MALFORMED``.
    """
    value = str(text or "").strip()
    if not value:
        raise EngineEndpointError(
            "ENGINE_ENDPOINT_EMPTY", "the engine endpoint is empty"
        )
    if value.startswith("unix://"):
        value = value.removeprefix("unix://")
        if not value.startswith("/"):
            raise _malformed(value, "a socket path must be absolute")
    if value.startswith("/"):
        return EngineEndpoint("unix", value)
    hostport = value.removeprefix("tcp://")
    host, separator, port = hostport.rpartition(":")
    if "/" in hostport or not separator or not host:
        raise _malformed(value, "expected host:port or an absolute socket path")
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= _MAX_PORT:
        raise _malformed(value, f"the port must be 1 to {_MAX_PORT}")
    return EngineEndpoint("tcp", f"{host}:{int(port)}")


@dataclasses.dataclass(frozen=True)
class EngineProbe:
    """What a probe found: ``running`` (the engine answered) or ``absent``."""

    state: Literal["running", "absent"]
    #: The engine version from its ``Health`` reply, when it gave one.
    version: str | None = None


HealthReader = Callable[[EngineEndpoint, float], Awaitable[Mapping[str, Any]]]


async def _engine_health(
    endpoint: EngineEndpoint, timeout_s: float
) -> Mapping[str, Any]:
    """Call the engine ``Health`` method over a local anonymous transport."""
    from medulla import MedullaClient

    client = await MedullaClient.connect(
        **endpoint.client_kwargs(),
        auth_mode="none",
        connect_timeout=timeout_s,
        timeout=timeout_s,
    )
    try:
        return await client.health()
    finally:
        await client.close()


def _reachable(endpoint: EngineEndpoint, timeout_s: float) -> bool:
    """True when the transport accepts a connection within ``timeout_s``."""
    try:
        if endpoint.kind == "unix":
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout_s)
                sock.connect(endpoint.address)
            return True
        host, _, port = endpoint.address.rpartition(":")
        with socket.create_connection((host.strip("[]"), int(port)), timeout_s):
            return True
    except OSError:
        return False


def probe_engine(
    endpoint: EngineEndpoint,
    timeout_s: float | None = None,
    *,
    health: HealthReader | None = None,
) -> EngineProbe:
    """Report whether an engine answers on ``endpoint``. Never starts anything.

    The probe opens the transport, then calls the engine ``Health`` method. An
    engine that answers with a refusal still counts as running. A transport
    that refuses, or a reply later than ``timeout_s``, counts as absent.
    """
    timeout = _DEFAULT_PROBE_TIMEOUT_S if timeout_s is None else float(timeout_s)
    if not _reachable(endpoint, timeout):
        return EngineProbe("absent")
    reader = health or _engine_health

    def ask() -> Mapping[str, Any]:
        return asyncio.run(asyncio.wait_for(reader(endpoint, timeout), timeout))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
        try:
            reply = worker.submit(ask).result()
        except (TimeoutError, OSError):
            return EngineProbe("absent")
        except Exception:  # an engine refusal is still an answer
            return EngineProbe("running")
    version = reply.get("version") if isinstance(reply, Mapping) else None
    return EngineProbe("running", str(version) if version else None)


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
            names.append("overstory")
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
    from scion_sdk.config import csv_values

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

    def __init__(
        self,
        invocation: HostedInvocation | None = None,
        *,
        fleet: FleetState,
        approvals: ApprovalDecisions,
    ) -> None:
        self._invocation = invocation
        self._fleet = fleet
        self._approvals = approvals

    async def execute_tool(self, tool: str, /, **kwargs: Any) -> Any:
        return await runtime._execute_tool(tool, **kwargs)

    def engine(self) -> Any:
        return runtime._get_engine()

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, str]:
        return self._approvals.decide(self.engine(), approval_id, decision)

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
        from arbora.api.registry.eg_binding import EgContractError

        if any(value is None for value in dependencies):
            raise EgContractError(
                "API composition requires services/auth/visibility/invoke"
            )
        from arbora.api.http.app import create_api_application

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
        return self._fleet.oauth_brokers.grant_bindings(actor)


def install_gateway_application(
    invocation: HostedInvocation | None = None,
    *,
    fleet: FleetState,
    approvals: ApprovalDecisions,
) -> NativeGatewayApplication:
    """Install the native application adapter at the process composition root."""

    application = NativeGatewayApplication(invocation, fleet=fleet, approvals=approvals)
    configure_gateway_application(application)
    return application


def start_composed_services(
    session: Any,
    engine: Any,
    *,
    action_policies: ActionPolicies,
    messaging_intake_enabled: bool | None = None,
    served_network: bool = False,
    session_boundary: Any = None,
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
        from functools import partial

        runner = (
            partial(
                run_web_ui,
                action_policies=action_policies,
                session_boundary=session_boundary,
            )
            if session_boundary is not None
            else partial(run_web_ui, action_policies=action_policies)
        )
        supervisor.start_service("overstory", runner, session)
    return supervisor
