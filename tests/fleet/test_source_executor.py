"""Source executor over the governed fleet gateway (ARBORA-FLEET-R043.7)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.registry import Effect
from arbora.fleet.source_executor import FleetSourceExecutor, SourceReadRequest


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def call(self, caller, server, tool, arguments, *, expected_effect):
        self.calls.append((caller, server, tool, dict(arguments), expected_effect))
        if server == "denied":
            raise PermissionError("no grant")
        if server == "slow":
            await asyncio.sleep(5)
        if server == "write":
            raise RuntimeError("fleet effect changed before dispatch")
        if server == "bad":
            return {"value": {"oops": 1}}
        page = int(arguments.get("cursor", "0"))
        nxt = {"next_cursor": str(page + 1)} if page < 9 else {}
        return {"value": {"items": [{"n": page * 2}, {"n": page * 2 + 1}], **nxt}}


@pytest.mark.spec("ARBORA-FLEET-R043.7")
async def test_reads_page_through_fleet_call_with_caller_delegation_and_per_source_errors() -> (
    None
):
    gateway, caller = FakeGateway(), object()
    executor = FleetSourceExecutor(gateway, caller)
    out = await executor.read_all(
        [
            SourceReadRequest(
                "a", "github-mcp", "list_issues", {"state": "open"}, max_pages=3
            ),
            SourceReadRequest("b", "gitlab-mcp", "list_issues", max_rows=3),
            SourceReadRequest("c", "denied", "t"),
            SourceReadRequest("d", "slow", "t", timeout_s=0.05),
            SourceReadRequest("e", "write", "t"),
            SourceReadRequest("f", "bad", "t"),
        ]
    )
    a, b, c, d, e, f = out
    # pagination is bounded by pages and by rows
    assert a.ok and len(a.rows) == 6 and a.pages == 3 and a.truncated
    assert b.ok and len(b.rows) == 3 and b.truncated
    # every call carries the caller and a READ effect expectation
    assert all(call[0] is caller and call[4] is Effect.READ for call in gateway.calls)
    assert gateway.calls[0][3] == {"state": "open"} and {
        "state": "open",
        "cursor": "1",
    } in [c[3] for c in gateway.calls]
    # a failing source returns its own error and does not fail the others
    assert (c.error_code, d.error_code, e.error_code, f.error_code) == (
        "SOURCE_NOT_GRANTED",
        "SOURCE_TIMEOUT",
        "SOURCE_UNAVAILABLE",
        "SOURCE_BAD_RESPONSE",
    )
