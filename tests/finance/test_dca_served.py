"""DCA plans: one proposal per occurrence, engine sizing, missed reported (ARBORA-DATA-MARKET-R006)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.domain.finance.dca import (
    DCAEvaluationError,
    DCAHost,
    DCAPlan,
    evaluate_dca,
    minor_units,
    run_dca_due,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import dca as ops
from arbora.api.ops.registry_factory import get_registry
from tests.finance._receipt_engine import ReceiptMachine
from tests.mcp_server._read_port_double import FakeReadPort

NO_OP: Any = None


def plan(**over: Any) -> DCAPlan:
    base: dict[str, Any] = {
        "plan_id": "p1",
        "revision": 3,
        "owner": "o1",
        "tenant_id": "t1",
        "symbol": "VTI",
        "account_id": "acct-1",
        "amount": Decimal("25.50"),
        "currency": "USD",
        "timezone": "America/Chicago",
        "recurrence_rule": "weekly:mon@09:00",
        "mode": "paper",
    }
    return DCAPlan.model_validate({**base, **over})


class Host(DCAHost):
    def __init__(self) -> None:
        self.paper: list[Any] = []
        self.missed_events: list[tuple[str, str]] = []
        self.fail_paper = False

    async def submit_paper(self, plan: DCAPlan, proposal: Any) -> None:
        if self.fail_paper:
            raise ConnectionError("paper door down")
        self.paper.append(proposal)

    async def missed(self, plan: DCAPlan, occurrence: str, reason: str) -> None:
        self.missed_events.append((occurrence, reason))


class Evaluator:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        assert method == "FinanceMarket"
        self.sent.append(params)
        spec = params["op"]["spec"]["kind"]["DollarCostAverageFixedAmount"]
        return {
            "strategy_version": 1,
            "side": "Buy",
            "amount_minor_units": spec["amount_minor_units"],
        }


@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
def test_minor_units_convert_by_currency_and_refuse_fractions() -> None:
    assert minor_units(Decimal("25.50"), "USD") == 2550
    assert minor_units(Decimal("500"), "JPY") == 500
    assert minor_units(Decimal("1.234"), "KWD") == 1234
    with pytest.raises(DCAEvaluationError):
        minor_units(Decimal("1.005"), "USD")


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_the_evaluator_is_the_engines() -> None:
    evaluator = Evaluator()
    answer = await evaluate_dca(evaluator.send, plan())
    assert answer["amount_minor_units"] == 2550
    assert evaluator.sent == [
        {
            "op": {
                "op": "strategy_evaluate",
                "spec": {
                    "version": 1,
                    "kind": {
                        "DollarCostAverageFixedAmount": {"amount_minor_units": 2550}
                    },
                },
            }
        }
    ]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_replay_and_clock_drift_yield_one_proposal_and_missed_is_reported() -> (
    None
):
    machine = ReceiptMachine()
    evaluator = Evaluator()
    host = Host()
    engine = machine.engine()
    occurrence = "p1:2026-03-09T09:00[America/Chicago]"
    outcomes = [
        await run_dca_due(engine, evaluator.send, host, plan(), occurrence)
        for _ in range(3)
    ]
    assert outcomes == ["proposed", "duplicate", "duplicate"]
    assert len(host.paper) == 1 and len(evaluator.sent) == 1
    # A paper door failure is a missed occurrence, reported once and never retried blind.
    host.fail_paper = True
    later = "p1:2026-03-16T09:00[America/Chicago]"
    assert await run_dca_due(engine, evaluator.send, host, plan(), later) == "missed"
    assert await run_dca_due(engine, evaluator.send, host, plan(), later) == "duplicate"
    assert host.missed_events == [(later, "ConnectionError")]
    # A live plan stays a proposal: no paper submission.
    host.fail_paper = False
    live = plan(mode="live", plan_id="p2")
    await run_dca_due(engine, evaluator.send, host, live, occurrence)
    assert len(host.paper) == 1
    # A paused plan does nothing.
    assert (
        await run_dca_due(engine, evaluator.send, host, plan(paused=True), "x")
        == "paused"
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_a_revised_plan_never_collides_with_earlier_proposals() -> None:
    machine = ReceiptMachine()
    evaluator, host = Evaluator(), Host()
    occurrence = "p1:2026-03-09T09:00[America/Chicago]"
    one = await run_dca_due(
        machine.engine(), evaluator.send, host, plan(revision=3), occurrence
    )
    two = await run_dca_due(
        machine.engine(), evaluator.send, host, plan(revision=4), occurrence
    )
    assert (one, two) == ("proposed", "proposed")


class PlanEngine:
    """Plan nodes with a compare-and-set, as the engine holds them."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}

    async def send(
        self, _c: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        if method == "GetNodePropertiesBatch":
            return [[n, self.nodes.get(n)] for n in params["node_ids"]]
        if method == "CreateNodeIfAbsent":
            self.nodes.setdefault(
                params["node_id"], msgpack.unpackb(params["properties_msgpack"])
            )
            return True
        assert method == "CompareAndSetNodeFields"
        node = self.nodes[params["node_id"]]
        for key, value in msgpack.unpackb(params["conditions_msgpack"]).items():
            if node.get(key) != value:
                return False
        node.update(msgpack.unpackb(params["updates_msgpack"]))
        return True


def ctx(tenant: str = "tenant:one") -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant=tenant, principal="p"),
        services={"finance_read_port": FakeReadPort({tenant: "acct-1"})},
        idempotency_key="k",
    )


