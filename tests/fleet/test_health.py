"""Public-surface behaviour of the fail-closed fleet health collector."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from arbora.fleet.health import (
    AuthorityNotServed,
    FleetAuthorities,
    collect_fleet_health,
    health_payload,
    mark_dependency_failure,
    unavailable_fleet_health,
)

pytestmark = pytest.mark.spec("ARBORA-FLEET-R024.750")
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


def test_failed_authority_is_never_empty() -> None:
    snapshot = collect_fleet_health(
        FleetAuthorities(connect=Store, goals=goals, workers=broken), now=NOW
    )
    evidence = snapshot.evidence
    assert (evidence.status, evidence.ready, evidence.autoscaling_ready) == (
        "partial",
        False,
        False,
    )
    assert evidence.dependencies["worker_registry"].status == "unavailable"
    assert snapshot.dispatch_workers is None
    assert all("secret" not in line for line in evidence.diagnostics)


@pytest.mark.parametrize(
    ("authorities", "status", "unserved"),
    [
        (
            FleetAuthorities(connect=Store, goals=goals),
            "healthy",
            ("worker_registry",),
        ),
        (
            FleetAuthorities(),
            "not_served",
            ("goal_rehydration", "control_store", "worker_registry"),
        ),
    ],
)
def test_unserved_authority_is_not_applicable_to_readiness(
    authorities: FleetAuthorities, status: str, unserved: tuple[str, ...]
) -> None:
    """AuthorityNotServed reports not_served and never gates readiness."""
    evidence = collect_fleet_health(authorities, now=NOW).evidence
    assert (evidence.status, evidence.ready) == (status, True)
    # Autoscaling and convergence still need every authority served.
    assert not evidence.autoscaling_ready and not evidence.convergence_ready
    assert all(evidence.dependencies[name].status == "not_served" for name in unserved)


def test_reader_raising_authority_not_served_is_not_served() -> None:
    def unserved() -> Any:
        raise AuthorityNotServed("session store is not served in this process")

    evidence = collect_fleet_health(
        FleetAuthorities(connect=unserved, goals=goals, workers=lambda: []), now=NOW
    ).evidence
    assert evidence.dependencies["control_store"].status == "not_served"
    assert (evidence.status, evidence.ready) == ("healthy", True)


def test_late_failure_and_provider_failure_fail_closed() -> None:
    marked = mark_dependency_failure(
        collect_fleet_health(SERVED, now=NOW), "control_store", "topology", ValueError()
    )
    assert marked.evidence.dependencies["control_store"].status == "partial"
    assert not marked.evidence.convergence_ready
    unavailable = unavailable_fleet_health("autoscaler", now=NOW)
    assert (unavailable.status, unavailable.ready) == ("unavailable", False)


def test_readiness_body_names_unserved_authorities() -> None:
    from arbora.observability.runtime_health import not_served_authorities

    evidence = collect_fleet_health(FleetAuthorities(), now=NOW).evidence
    report = {
        "checks": [
            {"name": "engine", "status": "ok"},
            {
                "name": "fleet_supervision",
                "status": "ok",
                "detail": evidence.model_dump(mode="json"),
            },
        ]
    }
    assert not_served_authorities(report) == [
        "control_store",
        "goal_rehydration",
        "worker_registry",
    ]
    assert not_served_authorities({"checks": []}) == []


def test_readiness_body_names_failing_checks() -> None:
    from arbora.observability.runtime_health import failing_checks

    report = {
        "checks": [
            {"name": "engine", "status": "unhealthy", "reason": "secret detail"},
            {"name": "messaging", "status": "not_configured"},
            {"name": "source_provenance", "status": "unhealthy"},
        ]
    }
    assert failing_checks(report) == ["engine"]
