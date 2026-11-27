"""Replica convergence by catalog generation and digest (ARBORA-FLEET-R051.2)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from arbora.api.domain.fleet.catalog_reader import (
    CATALOG_GENERATION_BEHIND,
    CatalogGeneration,
    FleetCatalogReader,
    ServerPage,
    catalog_digest,
    convergence_timeout_s,
)
from arbora.api.domain.fleet.mcp_resource_reconciliation import (
    MCP_RESOURCE_FAMILIES,
    ReconciliationReceipt,
)
from arbora.fleet.health import (
    FleetAuthorities,
    collect_fleet_health,
    health_payload,
    with_catalog_generation,
)
from tests.test_fleet_catalog_reader import (
    SERVER,
    SERVER_COMPONENT,
    TOOL_COMPONENT,
    FakeFleetCatalogPort,
    component_page,
    server_page,
)

NOW_MS = 5_000_000


class EnginePort(FakeFleetCatalogPort):
    """A recorded engine whose registry revision (its catalog generation) can move."""

    def __init__(self, revision: int) -> None:
        super().__init__()
        self.revision = revision

    async def query_registered_servers(
        self, graph: str, limit: int, cursor: str | None, /
    ) -> ServerPage:
        return replace(server_page(SERVER), registry_revision=self.revision)

    async def search_components(self, request):  # type: ignore[no-untyped-def]
        return component_page(SERVER_COMPONENT, TOOL_COMPONENT)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def receipt_for(generation: CatalogGeneration) -> ReconciliationReceipt:
    return ReconciliationReceipt(
        tenant_id=generation.tenant,
        catalog_generation=generation.generation,
        snapshot_digest=generation.digest,
        acknowledged_families=MCP_RESOURCE_FAMILIES,
        issued_at_ms=NOW_MS - 1_000,
    )


def read_and_publish(reader: FleetCatalogReader):  # type: ignore[no-untyped-def]
    candidate = asyncio.run(reader.read())
    assert candidate.generation is not None
    reader.publish_generation(
        candidate, receipt_for(candidate.generation), now_ms=NOW_MS
    )
    return candidate


@pytest.mark.spec("ARBORA-FLEET-R051.2")
def test_lagging_replica_reports_degraded_and_never_mixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARBORA_REPLICA_CONVERGENCE_TIMEOUT_S", raising=False)
    engine = EnginePort(revision=5)
    clock_a, clock_b = Clock(), Clock()
    replica_a = FleetCatalogReader(engine, replica="a", clock=clock_a)
    replica_b = FleetCatalogReader(engine, replica="b", clock=clock_b)

    first_a = read_and_publish(replica_a)
    first_b = read_and_publish(replica_b)
    assert first_a.generation == first_b.generation
    assert first_a.generation is not None and first_a.generation.generation == 5
    assert len(first_a.generation.digest) == 64
    assert asyncio.run(replica_a.generation_status()).state == "converged"
    assert asyncio.run(replica_b.generation_status()).state == "converged"

    engine.revision = 6
    second_a = read_and_publish(replica_a)
    assert second_a.generation is not None and second_a.generation.generation == 6

    assert asyncio.run(replica_b.generation_status()).state == "converging"
    clock_b.now += 31
    lagging = asyncio.run(replica_b.generation_status())
    assert lagging.state == "degraded"
    assert lagging.reason == CATALOG_GENERATION_BEHIND
    assert lagging.engine_generation == 6
    # The degraded replica keeps its last-known-good generation whole.
    assert replica_b.published is first_b
    assert asyncio.run(replica_a.generation_status()).state == "converged"
    assert replica_a.published is second_a

    # Catching up clears the state; a generation is never assembled from two.
    read_and_publish(replica_b)
    assert asyncio.run(replica_b.generation_status()).state == "converged"


@pytest.mark.spec("ARBORA-FLEET-R051.2")
def test_generation_and_digest_are_validated_and_order_independent() -> None:
    with pytest.raises(ValueError):
        CatalogGeneration("t", 1, "not-hex")
    with pytest.raises(ValueError):
        CatalogGeneration("t", -1, "a" * 64)
    with pytest.raises(ValueError):
        CatalogGeneration("", 1, "a" * 64)
    forward = catalog_digest([SERVER_COMPONENT, TOOL_COMPONENT])
    assert forward == catalog_digest([TOOL_COMPONENT, SERVER_COMPONENT])
    assert forward != catalog_digest([SERVER_COMPONENT])


@pytest.mark.spec("ARBORA-FLEET-R051.2")
def test_convergence_timeout_is_a_bounded_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARBORA_REPLICA_CONVERGENCE_TIMEOUT_S", raising=False)
    assert convergence_timeout_s() == 30.0
    monkeypatch.setenv("ARBORA_REPLICA_CONVERGENCE_TIMEOUT_S", "600")
    assert convergence_timeout_s() == 600.0
    for refused in ("0", "0.5", "601"):
        monkeypatch.setenv("ARBORA_REPLICA_CONVERGENCE_TIMEOUT_S", refused)
        with pytest.raises(ValueError, match="between"):
            convergence_timeout_s()


@pytest.mark.spec("ARBORA-FLEET-R051.2")
def test_replica_publishes_its_generation_in_the_health_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARBORA_REPLICA_CONVERGENCE_TIMEOUT_S", raising=False)
    engine = EnginePort(revision=5)
    clock = Clock()
    reader = FleetCatalogReader(engine, replica="a", clock=clock)
    read_and_publish(reader)
    base = collect_fleet_health(FleetAuthorities())

    converged = with_catalog_generation(base, asyncio.run(reader.generation_status()))
    payload = health_payload(converged)
    assert payload["catalog_generation"]["generation"]["generation"] == 5
    assert converged.evidence.ready is True

    engine.revision = 9
    asyncio.run(reader.generation_status())
    clock.now += 31
    behind = with_catalog_generation(base, asyncio.run(reader.generation_status()))
    assert behind.evidence.status == "degraded"
    assert behind.evidence.ready is False
    assert health_payload(behind)["catalog_generation"]["reason"] == (
        CATALOG_GENERATION_BEHIND
    )
