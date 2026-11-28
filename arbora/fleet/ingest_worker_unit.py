"""Host and supervise the ingest worker; surface its health (ARBORA-FLEET-R045).

Arbora declares one supervised unit that runs the ``ingest-worker`` command of
``SCION-SOURCE-INGEST-R011`` against the served engine, and reports its health.
It holds no job logic, queue or policy: the gauges and counters of
``MEDULLA-INGEST-WORKER-R001.13`` are read through the engine metrics read and
returned unchanged. A stopped unit is ``unhealthy`` with reason
``worker_stopped``; an unreachable metrics read is ``metrics_unavailable``.
"""

from __future__ import annotations

import logging
import subprocess  # nosec B404 - the host starts one fixed-argv worker
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from arbora.fleet.health import FleetHealthSnapshot, _dependency, _with_dependency

__all__ = [
    "INGEST_WORKER_DEPENDENCY",
    "IngestWorkerHealth",
    "IngestWorkerSupervisor",
    "IngestWorkerUnit",
    "declare_ingest_worker_unit",
    "ingest_worker_health_snapshot",
    "with_ingest_worker_health",
    "with_ingest_worker_not_configured",
]

INGEST_WORKER_DEPENDENCY = "ingest_worker"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestWorkerUnit:
    """One supervised process: its name and the command line it runs."""

    name: str
    argv: tuple[str, ...]


class IngestWorkerState(StrEnum):
    """Whether the ingest worker answered its health probe."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class IngestWorkerHealth:
    state: IngestWorkerState
    reason: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)


def declare_ingest_worker_unit(
    *, engine_endpoint: str, enabled: bool = True
) -> IngestWorkerUnit | None:
    """The unit for the host to supervise, or ``None`` when configured off.

    ``enabled`` is the config key ``ingest_worker.enabled`` (default true).
    """
    if not enabled:
        return None
    if not engine_endpoint or not engine_endpoint.strip():
        raise ValueError("the ingest worker needs the served engine endpoint")
    return IngestWorkerUnit(
        name="ingest-worker",
        argv=("ingest-worker", "--engine-endpoint", engine_endpoint.strip()),
    )


def with_ingest_worker_health(
    snapshot: FleetHealthSnapshot,
    *,
    running: Callable[[], bool],
    read_metrics: Callable[[], Mapping[str, Any]],
    now: float | None = None,
) -> tuple[FleetHealthSnapshot, IngestWorkerHealth]:
    """Add the worker's health to a fleet snapshot and return it with the metrics."""
    checked = time.time() if now is None else float(now)
    if not running():
        health = IngestWorkerHealth(IngestWorkerState.UNHEALTHY, "worker_stopped")
    else:
        try:
            health = IngestWorkerHealth(
                IngestWorkerState.HEALTHY, None, dict(read_metrics())
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "an unreachable read is a state, not a crash", exc_info=True
            )
            health = IngestWorkerHealth(
                IngestWorkerState.UNHEALTHY, "metrics_unavailable"
            )
    if health.state == IngestWorkerState.HEALTHY:
        dependency = _dependency("healthy", checked, checked).model_copy(
            update={"metrics": dict(health.metrics)}
        )
    else:
        dependency = _dependency(
            "unavailable", checked, None, (f"ingest_worker: {health.reason}",)
        )
    return _with_dependency(snapshot, INGEST_WORKER_DEPENDENCY, dependency), health


def _popen(argv: tuple[str, ...]) -> Any:
    return subprocess.Popen(argv)  # nosec B603 - fixed argv from the declared unit, no shell


class IngestWorkerSupervisor:
    """The host's supervision of one declared worker unit: start, observe, stop.

    The host owns the process it started and nothing else. ``spawn`` is the one
    seam that starts a process (``subprocess.Popen`` in production); ``running``
    reports the live state the health route reads.
    """

    def __init__(
        self,
        unit: IngestWorkerUnit,
        *,
        read_metrics: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
        spawn: Callable[[tuple[str, ...]], Any] = _popen,
    ) -> None:
        self.unit = unit
        self.read_metrics = read_metrics
        self._spawn = spawn
        self._process: Any = None

    def start(self) -> bool:
        """Start the worker once; a failed start is logged, never raised into boot."""
        if self._process is not None:
            return True
        try:
            self._process = self._spawn(self.unit.argv)
        except OSError as exc:
            logger.warning("ingest worker did not start: %s", type(exc).__name__)
            return False
        return True

    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()


def with_ingest_worker_not_configured(
    snapshot: FleetHealthSnapshot, *, now: float | None = None
) -> FleetHealthSnapshot:
    """A fleet that runs no ingest worker reports ``not_configured``, never unhealthy."""
    checked = time.time() if now is None else float(now)
    # No diagnostic line: a fleet that supervises no worker has nothing to report,
    # and the status itself says it. The rollup already ignores ``not_configured``.
    dependency = _dependency("not_configured", checked, None)
    return _with_dependency(snapshot, INGEST_WORKER_DEPENDENCY, dependency)


async def ingest_worker_health_snapshot(
    snapshot: FleetHealthSnapshot, supervisor: IngestWorkerSupervisor | None
) -> FleetHealthSnapshot:
    """The snapshot with the supervised worker's health, or ``not_configured``."""
    if supervisor is None:
        return with_ingest_worker_not_configured(snapshot)
    metrics: Mapping[str, Any] = {}
    unreadable = False
    if supervisor.running() and supervisor.read_metrics is not None:
        try:
            metrics = await supervisor.read_metrics()
        except Exception:
            logging.getLogger(__name__).debug(
                "an unreachable read is a state, not a crash", exc_info=True
            )
            unreadable = True

    def read() -> Mapping[str, Any]:
        if unreadable:
            raise ConnectionError("metrics read failed")
        return metrics

    return with_ingest_worker_health(
        snapshot, running=supervisor.running, read_metrics=read
    )[0]
