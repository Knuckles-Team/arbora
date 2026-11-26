"""The hosted ingest worker and its health (ARBORA-FLEET-R045)."""

from __future__ import annotations

import pytest

from arbora.fleet.health import FleetAuthorities, collect_fleet_health, health_payload
from arbora.fleet.ingest_worker_unit import (
    INGEST_WORKER_DEPENDENCY,
    declare_ingest_worker_unit,
    with_ingest_worker_health,
)

METRICS = {"jobs_running": 2, "jobs_failed_total": 5, "queue_depth": 11}


def _health(running: bool, metrics):
    base = collect_fleet_health(FleetAuthorities(), now=100.0)
    return with_ingest_worker_health(
        base, running=lambda: running, read_metrics=metrics, now=101.0
    )


@pytest.mark.spec("ARBORA-FLEET-R045")
def test_stopped_worker_is_unhealthy_and_running_worker_exposes_metrics() -> None:
    snapshot, health = _health(True, lambda: METRICS)
    assert health.state == "healthy" and dict(health.metrics) == METRICS
    payload = health_payload(snapshot)
    assert (
        payload["evidence"]["dependencies"][INGEST_WORKER_DEPENDENCY]["status"]
        == "healthy"
    )

    snapshot, stopped = _health(False, lambda: METRICS)
    assert (stopped.state, stopped.reason) == ("unhealthy", "worker_stopped")
    assert stopped.metrics == {}
    assert (
        snapshot.evidence.dependencies[INGEST_WORKER_DEPENDENCY].status == "unavailable"
    )
    assert snapshot.evidence.ready is False


@pytest.mark.spec("ARBORA-FLEET-R045")
def test_unreachable_metrics_report_metrics_unavailable() -> None:
    def broken():
        raise ConnectionError("engine down")

    _snapshot, health = _health(True, broken)
    assert (health.state, health.reason) == ("unhealthy", "metrics_unavailable")


@pytest.mark.spec("ARBORA-FLEET-R045")
def test_unit_runs_the_ingest_worker_against_the_served_engine() -> None:
    unit = declare_ingest_worker_unit(engine_endpoint=" http://engine:9 ")
    assert unit is not None
    assert unit.argv == ("ingest-worker", "--engine-endpoint", "http://engine:9")
    assert declare_ingest_worker_unit(engine_endpoint="x", enabled=False) is None
    with pytest.raises(ValueError):
        declare_ingest_worker_unit(engine_endpoint="  ")
