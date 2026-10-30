"""Decide operations are service-only (ARBORA-OPS-R021.1)."""

from __future__ import annotations

import asyncio

import pytest

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


def test_decide_commit_result_carries_provenance_and_is_service_only() -> None:
    op = _op()
    assert principal_rule(op, _caller(principal_kind="human")) is not None
    assert principal_rule(op, _caller(principal_kind="service")) is None

    class _Runner:
        async def commit(self, request, *, tenant):
            return {"decision": "route-a", "tenant": tenant}

    context = service_context(tenant="tenant:one", decide_runner=_Runner())
    context.caller.principal = "service:decider"
    result = asyncio.run(decide.handle_decide_commit(context, {"request": {}}, op))
    assert result["value"]["decision"] == "route-a"
    assert result["provenance"] == {
        "op": "decide.commit",
        "tenant": "tenant:one",
        "principal": "service:decider",
    }
