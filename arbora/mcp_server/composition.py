"""Arbora composition adapters for gateway and supervised co-services."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import contextvars
import dataclasses
import json
import logging
import os
import re
import signal
import socket
import subprocess  # nosec B404 - the one place that starts the local engine
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions
from arbora.browser_control.browser_state import BrowserState
from arbora.core.environment import inherited_environment, read_env
from arbora.core.settings import RuntimeConfig
from arbora.fleet.state import FleetState
from arbora.gateway.state import GatewayState
from arbora.mcp_server import runtime
from arbora.mcp_server.host_caches import HostCaches, ToolTable
from arbora.mcp_server.hosted_invocation import HostedInvocation
from arbora.mcp_server.routes import mount_rest_routes
from arbora.mcp_server.served_engine import EngineAttachments
from arbora.mcp_server.webui_mcp_delegation import webui_mcp_delegation_helpers
from arbora.messaging.intake import configured_platforms, start_messaging_intake
from arbora.messaging.memory_webhooks import start_memory_webhook_pump
from arbora.observability.state import Telemetry
from arbora.security.error_surface import carry_errors
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
        socket_path=endpoint.address if endpoint.kind == "unix" else None,
        tcp_addr=endpoint.address if endpoint.kind == "tcp" else None,
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
            reply = worker.submit(carry_errors(ask)).result()
        except (TimeoutError, OSError):
            return EngineProbe("absent")
        except Exception:  # an engine refusal is still an answer
            logger.debug("engine answered the probe with a refusal", exc_info=True)
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
    settings: RuntimeConfig,
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

    config = settings

    is_gateway_daemon_role = str(read_env("KG_DAEMON_ROLE", "client")).strip() == "host"
    platforms = configured_platforms(engine)
    # Intake is enabled per platform: credentials alone never open a poller.
    enabled = set(csv_values(read_env("MESSAGING_INTAKE_PLATFORMS")))
    return CompositionPlan(
        messaging_platforms=platforms,
        intake_platforms=tuple(p for p in platforms if p in enabled),
        web_ui_enabled=bool(getattr(config, "enable_web_ui", False)),
        messaging_intake_enabled=_explicitly_true(messaging_intake_enabled)
        and (is_gateway_daemon_role or served_network),
    )


@dataclasses.dataclass(frozen=True)
class LocalEngineConfig:
    """The local engine lifecycle settings (ARBORA-DEPLOY-R018 config keys)."""

    port: int = 47980
    start_timeout_s: float = 60.0
    probe_timeout_s: float = 2.0
    idle_shutdown_s: int = 300
    stop_grace_s: float = 10.0
    #: The engine binary; ``None`` uses the one the engine wheel packages.
    binary: str | None = None
    #: The persist directory; ``None`` uses ``<user data dir>/engine``.
    persist_dir: Path | None = None
    #: Extra engine arguments, for example the explicit local no-auth mode.
    extra_args: tuple[str, ...] = ()
    #: Extra engine environment, for example the bound connector-pack importer.
    extra_env: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def endpoint(self) -> EngineEndpoint:
        return EngineEndpoint("tcp", f"127.0.0.1:{self.port}")


_RANGES: Mapping[str, tuple[float, float]] = MappingProxyType(
    {
        "ENGINE_LOCAL_PORT": (1024, 65535),
        "ENGINE_START_TIMEOUT_S": (5, 600),
        "ENGINE_PROBE_TIMEOUT_S": (0.1, 30),
        "ENGINE_IDLE_SHUTDOWN_S": (30, 86400),
        "ENGINE_STOP_GRACE_S": (1, 120),
    }
)


def _ranged(name: str, default: float) -> float:

    raw = read_env(name, default)
    low, high = _RANGES[name]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    if not low <= value <= high:
        raise EngineRefusal(
            "ENGINE_CONFIG_INVALID", f"{name} must be a number from {low:g} to {high:g}"
        )
    return value


def local_engine_config(settings: RuntimeConfig, **overrides: Any) -> LocalEngineConfig:
    """The lifecycle settings from the environment; ``overrides`` win."""
    anonymous = settings.arbora_auth_mode == "none"
    base = LocalEngineConfig(
        extra_args=("--auth-mode", "none") if anonymous else (),
        extra_env=_anonymous_importer_env() if anonymous else {},
        port=int(_ranged("ENGINE_LOCAL_PORT", 47980)),
        start_timeout_s=_ranged("ENGINE_START_TIMEOUT_S", 60),
        probe_timeout_s=_ranged("ENGINE_PROBE_TIMEOUT_S", 2),
        idle_shutdown_s=int(_ranged("ENGINE_IDLE_SHUTDOWN_S", 300)),
        stop_grace_s=_ranged("ENGINE_STOP_GRACE_S", 10),
        binary=str(read_env("MEDULLA_BIN", "") or "") or None,
    )
    return dataclasses.replace(base, **overrides)


def _anonymous_importer_env() -> dict[str, str]:
    """Bind Arbora's own content pack import to the explicit no-auth principal."""
    import hashlib

    from arbora.security.anonymous import PRINCIPAL

    digest = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
    return {"MEDULLA_CONNECTOR_PACK_IMPORTERS": f"arbora=principal:sha256:{digest}"}


