"""The scale verdict is the engine's ``Decide`` answer (ARBORA-FLEET-R024.16).

The engine is a structural double of the one method the policy sends; the question kind
is engine ``main``, so the pin-lag test runs the real guard against the pinned release.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.scaling import engine_policy
from arbora.fleet.scaling.engine_policy import EngineScalePolicy
from arbora.fleet.scaling.fleet_actuator import bind_request
from arbora.fleet.scaling.scaling_intent_reconciler import (
    MemoryScaleIntentLedger,
    ScaleIntentReconciler,
    ScaleIntentState,
)
from tests.fleet.scaling._scale_builder import declare_scale_builder
from tests.fleet.scaling.test_scaling_intent_reconciler import NOW, Actuator, intent

PRINCIPAL = "person:one"
SCHEMA = {
    "component_id": "deploy.feature_schema",
    "definition_digest": "sha256:" + "0" * 64,
    "kind": "feature_schema",
}


def _record(request: dict[str, Any], option: str, **over: Any) -> dict[str, Any]:
    record = {
        "caller_principal": "principal:sha256:"
        + hashlib.sha256(PRINCIPAL.encode()).hexdigest(),
        "tenant_id": request["tenant_id"],
        "question": request["question"],
        "inputs": {"params": request["params"]},
        "record_id": "decision:scale-1",
        "record_digest": "digest:scale-1",
        "outcome": {
            "outcome": "advisory",
            "scores": [{"option_id": option, "score": {"value": 1}}],
        },
    }
    return {"records": [{**record, **over}]}


class Engine:
    """Answers ``Decide`` with ``answer(request)``; records each wire call."""

    def __init__(self, option: str = "allow", **over: Any) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.option, self.over = option, over

    async def __call__(self, context: Any, method: str, params: dict[str, Any]) -> Any:
        self.sent.append((method, params))
        return _record(params["request"], self.option, **self.over)


def _context() -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t1", principal=PRINCIPAL),
        services={"deploy_feature_schema": SCHEMA},
    )


def _assert_engine_fields_only(sent: str, record: Any) -> None:
    """Only the fields the engine's ScaleIntentRecord reads leave arbora."""
    wire = json.loads(sent)
    assert set(wire) == engine_policy.WIRE_FIELDS
    assert wire["target"]["workload_id"] == record.target.workload_id
    assert "reason" not in wire and "retry_policy" not in wire


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Engine:
    """The question kind declared (as after the re-pin) and an engine double."""
    engine = Engine()

    async def graph(context: Any) -> str:
        return "graph:t1"

    declare_scale_builder(monkeypatch)
    monkeypatch.setattr("arbora.api.ops.engine_calls.engine_send", engine)
    monkeypatch.setattr("arbora.api.invoke.bootstrap.caller_dispatch_graph", graph)
    return engine


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.16")
async def test_a_pinned_release_without_the_question_refuses_before_any_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Engine()
    monkeypatch.setattr("arbora.api.ops.engine_calls.engine_send", engine)
    monkeypatch.delattr("medulla.decision_client.scale_intent_request", raising=False)
    reconciler = ScaleIntentReconciler(
        MemoryScaleIntentLedger(),
        actuator := Actuator([]),
        policy=EngineScalePolicy(),
        clock=lambda: NOW,
    )
    with bind_request(_context()):
        with pytest.raises(OperationRefused) as refused:
            await reconciler.reconcile(intent(), controller_id="writer-1")
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "decision_client.scale_intent_request"
    assert engine.sent == [] and actuator.calls == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.16")
async def test_an_engine_allow_is_the_decision_and_names_the_sent_question(
    served: Engine,
) -> None:
    record = intent()
    with bind_request(_context()):
        decision = await EngineScalePolicy().decide(record)
    assert decision.allowed is True and decision.decision_id.startswith("decision:")
    ((method, params),) = served.sent
    request = params["request"]
    assert method == "Decide" and set(params) == {"request"}
    assert request["tenant_id"] == "t1"
    assert request["question"]["kind"] == "scale_intent"
    assert request["question"]["question_id"] == f"fleet.scale:{record.execution_key}"
    names = {p["name"]: p["value"]["value"] for p in request["params"]}
    assert names["graph"] == "graph:t1" and record.execution_key in names["intent"]
    _assert_engine_fields_only(names["intent"], record)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.16")
async def test_an_engine_refusal_denies_and_the_actuator_is_not_reached(
    served: Engine,
) -> None:
    served.option = "refuse:SCALE_POOL_HEADROOM_EXCEEDED"
    actuator = Actuator([])
    reconciler = ScaleIntentReconciler(
        MemoryScaleIntentLedger(),
        actuator,
        policy=EngineScalePolicy(),
        clock=lambda: NOW,
    )
    with bind_request(_context()):
        result = await reconciler.reconcile(intent(), controller_id="writer-1")
    assert result.state == ScaleIntentState.DENIED.value
    assert "SCALE_POOL_HEADROOM_EXCEEDED" in result.detail
    assert actuator.calls == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.16")
async def test_a_record_that_does_not_answer_the_request_or_names_no_verdict_refuses(
    served: Engine,
) -> None:
    record = intent()
    for answered in (
        {"tenant_id": "other"},
        {"caller_principal": "principal:sha256:" + "0" * 64},
        {
            "outcome": {
                "outcome": "advisory",
                "scores": [{"option_id": "maybe", "score": {"value": 1}}],
            }
        },
    ):
        served.over = answered
        with bind_request(_context()):
            with pytest.raises(OperationRefused) as refused:
                await EngineScalePolicy().decide(record)
        assert refused.value.code == "RECEIPT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.16")
async def test_no_bound_request_refuses_unavailable(served: Engine) -> None:
    with pytest.raises(OperationRefused) as refused:
        await EngineScalePolicy().decide(intent())
    assert refused.value.code == "UNAVAILABLE" and served.sent == []
