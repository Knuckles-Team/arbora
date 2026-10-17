"""Public-surface behaviour of the fail-closed fleet health collector."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from graph_os.fleet.health import (
    FleetAuthorities,
    collect_fleet_health,
    health_payload,
    mark_dependency_failure,
    unavailable_fleet_health,
)

pytestmark = pytest.mark.spec("GRAPHOS-FLEET-R024.750")
NOW = 1_000.0


class Store:
    """In-memory control store with the ``dialect`` attribute readers expect."""

    dialect = "sqlite"

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE TABLE sessions (status TEXT, metadata_json TEXT)")
        rows = [("active", '{"domain": "ops"}'), ("failed", '{"domain": "ops"}')]
        self._conn.executemany("INSERT INTO sessions VALUES (?, ?)", rows)

    def cursor(self) -> sqlite3.Cursor:
        return self._conn.cursor()

    def close(self) -> None:
        self._conn.close()


def goals() -> dict[str, Any]:
    return {"active": 1, "tracked": 2}


def broken() -> list[dict[str, Any]]:
    raise ConnectionError("postgres://user:secret@host/db")


SERVED = FleetAuthorities(connect=Store, goals=goals, workers=lambda: [{"id": "w"}])


def test_all_authorities_served_is_ready() -> None:
    snapshot = collect_fleet_health(SERVED, now=NOW)
    assert snapshot.evidence.status == "healthy" and snapshot.evidence.ready
    assert snapshot.sessions == {"total": 2, "by_status": {"active": 1, "failed": 1}}
    assert snapshot.domains == {
        "ops": {"total": 2, "active": 1, "errored": 1, "error_rate": 0.5}
    }
    assert health_payload(snapshot)["evidence"]["schema_version"] == "fleet.health.v1"


@pytest.mark.parametrize(
    ("authorities", "status", "missing"),
    [
        (FleetAuthorities(connect=Store, goals=goals), "partial", "worker_registry"),
        (
            FleetAuthorities(connect=Store, goals=goals, workers=broken),
            "partial",
            "worker_registry",
        ),
        (FleetAuthorities(), "unavailable", "control_store"),
    ],
)
def test_unserved_or_failed_authority_is_never_empty(
    authorities: FleetAuthorities, status: str, missing: str
) -> None:
    snapshot = collect_fleet_health(authorities, now=NOW)
    evidence = snapshot.evidence
    assert (evidence.status, evidence.ready, evidence.autoscaling_ready) == (
        status,
        False,
        False,
    )
    assert evidence.dependencies[missing].status == "unavailable"
    assert snapshot.dispatch_workers is None
    assert all("secret" not in line for line in evidence.diagnostics)


def test_late_failure_and_provider_failure_fail_closed() -> None:
    marked = mark_dependency_failure(
        collect_fleet_health(SERVED, now=NOW), "control_store", "topology", ValueError()
    )
    assert marked.evidence.dependencies["control_store"].status == "partial"
    assert not marked.evidence.convergence_ready
    unavailable = unavailable_fleet_health("autoscaler", now=NOW)
    assert (unavailable.status, unavailable.ready) == ("unavailable", False)
