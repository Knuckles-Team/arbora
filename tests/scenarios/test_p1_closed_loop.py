"""P1: the minimal closed loop (ARBORA-ACCEPTANCE-R006.1.1 and R006.1.2).

The run step is a deterministic fixture model: it returns the named action
``defer_deploy`` when the injected bundle holds the claim and ``start_deploy`` otherwise.
Memory goes through the served ``memory.*`` operations over HTTP at arbora's boundary.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from arbora.testing.scenario_stack import (
    DECISION_RECORD_MISSING,
    SCENARIO_ACTION_UNCHANGED,
    SCENARIO_ACTION_UNCITED,
    SCENARIO_CONTROL_MISSING,
    ActionTrace,
    ScenarioCounterfactual,
    ScenarioRefused,
    ScenarioRun,
    ScenarioStack,
    check_counterfactual,
    check_provenance,
)
from tests.api._served_support import person, route_methods
from tests.scenarios.conftest import scenario_stack  # noqa: F401
from tests.test_served_operation_api import _call

CLAIM_KEY = "deploy-window"
CLAIM_TEXT = "deploy window is closed"


def _step(bundle: list[dict[str, Any]]) -> str:
    """The fixture model: the injected bundle decides the next action."""
    closed = any(item.get("text") == CLAIM_TEXT for item in bundle)
    return "defer_deploy" if closed else "start_deploy"


def _retrieve(stack: ScenarioStack, key: str, bearer: str) -> list[dict[str, Any]]:
    """Retrieve one claim as ``bearer``; a refused or absent claim leaves the bundle empty."""
    response = _call(
        stack.application, "memory.read", {"key": key}, Authorization=bearer
    )
    if response.status_code != 200:
        return []
    value = response.json()["result"]
    value = value["value"] if "value" in value and "found" not in value else value
    return [value["value"]] if value.get("found") else []


def _seed(stack: ScenarioStack) -> None:
    stack.http(
        "memory.write",
        {"key": CLAIM_KEY, "value": {"text": CLAIM_TEXT}},
        **{"Idempotency-Key": "seed-1"},
    )


@pytest.fixture
def people(scenario_stack: ScenarioStack, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """A second credential whose person holds no scope: policy refuses it the claim."""
    route_methods(
        scenario_stack.engine,
        {},
        monkeypatch,
        {
            "fixture-verified": person("one"),
            "Bearer limited": dataclasses.replace(person("limited"), roles=()),
        },
    )


def _counterfactual(stack: ScenarioStack) -> ScenarioCounterfactual:
    with_claim = _step(_retrieve(stack, CLAIM_KEY, "Bearer fixture-verified"))
    empty = _step(_retrieve(stack, "absent-key", "Bearer fixture-verified"))
    refused = _step(_retrieve(stack, CLAIM_KEY, "Bearer limited"))
    return ScenarioCounterfactual(
        claim_id=CLAIM_KEY,
        with_claim_action=with_claim,
        without_claim_action=empty,
        refused_by_policy_action=refused,
    )


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.1.1")
def test_retrieved_claim_changes_the_next_action(
    scenario_stack: ScenarioStack,  # noqa: F811
    people: None,
) -> None:
    _seed(scenario_stack)
    refusal = _call(
        scenario_stack.application,
        "memory.read",
        {"key": CLAIM_KEY},
        Authorization="Bearer limited",
    )
    assert refusal.status_code in {401, 403}, refusal.text
    counterfactual = _counterfactual(scenario_stack)

    assert counterfactual.with_claim_action == "defer_deploy"
    assert counterfactual.without_claim_action == "start_deploy"
    assert counterfactual.refused_by_policy_action == "start_deploy"
    check_counterfactual(counterfactual)
    run = ScenarioRun(
        scenario_id="p1-closed-loop",
        phase="P1",
        result="passed",
        commit="0" * 40,
        requirement_ids=("ARBORA-ACCEPTANCE-R006.1.1",),
        counterfactual=counterfactual,
    )
    assert run.engine_record()["record_kind"] == "scenario_run"
    assert "counterfactual" not in run.engine_record()["record"]

    equal = counterfactual.model_copy(update={"without_claim_action": "defer_deploy"})
    with pytest.raises(ScenarioRefused) as unchanged:
        check_counterfactual(equal)
    assert unchanged.value.code == SCENARIO_ACTION_UNCHANGED
    missing = counterfactual.model_copy(update={"refused_by_policy_action": None})
    with pytest.raises(ScenarioRefused) as control:
        check_counterfactual(missing)
    assert control.value.code == SCENARIO_CONTROL_MISSING


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.1.2")
def test_trace_records_action_and_cites_claim() -> None:
    decisions = {"decision-1"}
    trace = ActionTrace(
        run_id="run-a",
        action="defer_deploy",
        cited_claim_ids=(CLAIM_KEY,),
        decision_record_id="decision-1",
    )
    check_provenance(trace, [CLAIM_KEY], decisions.__contains__)

    uncited = ActionTrace("run-b", "start_deploy", (), "decision-1")
    with pytest.raises(ScenarioRefused) as no_claim:
        check_provenance(uncited, [CLAIM_KEY], decisions.__contains__)
    assert no_claim.value.code == SCENARIO_ACTION_UNCITED

    not_retrieved = ActionTrace("run-a", "defer_deploy", ("other",), "decision-1")
    with pytest.raises(ScenarioRefused) as foreign:
        check_provenance(not_retrieved, [CLAIM_KEY], decisions.__contains__)
    assert foreign.value.code == SCENARIO_ACTION_UNCITED

    unresolved = ActionTrace("run-a", "defer_deploy", (CLAIM_KEY,), "decision-9")
    with pytest.raises(ScenarioRefused) as record:
        check_provenance(unresolved, [CLAIM_KEY], decisions.__contains__)
    assert record.value.code == DECISION_RECORD_MISSING
