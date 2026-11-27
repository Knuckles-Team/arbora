"""The host's owned observability state (ARBORA-HOST-R039.4).

``Observability`` is one field of ``HostContext``. It splits into sub-objects, each
owning its lock and changing state only through its own methods. Nothing here is
module-level mutable state: the composition root builds the field once and passes the
narrowest sub-object to the units that held the former module globals.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GatewayTrend",
    "RuntimeSignalBuffer",
    "Signals",
    "HealthState",
    "IdentityContract",
    "Observability",
    "build_health_state",
    "ProbeTable",
    "ReadinessAuthority",
    "Slot",
    "SUBSYSTEMS",
    "SubsystemWindows",
    "Telemetry",
]


class Telemetry:
    """The trace pipeline handles: OTLP provider, Langfuse exporter, event sequence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: Serializes the read-modify-write of a durable trace cursor.
        self.cursor_guard = threading.Lock()
        self._provider: Any = None
        self._exporter: Any = None
        self._exporter_built = False
        self._last_sequence = 0
        self._otel_initialized = False
        self._agent_instrumented = False

    def start_provider(self, build: Callable[[], Any]) -> bool:
        """Start the OTLP provider once; ``build`` returns it or ``None`` when unavailable."""
        with self._lock:
            if self._provider is not None:
                return True
            provider = build()
            self._provider = provider
            return provider is not None

    def stop_provider(self) -> None:
        """Shut the provider down (tests and process shutdown)."""
        with self._lock:
            provider, self._provider = self._provider, None
        if provider is not None:
            provider.shutdown()

    def exporter(self, build: Callable[[], Any]) -> Any:
        """Return the Langfuse exporter, building it on first use (``None`` if unconfigured)."""
        with self._lock:
            if not self._exporter_built:
                self._exporter = build()
                self._exporter_built = True
            return self._exporter

    def install_exporter(self, exporter: Any) -> None:
        """Install a specific exporter (tests inject a fake client)."""
        with self._lock:
            self._exporter = exporter
            self._exporter_built = True

    def reset_exporter(self) -> None:
        """Forget the exporter so the next call re-probes the environment."""
        with self._lock:
            self._exporter = None
            self._exporter_built = False

    def next_event_sequence(self) -> int:
        """Return a monotonic, wall-clock-anchored numeric event sequence."""
        with self._lock:
            self._last_sequence = max(time.time_ns(), self._last_sequence + 1)
            return self._last_sequence

    def mark_otel_initialized(self, *, agent_instrumented: bool) -> None:
        """Record that the metadata-only pipeline is up."""
        with self._lock:
            self._agent_instrumented = agent_instrumented
            self._otel_initialized = True

    @property
    def otel_initialized(self) -> bool:
        """Whether the metadata-only pipeline was initialized."""
        with self._lock:
            return self._otel_initialized

    @property
    def agent_instrumented(self) -> bool:
        """Whether the metadata-only agent instrumentation is active."""
        with self._lock:
            return self._agent_instrumented


#: The served subsystems whose calls are tracked.
SUBSYSTEMS = ("engine", "discovery", "fleet")


def _open_calls(names: Sequence[str]) -> dict[str, dict[int, float]]:
    return {name: {} for name in names}


def _recent_outcomes(
    names: Sequence[str], window: int
) -> dict[str, deque[tuple[bool, float]]]:
    return {name: deque(maxlen=window) for name in names}


class SubsystemWindows:
    """Per-subsystem in-flight calls and the window of recent outcomes."""

    def __init__(self, names: Sequence[str] = SUBSYSTEMS, window: int = 50) -> None:
        self._lock = threading.Lock()
        self._in_flight = _open_calls(names)
        self._recent = _recent_outcomes(names, window)
        self._next_id = 0

    def begin(self, name: str, started: float) -> int:
        """Record one call entering ``name``; return its id."""
        with self._lock:
            self._next_id += 1
            self._in_flight[name][self._next_id] = started
            return self._next_id

    def end(self, name: str, call_id: int, *, ok: bool, elapsed: float) -> None:
        """Record one call leaving ``name`` with its outcome."""
        with self._lock:
            self._in_flight[name].pop(call_id, None)
            self._recent[name].append((ok, elapsed))

    def view(
        self, names: Sequence[str]
    ) -> dict[str, tuple[list[float], list[tuple[bool, float]]]]:
        """One consistent copy of every named subsystem's calls and outcomes."""
        with self._lock:
            return {
                name: (list(self._in_flight[name].values()), list(self._recent[name]))
                for name in names
            }


