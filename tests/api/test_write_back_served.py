"""Queued write-back proposals are approved one at a time (ARBORA-OPS-R865)."""

from __future__ import annotations

import pytest
from medulla.generated import models

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import write_back
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context


@pytest.mark.spec("ARBORA-OPS-R865")
async def test_approve_one_proposal_and_refuse_without_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(models, "DecisionLogOpWriteback", raising=False)
    ids = {o.id for o in write_back.operations()}
    assert "write_back.proposal.approve" not in ids  # omitted until published
    monkeypatch.setattr(models, "DecisionLogOpWriteback", object, raising=False)
    ops = {o.id: o for o in write_back.operations()}
    approve = ops["write_back.proposal.approve"]
    assert approve.principals.name == "HUMAN" and approve.confirm.name == "PLAN"
    assert "write_back.proposals.list" in ops
    assert write_back.NATIVE_IDEMPOTENCY["write_back.proposal.approve"]

    engine = EngineDouble(monkeypatch)
    engine.serve("DecisionLog", lambda p: {"approved": True})
    engine.install(write_back)
    params = {"queue_id": "q1", "proposal_id": "p1"}
    outside = engine_context(scopes=frozenset())
    with pytest.raises(OperationRefused) as refused:
        await write_back.handle_write_back_proposal_approve(outside, params, approve)
    assert refused.value.code == "SCOPE_REQUIRED" and engine.sent == []
    inside = engine_context(scopes=frozenset({"approvals:decide"}))
    await write_back.handle_write_back_proposal_approve(inside, params, approve)
    ((method, sent, idempotent),) = engine.sent
    assert method == "DecisionLog" and idempotent
    assert sent["op"]["computation"] == {
        "kind": "approve",
        "queue_id": "q1",
        "proposal_id": "p1",
    }
