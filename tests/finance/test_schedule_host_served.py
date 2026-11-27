"""The production finance schedule host and its composition (ARBORA-DATA-MARKET-R002)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.finance.loop_body import FinanceLoopBody
from arbora.finance.schedule_host import (
    EngineFinanceScheduleHost,
    delegation_node_id,
    schedule_node_id,
)
from arbora.fleet.loop_worker import LoopRun
from arbora.mcp_server.served_invocation import (
    _loop_body,
    _with_finance_schedule_host,
    _with_loop_worker,
)

LAST = "2026-03-06T12:00:00+00:00"


class Engine:
    """Engine nodes by id; records every send."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def put(self, node: str, **props: Any) -> None:
        self.nodes[node] = props

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        self.sent.append((method, params))
        if method == "GetNodePropertiesBatch":
            return [
                [n, msgpack.packb(self.nodes[n], use_bin_type=True)]
                for n in params["node_ids"]
                if n in self.nodes
            ]
        if method == "CompareAndSetNodeFields":
            node = self.nodes[params["node_id"]]
            want = msgpack.unpackb(params["conditions_msgpack"], raw=False)
            if any(node.get(k) != v for k, v in want.items()):
                return {"swapped": False}
            node.update(msgpack.unpackb(params["updates_msgpack"], raw=False))
            return {"swapped": True}
        return {}


def schedule(engine: Engine, tenant: str = "t1", **over: Any) -> None:
    engine.put(
        schedule_node_id(tenant, "sched-1"),
        tenant_id=tenant,
        owner="o1",
        action="scan",
        timezone="America/Chicago",
        trigger="daily@09:00",
        grant="finance:read",
        last_run=LAST,
        request={"series": "x"},
        **over,
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_the_host_reads_the_entry_and_a_foreign_one_is_absent() -> None:
    engine = Engine()
    schedule(engine)
    host = EngineFinanceScheduleHost(engine.send, "t1")
    record = await host.record("sched-1")
    assert record.entry.owner == "o1" and record.grant == "finance:read"
    assert record.last_run == datetime.fromisoformat(LAST)
    assert await host.record("missing") is None
    foreign = EngineFinanceScheduleHost(engine.send, "t2")
    assert await foreign.record("sched-1") is None


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_the_owner_grant_scan_door_and_last_run_swap() -> None:
    engine = Engine()
    schedule(engine)
    host = EngineFinanceScheduleHost(engine.send, "t1")
    record = await host.record("sched-1")
    assert not await host.owner_holds(record)
    engine.put(
        delegation_node_id("t1", "o1", "finance:read"), tenant_id="t1", active=True
    )
    assert await host.owner_holds(record)
    await host.run_job(record, "sched-1:k")
    assert engine.sent[-1] == (
        "FinanceMarket",
        {"op": {"op": "signal_scan", "request": {"series": "x"}}},
    )
    now = datetime(2026, 3, 10, 12, tzinfo=UTC)
    await host.mark_run(record, now)
    assert (
        engine.nodes[schedule_node_id("t1", "sched-1")]["last_run"] == now.isoformat()
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_an_unbound_job_door_refuses_unavailable() -> None:
    engine = Engine()
    schedule(engine, tenant="t1")
    engine.nodes[schedule_node_id("t1", "sched-1")]["action"] = "backfill"
    host = EngineFinanceScheduleHost(engine.send, "t1")
    record = await host.record("sched-1")
    with pytest.raises(OperationRefused) as refused:
        await host.run_job(record, "k")
    assert refused.value.code == "UNAVAILABLE"
    assert [m for m, _ in engine.sent if m == "FinanceMarket"] == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_the_production_composition_binds_the_host_and_runs_the_body() -> None:
    session = lambda: SimpleNamespace(tenant="t1")  # noqa: E731
    bound = _with_finance_schedule_host({}, object(), "g", lambda t: t, session)
    host = bound["finance_schedule_host"]
    assert isinstance(host, EngineFinanceScheduleHost)
    body = _loop_body(bound)
    assert isinstance(body, FinanceLoopBody)
    with_worker, startup = _with_loop_worker(bound, object(), "g", lambda t: t, session)
    assert startup is not None and "loop_worker_runner" in with_worker
    # A host the deployment binds itself is kept.
    mine = object()
    assert (
        _with_finance_schedule_host(
            {"finance_schedule_host": mine}, object(), "g", lambda t: t, session
        )["finance_schedule_host"]
        is mine
    )
    # The composed body, over the same engine reads, reports an unknown schedule.
    result = await FinanceLoopBody(EngineFinanceScheduleHost(Engine().send, "t1")).run(
        LoopRun("nope", "task", 1, "d"), _beat
    )
    assert (result.outcome, result.detail) == ("Failed", "SCHEDULE_NOT_FOUND")


async def _beat() -> None:
    return None
