"""The production catalog refresh fetches the engine's reconciliation receipt.

ARBORA-FLEET-R051.2 and ARBORA-MCP-RESOURCES-R003.2: the refresh publishes a
generation only through the receipt guard, the receipt comes from the engine,
and the health route carries the replica's generation.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from arbora.fleet.catalog_reader import FleetCatalogReader
from arbora.fleet.mcp_resource_reconciliation import (
    MCP_RESOURCE_FAMILIES,
    PublicationRefusedError,
    receipt_from_engine,
)
from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.state import FleetState
from arbora.gateway import fleet as fleet_api
from arbora.pin_lag.unavailable import PinLagUnavailable
from tests.fleet.test_catalog_convergence import EnginePort

NOW_S = 5_000.0


class ReceiptPort(EnginePort):
    """A recorded engine that serves the reconciliation receipt for its generation."""

    def __init__(self, revision: int, *, reconciled: bool = True) -> None:
        super().__init__(revision)
        self.reconciled = reconciled
        self.receipt_calls = 0
        self.digest = ""

    async def reconciliation_receipt(self):  # type: ignore[no-untyped-def]
        self.receipt_calls += 1
        if not self.reconciled:
            return receipt_from_engine(None)
        return receipt_from_engine(
            SimpleNamespace(
                tenant_id=self.context.tenant_id,
                catalog_generation=self.revision,
                snapshot_digest=self.digest,
                acknowledged_families=sorted(MCP_RESOURCE_FAMILIES),
                issued_at_ms=int(NOW_S * 1000) - 1_000,
            )
        )


@pytest.fixture(autouse=True)
def _controlled_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW_S)


def _mux(port: EnginePort) -> tuple[MCPMultiplexer, FleetCatalogReader]:
    reader = FleetCatalogReader(port, replica="a", clock=lambda: 100.0)
    return MCPMultiplexer(reader, fleet=FleetState()), reader


@pytest.mark.spec("ARBORA-FLEET-R051.2")
@pytest.mark.spec("ARBORA-MCP-RESOURCES-R003.2")
def test_refresh_publishes_only_with_the_engine_receipt() -> None:
    port = ReceiptPort(revision=5)
    mux, reader = _mux(port)
    candidate = asyncio.run(reader.read())
    assert candidate.generation is not None
    port.digest = candidate.generation.digest

    asyncio.run(mux.refresh_engine_catalog())

    assert port.receipt_calls == 1
    assert reader.published is not None
    assert reader.published.generation == candidate.generation


@pytest.mark.spec("ARBORA-FLEET-R051.2")
@pytest.mark.spec("ARBORA-MCP-RESOURCES-R003.2")
def test_unreconciled_refresh_keeps_the_last_known_good_generation() -> None:
    port = ReceiptPort(revision=5)
    mux, reader = _mux(port)
    first = asyncio.run(reader.read())
    assert first.generation is not None
    port.digest = first.generation.digest
    asyncio.run(mux.refresh_engine_catalog())
    published = reader.published

    port.revision = 6
    port.reconciled = False
    with pytest.raises(PublicationRefusedError) as refused:
        asyncio.run(mux.refresh_engine_catalog())

    assert refused.value.result.reason == "missing"
    assert reader.published is published


@pytest.mark.spec("ARBORA-FLEET-R051.2")
def test_pinned_engine_without_the_receipt_method_refuses_unavailable() -> None:
    port = EnginePort(revision=5)  # no reconciliation_receipt: the pinned engine
    mux, reader = _mux(port)

    with pytest.raises(PinLagUnavailable) as lacking:
        asyncio.run(reader.fetch_receipt())
    assert "catalog_reconciliation_receipt" in lacking.value.missing

    asyncio.run(mux.refresh_engine_catalog())  # serves, but publishes nothing
    assert reader.published is None


@pytest.mark.spec("ARBORA-FLEET-R051.2")
def test_health_route_carries_the_replica_generation() -> None:
    port = ReceiptPort(revision=5)
    mux, reader = _mux(port)
    candidate = asyncio.run(reader.read())
    assert candidate.generation is not None
    port.digest = candidate.generation.digest
    asyncio.run(mux.refresh_engine_catalog())

    fleet_api.bind_catalog_generation(reader.generation_status)
    try:
        snapshot = asyncio.run(fleet_api._health_snapshot())
    finally:
        fleet_api.bind_catalog_generation(None)

    from arbora.fleet.health import health_payload

    generation = health_payload(snapshot)["catalog_generation"]
    assert generation["state"] == "converged"
    assert generation["generation"]["generation"] == 5