def _persist_dir(config: LocalEngineConfig) -> Path:
    if config.persist_dir is not None:
        return config.persist_dir
    import platformdirs

    return Path(platformdirs.user_data_dir("arbora")) / "engine"


def _engine_binary(config: LocalEngineConfig) -> str:
    """The engine executable, or an ``ENGINE_BINARY_MISSING`` refusal."""
    if config.binary:
        path = Path(config.binary).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise EngineRefusal(
            "ENGINE_BINARY_MISSING", "MEDULLA_BIN does not name an executable file"
        )
    try:
        from medulla.server_binary import resolve_server_binary

        return resolve_server_binary()
    except Exception as exc:
        raise EngineRefusal(
            "ENGINE_BINARY_MISSING", "no engine binary: install a medulla wheel"
        ) from exc


def _check_launch_contract(binary: str) -> None:
    """Refuse a binary that cannot stop itself when idle."""
    try:
        help_text = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineRefusal(
            "ENGINE_LAUNCH_CONTRACT_INVALID", "the engine binary cannot report --help"
        ) from exc
    if "--idle-shutdown-secs" not in help_text:
        raise EngineRefusal(
            "ENGINE_LAUNCH_CONTRACT_INVALID",
            "the engine binary lacks --idle-shutdown-secs",
        )


@dataclasses.dataclass
class _Attachment:
    """One endpoint this process is attached to, with its shared client lock."""

    endpoint: EngineEndpoint
    config: LocalEngineConfig
    lock_fd: int
    process: subprocess.Popen[bytes] | None = None


def _take_client_lock(endpoint: EngineEndpoint) -> int:
    from arbora.deployment import engine_lock, file_lock

    fd = os.open(
        str(engine_lock.engine_clients_lock_path(endpoint.address)),
        os.O_RDWR | os.O_CREAT,
        0o644,
    )
    try:
        file_lock.lock_shared_nb(fd)
    except OSError:
        # A peer is standing the engine down right now; wait for its verdict.
        file_lock.lock_shared(fd)
    return fd


def _write_marker(endpoint: EngineEndpoint, pid: int) -> None:
    from arbora.deployment import engine_lock

    engine_lock.engine_marker_path(endpoint.address).write_text(
        json.dumps({"pid": pid, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    )


def _spawn_engine(
    endpoint: EngineEndpoint, config: LocalEngineConfig
) -> subprocess.Popen[bytes]:
    """Start the engine once, under the spawn guard, and record ownership."""
    from arbora.deployment import engine_lock
    from arbora.observability.gateway_metrics import ENGINE_SPAWNS

    binary = _engine_binary(config)
    _check_launch_contract(binary)
    persist = _persist_dir(config)
    persist.mkdir(parents=True, exist_ok=True)
    command = [
        binary,
        "--tcp-only",
        "--tcp-addr",
        endpoint.address,
        "--persist-dir",
        str(persist),
        "--idle-shutdown-secs",
        str(config.idle_shutdown_s),
        *config.extra_args,
    ]
    with open(engine_lock.engine_log_path(endpoint.address), "ab") as log:
        process = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed argv
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**inherited_environment(), **config.extra_env},
        )
    ENGINE_SPAWNS.inc()
    _write_marker(endpoint, process.pid)
    logger.info(
        "started the local engine (pid %s) on %s", process.pid, endpoint.address
    )
    return process


def _wait_until_running(
    endpoint: EngineEndpoint,
    config: LocalEngineConfig,
    process: subprocess.Popen[bytes],
) -> None:
    deadline = time.monotonic() + config.start_timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise EngineRefusal(
                "ENGINE_START_TIMEOUT",
                f"the engine exited with status {process.returncode} while starting",
            )
        if probe_engine(endpoint, config.probe_timeout_s).state == "running":
            return
        time.sleep(0.2)
    raise EngineRefusal(
        "ENGINE_START_TIMEOUT",
        f"the engine did not answer within {config.start_timeout_s:g} seconds",
    )