class ProbeTable:
    """Named probes bound by the host; the table owns its lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probes: dict[str, Callable[..., Any]] = {}

    def bind(self, name: str, probe: Callable[..., Any]) -> None:
        """Bind (or replace) the probe for ``name``."""
        with self._lock:
            self._probes[name] = probe

    def clear(self) -> None:
        """Unbind every probe."""
        with self._lock:
            self._probes.clear()

    def get(self, name: str) -> Callable[..., Any] | None:
        """The probe bound to ``name``, or ``None``."""
        with self._lock:
            return self._probes.get(name)

    def copy(self) -> dict[str, Callable[..., Any]]:
        """A snapshot of every bound probe."""
        with self._lock:
            return dict(self._probes)


class ReadinessAuthority:
    """The one readiness session of the process and the token of its owner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: Any = None
        self._owner: object | None = None

    def claim(self, session: Any) -> object:
        """Claim the authority; a second claim fails closed."""
        with self._lock:
            if self._session is not None or self._owner is not None:
                raise RuntimeError("readiness authority is already claimed")
            self._session = session
            self._owner = object()
            return self._owner

    def release(self, owner: object) -> bool:
        """Release the authority only for the opaque token that claimed it."""
        with self._lock:
            if owner is not self._owner:
                return False
            self._session = None
            self._owner = None
            return True

    @property
    def session(self) -> Any:
        """The claimed readiness session, or ``None``."""
        with self._lock:
            return self._session


class Slot:
    """One replaceable value that readiness reads (the process credential lease)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Any = None

    def set(self, value: Any) -> None:
        """Replace the value (``None`` clears)."""
        with self._lock:
            self._value = value

    def get(self) -> Any:
        """The value, or ``None``."""
        with self._lock:
            return self._value

    def take(self) -> Any:
        """The value, cleared in the same step."""
        with self._lock:
            value, self._value = self._value, None
            return value


class IdentityContract:
    """The OAuth client contract reconciler and its config-invalid flag."""

    def __init__(self) -> None:
        self.reconciler = Slot()
        self.supervisor = Slot()
        self._lock = threading.Lock()
        self._config_invalid = False

    def set_config_invalid(self, invalid: bool) -> None:
        """Record that the contract configuration is set but unusable."""
        with self._lock:
            self._config_invalid = bool(invalid)

    @property
    def config_invalid(self) -> bool:
        """Whether the contract configuration is set but unusable."""
        with self._lock:
            return self._config_invalid


@dataclass(frozen=True, slots=True)
class HealthState:
    """Subsystem windows, the host's probes and the readiness inputs."""

    windows: SubsystemWindows
    probes: ProbeTable
    authority: ReadinessAuthority
    lease: Slot
    identity: IdentityContract
    readiness_probes: ProbeTable


def build_health_state() -> HealthState:
    """A fresh health state: empty windows, no probes, no authority claimed."""
    return HealthState(
        windows=SubsystemWindows(),
        probes=ProbeTable(),
        authority=ReadinessAuthority(),
        lease=Slot(),
        identity=IdentityContract(),
        readiness_probes=ProbeTable(),
    )


class RuntimeSignalBuffer:
    """The bounded ring of runtime-reliability signals and their node sequence."""

    def __init__(self, capacity: int = 512) -> None:
        self._lock = threading.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._sequence = itertools.count()

    def append(self, signal: dict[str, Any]) -> None:
        """Add one signal; the oldest drops when the ring is full."""
        with self._lock:
            self._buffer.append(signal)

    def snapshot(self) -> list[dict[str, Any]]:
        """A copy of the buffered signals."""
        with self._lock:
            return list(self._buffer)

    def drain(self) -> list[dict[str, Any]]:
        """Take and clear the buffered batch."""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
        return items

    def next_sequence(self) -> int:
        """The next per-occurrence node sequence number."""
        with self._lock:
            return next(self._sequence)


class GatewayTrend:
    """The gateway's request-latency trend buffer, window history and evidence store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffer: Any = None
        self._history: list[dict[str, Any]] = []
        self._store: Any = None
        self._tasks: set[Any] = set()

    def buffer(self, build: Callable[[], Any]) -> Any:
        """The trend buffer, built on first use."""
        with self._lock:
            if self._buffer is None:
                self._buffer = build()
            return self._buffer

    def history(self) -> list[dict[str, Any]]:
        """A copy of the rolling window history."""
        with self._lock:
            return list(self._history)

    def append_window(self, trend: dict[str, Any], keep: int) -> None:
        """Add one window; only the newest ``keep`` windows stay."""
        with self._lock:
            self._history.append(trend)
            del self._history[:-keep]

    def bind_store(self, store: Any) -> None:
        """Bind the store that keeps anomalous windows as evidence."""
        with self._lock:
            self._store = store

    def store(self) -> Any:
        """The bound evidence store, or ``None``."""
        with self._lock:
            return self._store

    def hold(self, task: Any) -> None:
        """Keep a strong reference to a background check until it completes."""
        with self._lock:
            self._tasks.add(task)
        task.add_done_callback(self._release)

    def _release(self, task: Any) -> None:
        with self._lock:
            self._tasks.discard(task)


@dataclass(frozen=True, slots=True)
class Signals:
    """Runtime-reliability signals and the gateway latency trend."""

    runtime: RuntimeSignalBuffer
    gateway: GatewayTrend


@dataclass(frozen=True, slots=True)
class Observability:
    """Every owned observability object of one host process."""

    telemetry: Telemetry
    health: HealthState
    signals: Signals
