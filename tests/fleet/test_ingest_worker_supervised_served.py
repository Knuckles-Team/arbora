"""The host supervises the ingest worker and the health route reports it (ARBORA-FLEET-R045).

``start_fleet_onboarding`` is the boot entry the served process calls. The fleet
health route (``fleet_health``) then reports the supervised worker as healthy,
stopped or unreadable, and a fleet with no worker as ``not_configured``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet import onboarding as onboarding_module
from arbora.gateway import fleet as fleet_api

METRICS = {"jobs_running": 1, "queue_depth": 4}


class _Process:
    def __init__(self) -> None:
        self.exit: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit

    def terminate(self) -> None:
        self.terminated = True


class _Client:
    pass


def _engine() -> Any:
    client = _Client()
    return SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=client)
        )
    )


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch) -> Any:
    answers: dict[str, Any] = {"metrics": METRICS}
    spawned: list[tuple[tuple[str, ...], _Process]] = []

    async def send_by_id(client: Any, method: str, params: dict, graph: str, **_: Any):
        if method == "Metrics":
            if answers["metrics"] is None:
                raise ConnectionError("engine metrics unreachable")
            return answers["metrics"]
        return {}

    monkeypatch.setattr("medulla.generated.send_by_id", send_by_id)
    monkeypatch.setattr("arbora.api.ops._engine.decode", lambda value: value)
    monkeypatch.setattr("arbora.pin_lag.medulla.require_methods", lambda *m: None)
    monkeypatch.setattr(
        "arbora.deployment.data_prep.data_prep_path", lambda: None, raising=False
    )
    monkeypatch.setattr(
        onboarding_module.FleetOnboardingService, "start", lambda self: "thread"
    )

    def spawn(argv: tuple[str, ...]) -> _Process:
        process = _Process()
        spawned.append((argv, process))
        return process

    def start(endpoint: str | None) -> None:
        if endpoint is None:
            monkeypatch.delenv("GRAPH_SERVICE_ENDPOINTS", raising=False)
        else:
            monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", endpoint)
        onboarding_module.start_fleet_onboarding(
            engine=_engine(),
            session=SimpleNamespace(graph="g", tenant="t"),
            multiplexer=SimpleNamespace(),
            virtual_catalog=SimpleNamespace(),
            declarations=SimpleNamespace(),
            action_policies=SimpleNamespace(),
            served_url="",
            spawn_worker=spawn,
        )

    yield SimpleNamespace(start=start, answers=answers, spawned=spawned)
    fleet_api.bind_ingest_worker(None)


async def _route() -> dict[str, Any]:
    response = await fleet_api.fleet_health(SimpleNamespace())
    body = json.loads(response.body)
    return body["evidence"]["dependencies"]["ingest_worker"]


@pytest.mark.spec("ARBORA-FLEET-R045")
async def test_running_worker_reports_healthy_and_the_host_started_it(
    boot: Any,
) -> None:
    boot.start("tcp://engine:7000")
    assert [argv for argv, _ in boot.spawned] == [
        ("ingest-worker", "--engine-endpoint", "tcp://engine:7000")
    ]
    assert (await _route())["status"] == "healthy"


@pytest.mark.spec("ARBORA-FLEET-R045")
async def test_stopped_worker_reports_worker_stopped(boot: Any) -> None:
    boot.start("tcp://engine:7000")
    boot.spawned[0][1].exit = 1
    dependency = await _route()
    assert dependency["status"] == "unavailable"
    assert dependency["diagnostics"] == ["ingest_worker: worker_stopped"]


@pytest.mark.spec("ARBORA-FLEET-R045")
async def test_unreachable_metrics_report_metrics_unavailable(boot: Any) -> None:
    boot.start("tcp://engine:7000")
    boot.answers["metrics"] = None
    dependency = await _route()
    assert dependency["status"] == "unavailable"
    assert dependency["diagnostics"] == ["ingest_worker: metrics_unavailable"]


@pytest.mark.spec("ARBORA-FLEET-R045")
async def test_a_fleet_with_no_worker_reports_not_configured_not_unhealthy(
    boot: Any,
) -> None:
    boot.start(None)
    assert boot.spawned == []
    response = await fleet_api.fleet_health(SimpleNamespace())
    evidence = json.loads(response.body)["evidence"]
    assert evidence["dependencies"]["ingest_worker"]["status"] == "not_configured"
    # Nothing supervising a worker never marks the fleet unready by itself.
    assert not any("ingest_worker" in line for line in evidence["diagnostics"])


@pytest.mark.spec("ARBORA-FLEET-R045")
async def test_the_route_returns_the_engine_worker_gauges_unchanged(boot: Any) -> None:
    """The engine ``Metrics`` reply, ``medulla_ingest_*`` series included, is surfaced as read."""
    gauges = {
        "medulla_ingest_jobs_total": {"outcome=succeeded": 7, "outcome=failed": 1},
        "medulla_ingest_dead_letter_total": {"queue=ingest": 2},
        "medulla_lane_depth": {"lane=ingest,queue_class=bulk": 4},
        "medulla_lane_inflight": {"lane=ingest": 1},
    }
    boot.answers["metrics"] = gauges
    boot.start("tcp://engine:7000")
    dependency = await _route()
    assert dependency["status"] == "healthy"
    assert dependency["metrics"] == gauges
    boot.spawned[0][1].exit = 1
    assert (await _route())["metrics"] is None
