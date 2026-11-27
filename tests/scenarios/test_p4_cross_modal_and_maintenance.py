"""P4: the cross-modal bet and memory maintenance (ARBORA-ACCEPTANCE-R006.4).

The engine double answers the one federated query, the shape refusal at commit and the
maintenance methods; every call goes through the served operations over HTTP as the
verified principal. The falsification-gate verdict is an engine ask (``OVERSTORY-GAPS``,
section ``MEDULLA FOR ARBORA (w17)``): the scenario refuses a plan without a passed verdict.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.testing.scenario_stack import (
    ONE_PLAN_VIOLATED,
    PLAN_GATE_FAILED,
    ScenarioRefused,
    ScenarioRun,
    ScenarioStack,
    check_one_plan,
    check_plan_gate,
)
from tests.api._served_support import RecordingEngine, person, route_methods
from tests.scenarios.conftest import scenario_stack  # noqa: F401
from tests.test_served_operation_api import _call

PLAN = {
    "stages": [
        {"modality": "graph", "op": "neighbourhood"},
        {"modality": "vector", "op": "nearest"},
        {"modality": "attribute", "op": "filter"},
    ],
    "plans": ["plan-1"],
    "transaction": "txn-1",
    "security_context": "ctx-1",
}
GATE = {"passed": True, "plan": "plan-1", "recorded": True}
PROVENANCE = {"source": "scenario", "receipt": "run-1"}


def _ranking(state: dict[str, Any]) -> list[str]:
    scores = state["scores"]
    return sorted(scores, key=lambda node: -scores[node])


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.4")
def test_one_plan_validation_and_decay(
    scenario_stack: ScenarioStack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla import EngineResponseError

    stack = scenario_stack
    state: dict[str, Any] = {"scores": {"node:a": 0.9, "node:b": 0.5}}
    engine = RecordingEngine()

    def refuse_shape(_params: Any, _principal: str) -> Any:
        raise EngineResponseError("RECORD_SHAPE_INVALID", "RECORD_SHAPE_INVALID: min")

    def decay(_params: Any, _principal: str) -> Any:
        state["scores"]["node:a"] = 0.1
        return {"decayed": 1, "pruned": 0}

    route_methods(
        stack.engine,
        {
            "Uql": engine.record(
                "Uql",
                lambda *_: {
                    "rows": [{"id": node} for node in _ranking(state)],
                    "plan": PLAN,
                    "gate": GATE,
                    "provenance": PROVENANCE,
                },
            ),
            "Consolidate": engine.record("Consolidate", refuse_shape),
            "DecayMemories": engine.record("DecayMemories", decay),
        },
        monkeypatch,
        {
            "fixture-verified": person(
                "one",
                extra_scopes=(
                    "query:unified",
                    "memory:write",
                    "graph:write",
                    "graph:admin",
                ),
            )
        },
    )
    query = {"text": "MATCH (n) RETURN n"}
    before = _call(stack.application, "query.uql", query).json()["result"]
    check_one_plan(before["plan"])
    check_plan_gate(before["gate"])
    assert before["provenance"] == PROVENANCE
    assert [row["id"] for row in before["rows"]] == ["node:a", "node:b"]

    refused = _call(
        stack.application,
        "eg.graph.Consolidate",
        {"episodic_ids": ["episode:1"], "semantic_props_msgpack": ""},
        **{"Idempotency-Key": "shape-1"},
    )
    assert refused.json()["error"]["code"] == "RECORD_SHAPE_INVALID", refused.text

    swept = _call(
        stack.application,
        "eg.graph.DecayMemories",
        {"half_life_ms": 60_000, "ids": ["node:a"], "now_ms": 1_000_000},
        **{"Idempotency-Key": "decay-1"},
    )
    assert swept.status_code == 200, swept.text
    after = _call(stack.application, "query.uql", query).json()["result"]
    assert [row["id"] for row in after["rows"]] == ["node:b", "node:a"]
    assert {p for *_, p in engine.calls} == {"person:one"}
    run = ScenarioRun(
        scenario_id="p4-cross-modal",
        phase="P4",
        result="passed",
        commit="0" * 40,
        requirement_ids=("ARBORA-ACCEPTANCE-R006.4",),
    )
    assert run.engine_record()["record"]["phase"] == "P4"


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.4")
def test_a_split_plan_or_a_missing_gate_verdict_fails_the_scenario() -> None:
    check_one_plan(PLAN)
    for broken in (
        {**PLAN, "plans": ["plan-1", "plan-2"]},
        {**PLAN, "transaction": None},
        {**PLAN, "security_context": ""},
        {**PLAN, "stages": PLAN["stages"][:2]},
    ):
        with pytest.raises(ScenarioRefused) as split:
            check_one_plan(broken)
        assert split.value.code == ONE_PLAN_VIOLATED

    for verdict in (None, {}, {"passed": False}):
        with pytest.raises(ScenarioRefused) as gate:
            check_plan_gate(verdict)
        assert gate.value.code == PLAN_GATE_FAILED
