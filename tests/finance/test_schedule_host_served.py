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
    schedule_node_id,
)
from arbora.fleet.loop_worker import LoopRun
from arbora.mcp_server.served_invocation import (
    _loop_body,
    _with_finance_schedule_host,
    _with_loop_worker,
)

LAST = "2026-03-06T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _engine_declares_the_grant_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arbora.pin_lag.medulla.require_methods", lambda *_n: None)


class Engine:
    """Engine nodes by id; records every send."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.checks: list[dict[str, Any]] = []
        self.grants: set[str] = set()

    def put(self, node: str, **props: Any) -> None:
        self.nodes[node] = props

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        self.sent.append((method, params))
        handler = {
            "GetNodePropertiesBatch": self._get,
            "CompareAndSetNodeFields": self._swap,
            "CheckResourceAccess": self._check,
        }.get(method)
        return handler(params) if handler else {}

    def _get(self, params: dict[str, Any]) -> Any:
        return [
            [n, msgpack.packb(self.nodes[n], use_bin_type=True)]
            for n in params["node_ids"]
            if n in self.nodes
        ]

    def _swap(self, params: dict[str, Any]) -> Any:
        node = self.nodes[params["node_id"]]
        want = msgpack.unpackb(params["conditions_msgpack"], raw=False)
        if any(node.get(k) != v for k, v in want.items()):
            return {"swapped": False}
        node.update(msgpack.unpackb(params["updates_msgpack"], raw=False))
        return {"swapped": True}

    def _check(self, params: dict[str, Any]) -> Any:
        self.checks.append(params)
        return {"allow": params["resource_ref"] in self.grants}


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
    host = EngineFinanceScheduleHost(engine.send, "t1", scheduler="svc")
    record = await host.record("sched-1")
    assert record.entry.owner == "o1" and record.grant == "finance:read"
    assert record.last_run == datetime.fromisoformat(LAST)
    assert await host.record("missing") is None
    foreign = EngineFinanceScheduleHost(engine.send, "t2", scheduler="svc")
    assert await foreign.record("sched-1") is None


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_the_owner_grant_scan_door_and_last_run_swap() -> None:
    engine = Engine()
    schedule(engine)
    host = EngineFinanceScheduleHost(engine.send, "t1", scheduler="svc")
    record = await host.record("sched-1")
    assert not await host.owner_holds(record)
    engine.grants.add("finance-delegation:o1")
    assert await host.owner_holds(record)
    assert engine.checks[-1] == {
        "agent_id": "svc",
        "resource_ref": "finance-delegation:o1",
        "protocol": "knowledge",
        "operation": "finance:read",
    }
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
    host = EngineFinanceScheduleHost(engine.send, "t1", scheduler="svc")
    record = await host.record("sched-1")
    with pytest.raises(OperationRefused) as refused:
        await host.run_job(record, "k")
    assert refused.value.code == "UNAVAILABLE"
    assert [m for m, _ in engine.sent if m == "FinanceMarket"] == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_the_production_composition_binds_the_host_and_runs_the_body() -> None:
    session = lambda: SimpleNamespace(  # noqa: E731
        tenant="t1", actor=SimpleNamespace(actor_id="svc-1")
    )
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
    result = await FinanceLoopBody(
        EngineFinanceScheduleHost(Engine().send, "t1", scheduler="svc")
    ).run(LoopRun("nope", "task", 1, "d"), _beat)
    assert (result.outcome, result.detail) == ("Failed", "SCHEDULE_NOT_FOUND")


async def _beat() -> None:
    return None


async def _sized() -> dict[str, Any]:
    return {"amount_minor_units": 2550, "side": "Buy", "strategy_version": 1}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_dca_due_fills_a_paper_plan_through_the_bound_paper_door() -> None:
    from tests.finance._receipt_engine import ReceiptMachine

    engine = Engine()
    schedule(engine)
    node = schedule_node_id("t1", "sched-1")
    engine.nodes[node].update(action="dca_due", dca_plan_id="p1")
    engine.put(
        "dca-plan:t1:p1",
        plan_id="p1",
        revision=1,
        owner="o1",
        tenant_id="t1",
        symbol="VTI",
        account_id="acct-1",
        amount="25.50",
        currency="USD",
        timezone="America/Chicago",
        recurrence_rule="weekly:mon@09:00",
        mode="paper",
    )
    filled: list[tuple[Any, dict[str, Any], str]] = []

    async def paper(plan: Any, arguments: Any, key: str) -> None:
        filled.append((plan, dict(arguments), key))

    base_send = engine.send
    machine = ReceiptMachine()

    async def send(method: str, params: dict[str, Any]) -> Any:
        routes = {
            "FinanceMarket": lambda *_a: _sized(),
            "CreateNodeIfAbsent": machine.send,
            "FinanceReceiptTransition": machine.send,
        }
        return await routes.get(method, base_send)(method, params)

    host = EngineFinanceScheduleHost(send, "t1", scheduler="svc", paper_order=paper)
    record = await host.record("sched-1")
    await host.run_job(record, "occ-1")
    assert len(filled) == 1
    _plan, arguments, key = filled[0]
    assert arguments == {
        "account_id": "acct-1",
        "symbol": "VTI",
        "side": "buy",
        "qty": 25.5,
        "order_type": "market",
        "mode": "paper",
    }
    assert key == "dca:p1:1:occ-1"
    # A replay of the same occurrence fills nothing more.
    await host.run_job(record, "occ-1")
    assert len(filled) == 1
    # An unbound door is a missed occurrence, never a silent skip.
    unbound = EngineFinanceScheduleHost(send, "t1", scheduler="svc")
    await unbound.run_job(record, "occ-2")
    assert len(filled) == 1


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_the_paper_door_is_the_one_paper_handler_and_refuses_before_binding() -> (
    None
):
    from arbora.finance.paper_door import LatePaperDoor

    door = LatePaperDoor(lambda: SimpleNamespace())
    with pytest.raises(OperationRefused) as refused:
        await door(None, {}, "k")
    assert refused.value.code == "UNAVAILABLE"
    door.bind(SimpleNamespace(registry=SimpleNamespace(get=lambda _op: None)))
    with pytest.raises(RuntimeError):
        door.bind(object())
    with pytest.raises(OperationRefused):
        await door(None, {}, "k")


@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
def test_composition_binds_scheduler_principal_paper_door_and_import_ports() -> None:
    from arbora.finance.import_ports import BlobImportSource, EngineActivitySink
    from arbora.finance.paper_door import LatePaperDoor
    from arbora.mcp_server.served_invocation import _with_finance_import_ports

    session = lambda: SimpleNamespace(  # noqa: E731
        tenant="t1", actor=SimpleNamespace(actor_id="svc-1")
    )
    bound = _with_finance_schedule_host({}, object(), "g", lambda t: t, session)
    assert bound["finance_scheduler_principal"] == "svc-1"
    assert isinstance(bound["finance_paper_door"], LatePaperDoor)
    imports = _with_finance_import_ports(bound)
    assert isinstance(imports["finance_import_source"], BlobImportSource)
    assert isinstance(imports["finance_activity_sink"], EngineActivitySink)
    mine = object()
    assert (
        _with_finance_import_ports({"finance_activity_sink": mine})[
            "finance_activity_sink"
        ]
        is mine
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_a_pin_without_the_grant_check_refuses_and_no_local_grant_stands_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.pin_lag.medulla import PinLagUnavailable

    def lacking(*names: str) -> None:
        raise PinLagUnavailable(names[0])

    monkeypatch.setattr("arbora.pin_lag.medulla.require_methods", lacking)
    engine = Engine()
    schedule(engine)
    host = EngineFinanceScheduleHost(engine.send, "t1", scheduler="svc")
    with pytest.raises(OperationRefused) as refused:
        await host.owner_holds(await host.record("sched-1"))
    assert refused.value.code == "UNAVAILABLE" and engine.checks == []
