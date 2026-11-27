"""The gateway's owned run-time state (ARBORA-HOST-R039.7).

``GatewayState`` is the ``gateway`` field of the host context. Each part owns its
lock and changes state only through its own methods. Routers are built per
composition by ``build_<name>_router()``; nothing here is a module-level object.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.gateway.ports import GatewayApplicationPort

__all__ = [
    "RATE_CAP_PER_MINUTE",
    "EventRates",
    "Flag",
    "GatewayState",
    "Slot",
    "SqlSchemaHandler",
    "gateway_of",
    "gateway_on",
]

#: Naive storm cap: at most this many accepted events per source per minute. Its job
#: is to keep a misconfigured Alertmanager or Kuma from flooding the graph and the
#: task queue, not to be a real WAF.
RATE_CAP_PER_MINUTE = 120
_RATE_TABLE_LIMIT = 4096


#: ``(schema filter) -> (HTTP status, JSON body)`` of the SQL catalog projection.
SqlSchemaHandler = Callable[[str | None], Awaitable[tuple[int, dict[str, Any]]]]


class Slot[T]:
    """One value a composition binds and a route reads, under its own lock."""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: T | None = None

    def get(self) -> T | None:
        with self._lock:
            return self._value

    def set(self, value: T | None) -> None:
        with self._lock:
            self._value = value

    def get_or_build(self, build: Callable[[], T]) -> T:
        """The held value, built once on first use."""
        with self._lock:
            if self._value is None:
                self._value = build()
            return self._value


class Flag:
    """A one-way latch that serves once: a caller runs the start under the lock."""

    __slots__ = ("_claimed", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def run_once(self, start: Callable[[], bool]) -> bool:
        """Run ``start`` until it succeeds once; later calls answer ``True``."""
        with self._lock:
            if self._claimed:
                return True
            self._claimed = start()
            return self._claimed


class EventRates:
    """Per-source fleet-event counters over one-minute windows.

    A source is held under a keyed digest, so the table never stores the source text.
    """

    __slots__ = ("_counters", "_key", "_lock")

    def __init__(self) -> None:
        self._counters: dict[str, list[int]] = {}
        self._lock = threading.Lock()
        self._key = secrets.token_bytes(32)

    def source_ref(self, source: str) -> str:
        """The opaque reference of ``source`` for this process."""
        digest = hashlib.blake2s(
            source.encode("utf-8"), key=self._key, digest_size=12
        ).hexdigest()
        return f"source_{digest}"

    def capped(self, source: str, n: int = 1) -> bool:
        """True when accepting ``n`` more events from ``source`` exceeds the cap."""
        minute = int(time.time() // 60)
        ref = self.source_ref(source)
        with self._lock:
            if len(self._counters) >= _RATE_TABLE_LIMIT:
                self._drop_stale(minute)
                if (
                    len(self._counters) >= _RATE_TABLE_LIMIT
                    and ref not in self._counters
                ):
                    return True
            window = self._counters.get(ref)
            if window is None or window[0] != minute:
                window = [minute, 0]
                self._counters[ref] = window
            if window[1] + n > RATE_CAP_PER_MINUTE:
                return True
            window[1] += n
            return False

    def _drop_stale(self, minute: int) -> None:
        for key in [k for k, v in self._counters.items() if v[0] != minute]:
            self._counters.pop(key, None)


class GatewayState:
    """Every owned run-time object of one gateway."""

    __slots__ = (
        "aggregator",
        "application_slot",
        "broker",
        "catalog_generation",
        "catalog_reader",
        "catalog_state",
        "event_rates",
        "fleet_authorities",
        "ingest_worker",
        "metrics_listener",
        "scope_policy",
        "sql_schema",
    )

    def __init__(self) -> None:
        self.application_slot: Slot[GatewayApplicationPort] = Slot()
        #: The remote-OAuth broker, built on first use unless a composition injects one.
        self.broker: Slot[Any] = Slot()
        #: The remote-OAuth scope policy; unset passes the requested scopes through.
        self.scope_policy: Slot[Callable[..., Any]] = Slot()
        #: The dashboard aggregator; layout writes persist to the shared file.
        self.aggregator: Slot[Any] = Slot()
        #: The enhanced catalog reader the catalog composition binds.
        self.catalog_reader: Slot[Callable[[], Any]] = Slot()
        #: The supervisory readers; unbound readers fail closed as unavailable.
        self.fleet_authorities: Slot[Any] = Slot()
        #: The replica's catalog generation reader (ARBORA-FLEET-R051.2.2).
        self.catalog_generation: Slot[Callable[[], Any]] = Slot()
        #: The supervised ingest worker (ARBORA-FLEET-R045); unbound is not configured.
        self.ingest_worker: Slot[Any] = Slot()
        #: The catalog serving state reader (ARBORA-FLEET-R081.2); unbound is cold.
        self.catalog_state: Slot[Callable[[], Any]] = Slot()
        #: The read-only SQL catalog projection the composition binds: it answers
        #: the status code and the JSON body; unbound answers unavailable.
        self.sql_schema: Slot[SqlSchemaHandler] = Slot()
        self.event_rates = EventRates()
        #: Set once the standalone daemon's metrics listener is serving.
        self.metrics_listener = Flag()

    def bind_application(self, application: GatewayApplicationPort) -> None:
        """Install the application adapter once at the composition root."""
        if not isinstance(application, GatewayApplicationPort):
            raise TypeError("application does not implement GatewayApplicationPort")
        self.application_slot.set(application)

    def application(self) -> GatewayApplicationPort:
        """Return the bound adapter, failing closed before composition."""
        application = self.application_slot.get()
        if application is None:
            raise RuntimeError(
                "arbora gateway application port is not configured; "
                "the composition root must bind it before serving requests"
            )
        return application


def gateway_on(app: Any) -> GatewayState:
    """The gateway field of the host an application was composed over."""
    return app.state.host.gateway


def gateway_of(carrier: Any) -> GatewayState:
    """The gateway field of the host of a request's or websocket's application."""
    return gateway_on(carrier.app)
