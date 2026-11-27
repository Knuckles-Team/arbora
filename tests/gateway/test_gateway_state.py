"""The gateway field of the host context (ARBORA-HOST-R039.7).

Every owned run-time object of the REST gateway is a field of ``GatewayState``,
built once by host composition. Importing the gateway creates no router, binds no
reader and holds no counter.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from arbora.gateway.state import GatewayState, Slot, gateway_of

#: Names that held gateway state at module level before the gateway field existed.
DELETED = {
    "arbora.gateway.remote_oauth_api": (
        "_BROKER",
        "_SCOPE_POLICY",
        "remote_oauth_router",
    ),
    "arbora.gateway.dashboard_api": ("_aggregator", "dashboard_router"),
    "arbora.gateway.enhanced_catalog_api": ("_reader",),
    "arbora.gateway.fleet": (
        "_AUTHORITIES",
        "_CATALOG_GENERATION",
        "_INGEST_WORKER",
        "_CATALOG_STATE",
    ),
    "arbora.gateway.fleet_events": (
        "_rate_counters",
        "_rate_lock",
        "_rate_privacy_key",
    ),
    "arbora.gateway.daemon": ("_metrics_lock", "_metrics_started"),
    "arbora.gateway.ports": ("_application",),
    "arbora.gateway.ontology_api": ("ontology_router",),
    "arbora.gateway.registry_api": ("registry_router",),
}

_PROBE = """
import importlib, json, pkgutil
import arbora.gateway as package
from fastapi import APIRouter
from arbora.gateway.state import EventRates, Flag, GatewayState, Slot

held = {}
for info in pkgutil.iter_modules(package.__path__, "arbora.gateway."):
    module = importlib.import_module(info.name)
    found = [
        name
        for name, value in vars(module).items()
        if isinstance(value, (APIRouter, GatewayState, Slot, Flag, EventRates))
    ]
    if found:
        held[info.name] = found
deleted = {}
for name, names in json.loads(%r).items():
    module = importlib.import_module(name)
    deleted[name] = [n for n in names if hasattr(module, n)]
print(json.dumps({"held": held, "deleted": {k: v for k, v in deleted.items() if v}}))
"""


@pytest.mark.spec("ARBORA-HOST-R039.7")
def test_importing_the_gateway_creates_no_router_or_registry() -> None:
    probe = _PROBE % json.dumps({k: list(v) for k, v in DELETED.items()})
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report == {"held": {}, "deleted": {}}


@pytest.mark.spec("ARBORA-HOST-R039.7")
def test_two_hosts_hold_separate_gateway_state() -> None:
    one, other = GatewayState(), GatewayState()
    one.aggregator.set("one")
    one.ingest_worker.set("worker")
    assert other.aggregator.get() is None and other.ingest_worker.get() is None
    assert one.event_rates is not other.event_rates
    with pytest.raises(RuntimeError, match="not configured"):
        other.application()


@pytest.mark.spec("ARBORA-HOST-R039.7")
def test_a_slot_builds_once_and_an_unbound_flag_runs_until_it_succeeds() -> None:
    slot: Slot[list[int]] = Slot()
    built: list[int] = []
    first = slot.get_or_build(lambda: built.append(1) or [1])
    assert slot.get_or_build(lambda: built.append(2) or [2]) is first and built == [1]
    gateway = GatewayState()
    attempts = iter([False, True])
    assert gateway.metrics_listener.run_once(lambda: next(attempts)) is False
    assert gateway.metrics_listener.run_once(lambda: next(attempts)) is True
    assert gateway.metrics_listener.run_once(lambda: False) is True


@pytest.mark.spec("ARBORA-HOST-R039.7")
def test_event_rates_cap_each_source_per_window_and_hide_the_source() -> None:
    from arbora.gateway.state import RATE_CAP_PER_MINUTE

    rates = GatewayState().event_rates
    assert rates.capped("alertmanager", RATE_CAP_PER_MINUTE) is False
    assert rates.capped("alertmanager") is True
    assert rates.capped("another-source") is False
    assert "alertmanager" not in rates.source_ref("alertmanager")


@pytest.mark.spec("ARBORA-HOST-R039.7")
def test_a_route_reads_the_gateway_of_its_own_applications_host() -> None:
    gateway = GatewayState()
    app = SimpleNamespace(state=SimpleNamespace(host=SimpleNamespace(gateway=gateway)))
    assert gateway_of(SimpleNamespace(app=app)) is gateway


def _sql_schema_request(gateway: GatewayState) -> SimpleNamespace:
    async def body() -> dict[str, str]:
        return {"schema": "main"}

    host = SimpleNamespace(gateway=gateway)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(host=host)), json=body
    )


@pytest.mark.asyncio
async def test_sql_schema_route_reads_its_handler_from_the_request_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.gateway import graph_api

    monkeypatch.setattr("arbora.security.session.resolve_session", lambda **_: object())
    gateway = GatewayState()
    unbound = await graph_api._sql_schema_endpoint(_sql_schema_request(gateway))
    assert unbound.status_code == 503

    seen: list[str | None] = []

    async def handler(schema: str | None) -> tuple[int, dict[str, object]]:
        seen.append(schema)
        return 200, {"catalogs": []}

    gateway.sql_schema.set(handler)
    bound = await graph_api._sql_schema_endpoint(_sql_schema_request(gateway))
    assert bound.status_code == 200
    assert seen == ["main"]
