"""The host's owned observability state (ARBORA-HOST-R039.4).

``Observability`` is one field of ``HostContext``. It splits into sub-objects, each
owning its lock and changing state only through its own methods. Nothing here is
module-level mutable state: the composition root builds the field once and passes the
narrowest sub-object to the units that held the former module globals.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["Observability", "Telemetry"]


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


@dataclass(frozen=True, slots=True)
class Observability:
    """Every owned observability object of one host process."""

    telemetry: Telemetry
