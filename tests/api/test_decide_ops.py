"""Decide operations are service-only (ARBORA-OPS-R021.1)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.plan import params_digest
from arbora.api.invoke.steps import principal_rule
from arbora.api.ops import decide
from tests.api._ops_support import service_context
from tests.api._support import make_verified_caller

_caller = make_verified_caller("decide:commit")


def _op():
    (op,) = decide.operations()
    return op


def test_decide_commit_is_service_only() -> None:
    op = _op()
    assert op.id == "decide.commit"
    human = _caller(principal_kind="human")
    assert principal_rule(op, human) is not None, "a human-scoped token must be refused"


def test_decide_commit_allows_a_service_principal() -> None:
    op = _op()
    service = _caller(principal_kind="service")
    assert principal_rule(op, service) is None


@pytest.mark.spec("ARBORA-OPS-R021.1")
def test_commit_calls_engine_client_not_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    op = _op()
    assert principal_rule(op, _caller(principal_kind="human")) is not None
    assert principal_rule(op, _caller(principal_kind="service")) is None
    sent: list[tuple[str, dict]] = []

    async def fake_send(context, method, params, *, idempotent=False):
        sent.append((method, params))
        assert idempotent is True
        return {"record_id": "rec-1", "replayed": False}

    monkeypatch.setattr(decide, "engine_send", fake_send)
    context = service_context(
        tenant="tenant:one",
        decide_runner=object(),  # a composed runner is never read
    )
    context.caller.principal = "service:decider"
    request = {"record": {"kind": "route"}}
    result = asyncio.run(decide.handle_decide_commit(context, {"request": request}, op))
    assert sent == [("DecisionCommit", {"request": request})]
    assert result["value"]["record_id"] == "rec-1"
    assert result["value"]["record_digest"] == params_digest({"kind": "route"})
    assert result["provenance"] == {
        "op": "decide.commit",
        "tenant": "tenant:one",
        "principal": "service:decider",
    }


@pytest.mark.spec("ARBORA-OPS-R021.1")
def test_absent_engine_client_refuses_unavailable() -> None:
    context = service_context(tenant="tenant:one")
    context.client = None
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(decide.handle_decide_commit(context, {"request": {}}, _op()))
    assert refused.value.code == "UNAVAILABLE"
