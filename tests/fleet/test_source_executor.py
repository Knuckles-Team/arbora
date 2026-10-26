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


class CappedGateway:
    def __init__(self, *, delay: float = 0.0, rows_per_page: int = 1) -> None:
        self.delay = delay
        self.rows_per_page = rows_per_page
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def call(self, _caller, _server, _tool, arguments, *, expected_effect):
        assert expected_effect is Effect.READ
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            page = int(arguments.get("cursor", "0"))
            return {
                "value": {
                    "items": [
                        {"n": page * self.rows_per_page + i}
                        for i in range(self.rows_per_page)
                    ],
                    "next_cursor": str(page + 1),
                }
            }
        finally:
            self.active -= 1


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


@pytest.mark.spec("ARBORA-FLEET-R043.7")
async def test_source_and_row_caps_are_enforced() -> None:
    gateway = CappedGateway()
    executor = FleetSourceExecutor(gateway, object())

    result = await executor.read(
        SourceReadRequest("pages", "server", "read", max_pages=99, max_rows=99_999)
    )
    assert result.ok and result.pages == 20 and len(result.rows) == 20
    assert gateway.calls == 20

    gateway = CappedGateway(rows_per_page=2_000)
    result = await FleetSourceExecutor(gateway, object()).read(
        SourceReadRequest("rows", "server", "read", max_pages=99, max_rows=99_999)
    )
    assert (
        result.ok
        and result.pages == 5
        and len(result.rows) == 10_000
        and result.truncated
    )
    assert gateway.calls == 5


@pytest.mark.spec("ARBORA-FLEET-R043.7")
async def test_query_source_count_concurrency_and_deadline_caps_are_enforced() -> None:
    requests = [
        SourceReadRequest(str(i), "server", "read", max_pages=1) for i in range(32)
    ]
    gateway = CappedGateway(delay=0.001)
    executor = FleetSourceExecutor(gateway, object(), overall_timeout_s=1.0)
    results = await executor.read_all(requests)
    assert len(results) == 32 and all(result.ok for result in results)
    assert gateway.calls == 32 and gateway.max_active <= 8

    with pytest.raises(ValueError, match="too many sources"):
        await executor.read_all(
            requests + [SourceReadRequest("too-many", "server", "read")]
        )

    slow = CappedGateway(delay=0.1)
    results = await FleetSourceExecutor(
        slow, object(), overall_timeout_s=0.001
    ).read_all([SourceReadRequest(str(i), "server", "read") for i in range(8)])
    assert len(results) == 8
    assert all(result.error_code == "SOURCE_TIMEOUT" for result in results)
    await asyncio.sleep(0)
    assert slow.active == 0
