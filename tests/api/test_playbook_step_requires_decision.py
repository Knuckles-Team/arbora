"""A playbook write step needs a committed decision record (ARBORA-OPS-R871)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _decide, fleet
from arbora.api.registry import Effect
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, *args, **kwargs):
        self.calls += 1
        return {"ok": True}


@pytest.mark.spec("ARBORA-OPS-R871")
async def test_action_refuses_without_a_committed_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = {"kind": "ConnectorWriteBack", "step_id": "s1", "tenant": "tenant:one"}
    records = {"ok": good, "other-step": {**good, "step_id": "s2"}}
    engine = EngineDouble(monkeypatch)
    engine.serve("DecisionLog", lambda p: records.get(p["op"]["record_id"], {}))
    engine.install(_decide)
    gateway = _Gateway()
    context = engine_context()
    context.services = {"fleet_gateway": gateway}
    op = fleet.operations()[0].model_copy(update={"effect": Effect.WRITE})
    params = {"server": "s", "tool": "t", "arguments": {}, "playbook_step_id": "s1"}
    for digest, reason in (
        (None, "step_decision_missing"),
        ("nope", "step_decision_missing"),
        ("other-step", "step_decision_mismatch"),
    ):
        with pytest.raises(OperationRefused) as refused:
            await fleet.require_step_decision(
                context, {**params, "decision_record_digest": digest}, op
            )
        assert refused.value.details["reason"] == reason
    assert gateway.calls == 0
    await fleet.require_step_decision(
        context, {**params, "decision_record_digest": "ok"}, op
    )
    read = op.model_copy(update={"effect": Effect.READ})
    await fleet.require_step_decision(context, params, read)
