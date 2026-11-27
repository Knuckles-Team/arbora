"""A scheduled paper fill: a priced quantity and the delegated ownership check (ARBORA-DATA-MARKET-R006)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.domain.finance.delegation import DelegatedSubjectAccess, delegated
from arbora.api.invoke.bootstrap import deny_subject_access
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import finance
from arbora.api.ops.registry_factory import get_registry
from arbora.finance.dca import (
    DCAEvaluationError,
    DCAPlan,
    quantity_for_notional,
)
from arbora.finance.paper_door import LatePaperDoor
from arbora.finance.schedule_host import EngineFinanceScheduleHost, schedule_node_id
from tests.finance._receipt_engine import ReceiptMachine
from tests.finance.test_schedule_host_served import (  # noqa: F401
    Engine,
    _engine_declares_the_grant_check,
    schedule,
)

NOW = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
SERVICE = SimpleNamespace(
    tenant="t1", principal="svc-1", credential_kind="service", request_id="r1"
)
HUMAN = SimpleNamespace(
    tenant="t1", principal="owner-1", credential_kind="session", request_id="r2"
)


class Ledger:
    """The engine's grant ledger: allows one (agent, resource, grant) triple."""

    def __init__(self, *allowed: tuple[str, str, str]) -> None:
        self.allowed = set(allowed)
        self.asked: list[dict[str, Any]] = []

    async def wire(self, method: str, params: dict[str, Any]) -> Any:
        assert method == "CheckResourceAccess"
        self.asked.append(params)
        triple = (params["agent_id"], params["resource_ref"], params["operation"])
        return {"allow": triple in self.allowed}


def granted() -> Ledger:
    return Ledger(("svc-1", "finance-delegation:o1", "grant-1"))


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_a_delegated_service_fill_is_admitted_and_an_undelegated_one_refused() -> (
    None
):
    ledger = granted()
    check = DelegatedSubjectAccess(ledger.wire, deny_subject_access)
    # Undelegated: no assertion, so the default refusal answers; the ledger is not asked.
    assert await check(None, SERVICE, "acct-1") is False
    assert ledger.asked == []
    with delegated("o1", "grant-1", "acct-1"):
        assert await check(None, SERVICE, "acct-1") is True
        # Another account, another owner's grant and a human caller get nothing from it.
        assert await check(None, SERVICE, "acct-2") is False
        assert await check(None, HUMAN, "acct-1") is False
    with delegated("o2", "grant-1", "acct-1"):
        assert await check(None, SERVICE, "acct-1") is False
    # The assertion ended with the fill.
    assert await check(None, SERVICE, "acct-1") is False


def _services(check: Any) -> Any:
    registry = get_registry()
    op = registry.get("orders.paper_submit")

    class Runtime:
        service_scopes = frozenset(op.executor_scopes)
        bindings: dict[str, Any] = {}

        @asynccontextmanager
        async def as_service(self, tenant: str, *, required_scopes: Any) -> Any:
            yield object()

        async def check_subject_access(self, caller: Any, subject: str) -> bool:
            return await check(None, caller, subject)

    return SimpleNamespace(registry=registry, runtime=Runtime())


def _venue(monkeypatch: pytest.MonkeyPatch, price: str = "250.00") -> list[Any]:
    calls: list[Any] = []

    async def fleet(_context: Any, op_id: str, arguments: Any, **_k: Any) -> Any:
        calls.append((arguments["tool"], dict(arguments["arguments"])))
        if arguments["tool"] == "emerald_market_data":
            return {"value": {"symbol": "VTI", "last": float(price)}}
        return {"value": {"status": "FILLED", "order_id": "o1"}}

    monkeypatch.setattr(finance, "invoke_registered", fleet)
    return calls


def plan() -> DCAPlan:
    return DCAPlan.model_validate(
        {
            "plan_id": "p1",
            "revision": 1,
            "owner": "o1",
            "tenant_id": "t1",
            "symbol": "VTI",
            "account_id": "acct-1",
            "amount": Decimal("25.50"),
            "currency": "USD",
            "timezone": "America/Chicago",
            "recurrence_rule": "weekly:mon@09:00",
        }
    )