SET: dict[str, Any] = {
    "plan_id": "p1",
    "symbol": "VTI",
    "account": "acct-1",
    "amount": "25.50",
    "currency": "USD",
    "timezone": "America/Chicago",
    "recurrence": "weekly:mon@09:00",
    "bounds": {},
    "mode": "paper",
    "expected_revision": 0,
}


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> PlanEngine:
    fake = PlanEngine()
    monkeypatch.setattr(ops, "engine_send", fake.send)
    return fake


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_plans_set_and_pause_swap_by_revision(engine: PlanEngine) -> None:
    first = await ops.handle_dca_plans_set(ctx(), SET, NO_OP)
    assert first["value"] == {"plan_id": "p1", "revision": 1, "paused": False}
    with pytest.raises(OperationRefused) as stale:
        await ops.handle_dca_plans_set(ctx(), SET, NO_OP)  # expected 0, stored 1
    assert stale.value.code == "CONFLICT"
    paused = await ops.handle_dca_plans_pause(
        ctx(), {"plan_id": "p1", "paused": True, "expected_revision": 1}, NO_OP
    )
    assert paused["value"]["revision"] == 2 and paused["value"]["paused"] is True
    stored = engine.nodes[ops.plan_node_id("tenant:one", "p1")]
    assert stored["paused"] is True and stored["revision"] == 2
    with pytest.raises(OperationRefused) as foreign:
        await ops.handle_dca_plans_pause(
            ctx("tenant:two"),
            {"plan_id": "p1", "paused": False, "expected_revision": 2},
            NO_OP,
        )
    assert foreign.value.code == "NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_plans_set_refuses_a_bad_plan_or_another_account(
    engine: PlanEngine,
) -> None:
    for bad in (
        {"recurrence": "hourly"},
        {"timezone": "Nowhere/Zone"},
        {"amount": "1.005"},
    ):
        with pytest.raises(OperationRefused) as refused:
            await ops.handle_dca_plans_set(ctx(), {**SET, **bad}, NO_OP)
        assert refused.value.code == "INVALID_ARGUMENT"
    with pytest.raises(OperationRefused) as other:
        await ops.handle_dca_plans_set(ctx(), {**SET, "account": "acct-2"}, NO_OP)
    assert other.value.code == "POLICY_DENIED"
    assert engine.nodes == {}


@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
def test_the_plan_operations_are_served_as_finance_track_writes() -> None:
    for op_id in ("dca.plans.set", "dca.plans.pause"):
        spec = get_registry().get(op_id)
        assert spec is not None
        assert spec.scopes == frozenset({"finance:track"})
        assert op_id in ops.NATIVE_IDEMPOTENCY
