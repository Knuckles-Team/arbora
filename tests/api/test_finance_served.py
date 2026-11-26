"""Position and paper-trading actions check subject ownership (ARBORA-OPS-R017.2)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.executor import prepare_executor
from arbora.api.invoke.pipeline import EXTERNAL_EFFECT_OPERATIONS
from arbora.api.ops import finance
from arbora.api.registry import Effect, Executor, PrincipalRule, SubjectSource

OPS = {op.id: op for op in finance.operations()}
PAPER = {"account_id": "acct-1", "symbol": "AAPL", "side": "buy", "qty": 2.0}


def _runtime(owned: set[str]) -> Any:
    seen: list[str] = []

    async def check(_caller: Any, subject: str) -> bool:
        seen.append(subject)
        return subject in owned

    return SimpleNamespace(
        service_scopes=frozenset({"finance:propose-order", "finance:read"}),
        check_subject_access=check,
        seen=seen,
    )


def _caller(tenant: str) -> Any:
    return SimpleNamespace(tenant=tenant, principal="alice", effective_scopes=set())


def test_operations_declare_the_service_executor_and_the_subject() -> None:
    for op in OPS.values():
        assert op.executor is Executor.SERVICE
        assert op.principals is PrincipalRule.HUMAN_UNDELEGATED
        assert op.subject.source is SubjectSource.PARAM
        assert op.subject.path == "params.account_id"
    paper = OPS["orders.paper_submit"]
    assert paper.effect is Effect.WRITE and paper.idempotency.name == "KEY_REQUIRED"
    assert paper.scopes == {"finance:read", "finance:propose-order"}
    assert paper.executor_scopes == {"finance:propose-order"}
    assert "orders.paper_submit" in EXTERNAL_EFFECT_OPERATIONS


@pytest.mark.spec("ARBORA-OPS-R017.2")
@pytest.mark.asyncio
async def test_paper_action_requires_account_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fleet(_context: Any, op_id: str, arguments: Any, **_k: Any) -> Any:
        assert op_id == "fleet.call"
        calls.append((arguments["tool"], arguments["arguments"]))
        return {"value": json.dumps({"status": "FILLED", "order_id": "o1"})}

    monkeypatch.setattr(finance, "invoke_registered", fleet)
    runtime = _runtime({"acct-1"})
    paper = OPS["orders.paper_submit"]
    # The owner passes the check, then the handler reaches the venue once.
    assert await prepare_executor(paper, PAPER, _caller("t1"), runtime) is None
    answer = await finance.handle_orders_paper_submit(None, PAPER, paper)
    assert answer["value"]["order_id"] == "o1" and runtime.seen == ["acct-1"]
    assert calls == [
        (
            "emerald_orders",
            {
                "action": "submit",
                "symbol": "AAPL",
                "side": "buy",
                "qty": 2.0,
                "order_type": "market",
                "limit_price": 0.0,
            },
        )
    ]
    # Another tenant does not own the account: refused, and no provider call.
    stranger = _runtime(set())
    denied = await prepare_executor(
        paper, {**PAPER, "account_id": "acct-9"}, _caller("t2"), stranger
    )
    assert denied is not None and denied.code == "SUBJECT_ACCESS_DENIED"
    assert len(calls) == 1
    # A request that names an owner is refused before any ownership lookup.
    named = await prepare_executor(
        paper, {**PAPER, "owner": "bob"}, _caller("t1"), _runtime({"acct-1"})
    )
    assert named is not None and named.code == "INVALID_ARGUMENT"
    # A live mode is refused by the handler; it never reaches the venue.
    from arbora.api.invoke.pipeline import OperationRefused

    with pytest.raises(OperationRefused) as refused:
        await finance.handle_orders_paper_submit(None, {**PAPER, "mode": "live"}, paper)
    assert refused.value.code == "POLICY_DENIED" and len(calls) == 1


@pytest.mark.spec("ARBORA-OPS-R017.2")
@pytest.mark.asyncio
async def test_positions_list_checks_ownership_and_reads_the_portfolio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fleet(_context: Any, _op: str, arguments: Any, **_k: Any) -> Any:
        assert arguments["tool"] == "emerald_portfolio"
        return {"value": [{"symbol": "AAPL", "qty": 2}]}

    monkeypatch.setattr(finance, "invoke_registered", fleet)
    op = OPS["positions.list"]
    assert (
        await prepare_executor(op, {"account_id": "a"}, _caller("t"), _runtime({"a"}))
        is None
    )
    answer = await finance.handle_positions_list(None, {"account_id": "a"}, op)
    assert answer["value"]["items"] == [{"symbol": "AAPL", "qty": 2}]
    stranger = await prepare_executor(
        op, {"account_id": "b"}, _caller("t"), _runtime(set())
    )
    assert stranger is not None and stranger.code == "SUBJECT_ACCESS_DENIED"


@pytest.mark.spec("ARBORA-OPS-R017.2")
@pytest.mark.asyncio
async def test_an_unanswered_venue_refuses_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    async def fleet(*_a: Any, **_k: Any) -> Any:
        return {"value": {"error": "not in the catalog"}}

    monkeypatch.setattr(finance, "invoke_registered", fleet)
    with pytest.raises(OperationRefused) as refused:
        await finance.handle_positions_list(
            None, {"account_id": "a"}, OPS["positions.list"]
        )
    assert refused.value.code == "UNAVAILABLE"