def _abandon(endpoint: EngineEndpoint, process: subprocess.Popen[bytes]) -> None:
    from arbora.deployment import engine_lock

    with contextlib.suppress(OSError):
        process.terminate()
        process.wait(timeout=10)
    with contextlib.suppress(OSError):
        engine_lock.engine_marker_path(endpoint.address).unlink()


def ensure_local_engine(
    config: LocalEngineConfig, attachments: EngineAttachments
) -> EngineEndpoint:
    """Return the loopback engine endpoint, starting an engine only when none answers.

    Every process attaches by holding a shared lock on the endpoint's client
    lock for its life. Only a process that finds no engine starts one, under
    the per-endpoint spawn guard, and it alone writes the owner marker. On exit
    the last client out stops an engine this host started
    (:func:`stand_down_engine`).
    """
    from arbora.deployment import engine_lock

    endpoint = config.endpoint
    with attachments.lock:
        if endpoint.address in attachments.attached:
            return endpoint
        fd = _take_client_lock(endpoint)
        attachment = _Attachment(endpoint, config, fd)
        try:
            if probe_engine(endpoint, config.probe_timeout_s).state != "running":
                with engine_lock.engine_spawn_guard(endpoint.address) as sole:
                    if (
                        probe_engine(endpoint, config.probe_timeout_s).state
                        != "running"
                    ):
                        if not sole:
                            holder = engine_lock.engine_lock_holder(endpoint.address)
                            raise EngineRefusal(
                                "ENGINE_SPAWN_GUARD_BUSY",
                                "a peer is starting the engine"
                                f" (pid {(holder or {}).get('pid', '?')})",
                            )
                        attachment.process = _spawn_engine(endpoint, config)
                        attachments.spawned += 1
                        try:
                            _wait_until_running(endpoint, config, attachment.process)
                        except BaseException:
                            _abandon(endpoint, attachment.process)
                            raise
        except BaseException:
            _release_client_lock(fd)
            raise
        attachments.attached[endpoint.address] = attachment
        if len(attachments.attached) == 1:
            atexit.register(_stand_down_all, attachments)
    return endpoint


def _version_key(text: str) -> tuple[int, ...] | None:
    """A comparable key for a plain dotted release version, else ``None``."""
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(text))
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def engine_version_floor() -> tuple[int, ...] | None:
    """The lowest engine version this release serves, from its own package metadata."""
    from importlib import metadata

    try:
        requirements = metadata.requires("arbora") or []
    except metadata.PackageNotFoundError:
        return None
    floors = [
        key
        for text in requirements
        if (
            match := re.match(
                r"^medulla\s*(?:==|>=|~=)\s*([0-9][0-9.]*)\s*(?:;|$)", text.strip()
            )
        )
        and (key := _version_key(match.group(1)))
    ]
    return min(floors) if floors else None


def attach_reused_engine(
    endpoint: EngineEndpoint, probe_timeout_s: float | None = None
) -> EngineProbe:
    """Attach to an engine the operator chose to reuse; never start a replacement.

    A dead endpoint refuses ``REUSE_UNREACHABLE`` and an engine older than the
    floor refuses ``REUSE_INCOMPATIBLE``. Neither starts anything.
    """
    import hashlib

    digest = hashlib.sha256(endpoint.address.encode()).hexdigest()[:16]
    probe = probe_engine(endpoint, probe_timeout_s)
    if probe.state != "running":
        raise EngineRefusal(
            "REUSE_UNREACHABLE", f"the reused engine {digest} does not answer"
        )
    floor = engine_version_floor()
    found = _version_key(probe.version) if probe.version else None
    if floor is not None and found is not None and found < floor:
        raise EngineRefusal(
            "REUSE_INCOMPATIBLE",
            f"the reused engine {digest} is {probe.version}; this release needs"
            f" {'.'.join(map(str, floor))}",
        )
    logger.info("attached to the reused engine %s", digest)
    return probe


_CREDENTIAL_SETTINGS = ("GRAPH_SERVICE_AUTH_SECRET", "MEDULLA_SIGNER_KEYS_JSON")