ORDER = {
    "account_id": "acct-1",
    "symbol": "VTI",
    "side": "buy",
    "notional": "25.50",
    "order_type": "market",
    "mode": "paper",
}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_the_door_prices_the_notional_into_a_quantity_and_names_the_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _venue(monkeypatch)
    ledger = granted()
    door = LatePaperDoor(lambda: SERVICE, clock=lambda: NOW)
    door.bind(_services(DelegatedSubjectAccess(ledger.wire, deny_subject_access)))
    with delegated("o1", "grant-1", "acct-1"):
        answer = await door(plan(), ORDER, "dca:k")
    (quote_tool, quote_args), (order_tool, order_args) = calls
    assert (quote_tool, quote_args) == (
        "emerald_market_data",
        {"action": "quote", "symbol": "VTI"},
    )
    assert order_tool == "emerald_orders"
    # 25.50 / 250.00 = 0.102 units: the notional is never the quantity.
    assert order_args["qty"] == 0.102 and order_args["qty"] != 25.5
    assert "notional" not in order_args
    assert answer["price_reference"] == {
        "source": "emerald-exchange:emerald_market_data.quote",
        "symbol": "VTI",
        "price": "250.0",
        "read_at": NOW.isoformat(),
    }


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_an_undelegated_service_fill_is_refused_before_any_venue_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _venue(monkeypatch)
    door = LatePaperDoor(lambda: SERVICE, clock=lambda: NOW)
    door.bind(_services(DelegatedSubjectAccess(granted().wire, deny_subject_access)))
    with pytest.raises(OperationRefused) as refused:
        await door(plan(), ORDER, "dca:k")
    assert refused.value.code == "SUBJECT_ACCESS_DENIED"
    with delegated("o1", "grant-other", "acct-1"):
        with pytest.raises(OperationRefused) as refused:
            await door(plan(), ORDER, "dca:k")
    assert refused.value.code == "SUBJECT_ACCESS_DENIED"
    assert calls == []


@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
def test_quantity_is_exact_decimal_rounded_down_and_never_the_notional() -> None:
    assert quantity_for_notional(Decimal("100"), Decimal("3")) == Decimal("33.33333333")
    assert quantity_for_notional(Decimal("25.50"), Decimal("250")) == Decimal("0.102")
    for notional, price in (
        (Decimal("1"), Decimal("0")),
        (Decimal("1E-9"), Decimal("1")),
    ):
        with pytest.raises(DCAEvaluationError):
            quantity_for_notional(notional, price)


class AnnotatableReceipts(ReceiptMachine):
    """The receipt machine, taking the engine's compare-and-set on a claimed receipt."""

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        if method != "CompareAndSetNodeFields":
            return await super().send(method, params)
        node = self.nodes[params["node_id"]]
        held = msgpack.unpackb(params["conditions_msgpack"], raw=False)
        assert all(node[k] == v for k, v in held.items())
        node.update(msgpack.unpackb(params["updates_msgpack"], raw=False))
        return True


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R006")
async def test_the_price_reference_is_recorded_on_the_occurrence_receipt() -> None:
    engine = Engine()
    schedule(engine)
    engine.nodes[schedule_node_id("t1", "sched-1")].update(
        action="dca_due", dca_plan_id="p1"
    )
    engine.put(
        "dca-plan:t1:p1",
        **{k: (str(v) if k == "amount" else v) for k, v in plan().model_dump().items()},
    )
    machine = AnnotatableReceipts()
    base = engine.send
    seen: list[tuple[str, str]] = []

    async def paper(plan_: Any, arguments: Any, key: str) -> Any:
        from arbora.api.domain.finance.delegation import _HELD

        held = _HELD.get()
        seen.append((held.owner, held.account))
        return {
            "value": {},
            "price_reference": {"source": "venue", "price": "250.0"},
        }

    async def send(method: str, params: dict[str, Any]) -> Any:
        if method == "FinanceMarket":
            return {"amount_minor_units": 2550, "side": "Buy", "strategy_version": 1}
        if method in ("CreateNodeIfAbsent", "FinanceReceiptTransition"):
            return await machine.send(method, params)
        if method == "CompareAndSetNodeFields" and params["node_id"] in machine.nodes:
            return await machine.send(method, params)
        return await base(method, params)

    host = EngineFinanceScheduleHost(send, "t1", scheduler="svc", paper_order=paper)
    await host.run_job(await host.record("sched-1"), "occ-1")
    assert seen == [("o1", "acct-1")]
    (receipt,) = machine.nodes.values()
    assert receipt["priceReference"] == {"source": "venue", "price": "250.0"}
