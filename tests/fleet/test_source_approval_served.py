"""ARBORA-FLEET-R042.6: hot-path proof of source approval over a real MCP connector.

A fixture connector serves one read-only and one write operation over MCP. It
is onboarded through the real onboarding pass, and the served read goes through
the executor to the connector: the approved mapping serves rows, the held
mapping serves none and never reaches the connector's write tool.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from arbora.api.registry import Effect
from arbora.fleet.source_executor import FleetSourceExecutor, ForeignReadSourceRequest
from arbora.fleet.virtual_catalog import VirtualCatalog
from tests.fleet.test_source_approval import (
    CONNECTOR,
    READ,
    WRITE,
    _Engine,
    _onboard,
    contracts,  # noqa: F401
)
from tests.fleet.test_source_executor import RecordingForeignClient

TICKETS = [{"id": "t1", "title": "disk full"}, {"id": "t2", "title": "vpn down"}]


def _connector() -> tuple[FastMCP, list[str]]:
    mcp = FastMCP(CONNECTOR)
    called: list[str] = []

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_tickets() -> dict[str, Any]:
        called.append("list_tickets")
        return {"items": TICKETS}

    @mcp.tool(annotations={"destructiveHint": True})
    def delete_ticket(ticket_id: str) -> dict[str, Any]:
        called.append("delete_ticket")
        return {"deleted": ticket_id}

    return mcp, called


class _McpGateway:
    """The governed gateway's ``call`` seam, bridged to the real connector over MCP."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp

    async def call(
        self,
        _caller: Any,
        _server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        expected_effect: Effect,
    ) -> Any:
        assert expected_effect is Effect.READ
        async with Client(self._mcp) as client:
            result = await client.call_tool(tool, arguments)
        return {"value": result.structured_content}


def _request(decision: Any, contract: Any) -> ForeignReadSourceRequest:
    return ForeignReadSourceRequest(
        source_id=contract.source_id,
        connector=CONNECTOR,
        tool=contract.operation,
        source_name=contract.source_id,
        contract=contract,
        approval=decision.receipt.model_dump(mode="json") if decision.receipt else {},
        plan_digest="b" * 64,
        query_digest="a" * 64,
        limits={
            "max_rows": 100,
            "max_bytes": 4096,
            "chunk_rows": 2,
            "chunk_bytes": 1024,
            "deadline_ms": 10_000,
        },
        max_pages=1,
        max_rows=10,
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R042.6")
async def test_onboarded_read_contract_serves_rows_and_held_contract_serves_none(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    request.getfixturevalue("contracts").extend((READ, WRITE))
    catalog = VirtualCatalog()
    import asyncio

    onboarding = await asyncio.to_thread(_onboard, monkeypatch, _Engine(), catalog)
    decisions = {d.contract_iri: d for d in onboarding.source_decisions[CONNECTOR]}
    mappings = {m.mapping_id: m for m in catalog.mappings}
    assert mappings["urn:ac:read"].approved and not mappings["urn:ac:write"].approved

    mcp, called = _connector()
    foreign = RecordingForeignClient(
        pytest.importorskip("scion_sdk.adapters.foreign_read")
    )
    executor = FleetSourceExecutor(
        _McpGateway(mcp), SimpleNamespace(), foreign_client=foreign
    )

    served = await executor.read_approved(
        _request(decisions["urn:ac:read"], READ),
        mapping=mappings["urn:ac:read"],
        resource_uri="ontology://tickets",
    )
    assert served.ok and served.rows == len(TICKETS)
    assert called == ["list_tickets"]

    held = await executor.read_approved(
        _request(decisions["urn:ac:write"], WRITE),
        mapping=mappings["urn:ac:write"],
        resource_uri="ontology://tickets",
    )
    assert held.error_code == "SOURCE_HELD" and held.rows == 0
    assert called == ["list_tickets"]  # the write tool was never reached