def is_remote_engine(endpoint: EngineEndpoint) -> bool:
    """True for a TCP endpoint whose host is not loopback."""
    if endpoint.kind != "tcp":
        return False
    import ipaddress

    host = endpoint.address.rpartition(":")[0].strip("[]")
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def require_remote_engine_credential(endpoint: EngineEndpoint) -> None:
    """Refuse a remote engine that has no engine credential configured.

    A configured endpoint never starts a local engine. A remote one also needs
    the engine admission credential the deployment sets (see
    ``setup-config engine-admission``); without it Arbora refuses.
    """

    if is_remote_engine(endpoint) and not any(
        str(read_env(name, "") or "").strip() for name in _CREDENTIAL_SETTINGS
    ):
        raise EngineRefusal(
            "ENGINE_CREDENTIAL_MISSING",
            "a remote engine needs the engine admission credential",
        )


def _release_client_lock(fd: int) -> None:
    from arbora.deployment import file_lock

    file_lock.unlock(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stand_down_engine(endpoint: EngineEndpoint, attachments: EngineAttachments) -> bool:
    """Release this process's client lock; stop the engine when it owns the last one.

    Returns True when this call stopped the engine. The engine is stopped only
    when no other client holds the client lock and the owner marker exists.
    Otherwise it is left running.
    """
    from arbora.deployment import engine_lock, file_lock

    with attachments.lock:
        attachment = attachments.attached.pop(endpoint.address, None)
    if attachment is None:
        return False
    _release_client_lock(attachment.lock_fd)
    fd = os.open(
        str(engine_lock.engine_clients_lock_path(endpoint.address)),
        os.O_RDWR | os.O_CREAT,
        0o644,
    )
    try:
        try:
            file_lock.lock_exclusive_nb(fd)
        except OSError:
            return False  # another client remains
        marker = engine_lock.engine_marker_path(endpoint.address)
        if not marker.exists():
            return False
        return _stop_marked_engine(endpoint, attachment, marker)
    finally:
        file_lock.unlock(fd)
        os.close(fd)


def _owned_pid(endpoint: EngineEndpoint, marker: Path) -> int:
    """The pid of the engine this host started, or an ``OWNERSHIP_UNKNOWN`` refusal.

    An unreadable marker, a pid that is gone, or a process that does not
    carry this endpoint on its command line is not proof of ownership, so the
    caller never signals it.
    """

    def unknown(why: str) -> EngineRefusal:
        return EngineRefusal(
            "OWNERSHIP_UNKNOWN",
            f"refusing to stop the engine on {endpoint.address}: {why}",
        )

    try:
        pid = int(json.loads(marker.read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise unknown("the owner marker is unreadable") from exc
    if pid <= 1 or not _pid_alive(pid):
        raise unknown("the marked process is gone")
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return pid  # no /proc on this platform: the live pid is the evidence
    if endpoint.address.encode() not in command_line:
        raise unknown("the marked process is not this engine")
    return pid


def _stop_marked_engine(
    endpoint: EngineEndpoint, attachment: _Attachment, marker: Path
) -> bool:
    pid = _owned_pid(endpoint, marker)
    process = attachment.process
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        marker.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + attachment.config.stop_grace_s
    while time.monotonic() < deadline:
        if process is not None and process.pid == pid:
            if process.poll() is not None:
                break
        elif not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        logger.warning(
            "the engine on %s did not stop within the grace", endpoint.address
        )
    marker.unlink(missing_ok=True)
    return True


def _stand_down_all(attachments: EngineAttachments) -> None:
    for attachment in list(attachments.attached.values()):
        try:
            stand_down_engine(attachment.endpoint, attachments)
        except EngineRefusal as refusal:
            logger.error("engine stand-down refused: %s", refusal)
        except Exception:
            logger.exception(
                "engine stand-down failed for %s", attachment.endpoint.address
            )


def _service_thread_name(service: str, given: str | None) -> str:
    """The thread name a service asked for, else ``CoService-<service>``."""
    return given or f"CoService-{service}"


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
        *,
        thread_name: str | None = None,
    ) -> None:
        """Start one bounded-restart service in a copy of the ambient context.

        ``thread_name`` names the thread for a service whose thread is part of its
        contract (the terminal-mode HTTP host); the default is ``CoService-<name>``.
        """
        if session is None:
            raise PermissionError("verified co-service session is required")
        context = contextvars.copy_context()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=context.run,
            args=(self._run_supervised, name, run, stop_event),
            name=_service_thread_name(name, thread_name),
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

    def service_thread(self, name: str) -> threading.Thread | None:
        """The thread this supervisor started for ``name``, if it still holds it."""
        with self._lock:
            service = self._services.get(name)
        return None if service is None else service[1]

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
        engine_provider: Callable[[], Any],
        caches: HostCaches,
    ) -> None:
        self._engine_provider = engine_provider
        self._invocation = invocation
        self._fleet = fleet
        self._approvals = approvals
        self._caches = caches

    async def execute_tool(self, tool: str, /, **kwargs: Any) -> Any:
        return await runtime._execute_tool(self._caches.tools, tool, **kwargs)

    async def invoke_operation(
        self,
        op_id: str,
        params: Mapping[str, Any],
        /,
        *,
        request: Any,
        idempotency_key: str | None = None,
    ) -> Any:
        from arbora.api.invoke.pipeline import OperationRefused, invoke
        from arbora.api.registry import Surface
        from arbora.security.operation_result import OpError

        if self._invocation is None:
            raise OperationRefused("UNAVAILABLE", {"capability": "invoke"})
        caller = await self._invocation.authenticator.authenticate(request)
        result = await invoke(
            op_id,
            params,
            caller,
            Surface.HTTP,
            services=self._invocation.services,
            idempotency_key=idempotency_key,
        )
        if isinstance(result, OpError):
            raise OperationRefused(result.code, result.details)
        if result.code != "OK":
            raise OperationRefused(result.code, result.details)
        return result.value

    def engine(self) -> Any:
        return self._engine_provider()

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, str]:
        return self._approvals.decide(self.engine(), approval_id, decision)

    def ensure_tools_registered(self) -> None:
        runtime.ensure_tools_registered(self._caches)

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
            mount_rest_routes(app, self._caches.tools, prefix=prefix)
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
            config=services.config,
        )
        # Gateway callers already pass their /api prefix; an empty prefix uses
        # the public API root rather than mounting versioned routes at /v1.
        app.mount((prefix.rstrip("/") or "/api") + "/v1", child)

    def remote_oauth_grant_bindings(self, actor: Any) -> Sequence[Any]:
        return self._fleet.oauth_brokers.grant_bindings(actor)


def install_gateway_application(
    invocation: HostedInvocation | None = None,
    *,
    gateway: GatewayState,
    fleet: FleetState,
    approvals: ApprovalDecisions,
    engine_provider: Callable[[], Any],
    caches: HostCaches,
) -> NativeGatewayApplication:
    """Install the native application adapter at the process composition root."""

    application = NativeGatewayApplication(
        invocation,
        fleet=fleet,
        approvals=approvals,
        engine_provider=engine_provider,
        caches=caches,
    )
    gateway.bind_application(application)
    return application


def start_composed_services(
    session: Any,
    engine: Any,
    *,
    action_policies: ActionPolicies,
    tools: ToolTable,
    browser: BrowserState,
    telemetry: Telemetry,
    host: Any,
    settings: RuntimeConfig,
    messaging_intake_enabled: bool | None = None,
    served_network: bool = False,
    session_boundary: Any = None,
) -> CoServiceSupervisor:
    """Start Arbora messaging intake and the arbora-owned WebUI host."""

    plan = detect_composition(
        engine,
        settings=settings,
        messaging_intake_enabled=messaging_intake_enabled,
        served_network=served_network,
    )
    supervisor = CoServiceSupervisor()
    if plan.messaging_intake_configured:
        start_messaging_intake(supervisor, engine, session, plan.intake_platforms)
    elif plan.messaging_configured:
        logger.info("messaging credentials are present but inbound intake is disabled")

    if _explicitly_true(read_env("MEMORY_WEBHOOKS_ENABLED")):
        start_memory_webhook_pump(supervisor, engine, session)
    if plan.web_ui_enabled:
        from functools import partial

        runner = (
            partial(
                run_web_ui,
                action_policies=action_policies,
                engine=engine,
                verbs=tools.verbs,
                mcp_delegation=webui_mcp_delegation_helpers(tools),
                browser=browser,
                telemetry=telemetry,
                host_context=host,
                session_boundary=session_boundary,
            )
            if session_boundary is not None
            else partial(
                run_web_ui,
                action_policies=action_policies,
                engine=engine,
                verbs=tools.verbs,
                mcp_delegation=webui_mcp_delegation_helpers(tools),
                browser=browser,
                telemetry=telemetry,
                host_context=host,
            )
        )
        supervisor.start_service("overstory", runner, session)
    return supervisor
