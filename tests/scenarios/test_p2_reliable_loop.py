"""P2: the loop is reliable and inspectable (ARBORA-ACCEPTANCE-R006.2).

Claims go through the served ``memory.*`` operations over HTTP, one engine KV double
behind them. The fixture model decides each task from the retrieved bundle.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.testing.scenario_stack import (
    CLAIM_LINEAGE_MISSING,
    NO_MEMORY_LIFT,
    WRITE_CONFLICT_MISSING,
    WRITE_LOST,
    ScenarioRefused,
    ScenarioRun,
    ScenarioStack,
    check_claim_lineage,
    check_no_write_lost,
    check_write_conflict,
    filter_claims,
    require_memory_lift,
)
from tests.scenarios.conftest import scenario_stack  # noqa: F401
from tests.test_served_operation_api import _call

SOURCE = "fixture-connector"
RECORDS = 100
TASKS = 20


def _claim(index: int) -> dict[str, Any]:
    return {
        "id": f"claim-{index:03d}",
        "text": f"record {index} is current",
        "source": SOURCE if index % 2 == 0 else "other-connector",
        "actor": "person:one",
        "recorded_at": 1_000 + index,
        "confidence": 0.5 + index / 200,
        "valid_from": index,
    }


def _ingest(stack: ScenarioStack) -> list[str]:
    keys = []
    for index in range(RECORDS):
        claim = _claim(index)
        stack.http(
            "memory.write",
            {"key": claim["id"], "value": claim},
            **{"Idempotency-Key": f"ingest-{index}"},
        )
        keys.append(claim["id"])
    return keys


def _stored(stack: ScenarioStack) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"prefix": "claim-", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = stack.http("memory.list", params)
        claims += [entry["value"] for entry in page["entries"]]
        cursor = page.get("next_cursor")
        if not cursor:
            return claims


def _task_passes(task: int, bundle: list[dict[str, Any]]) -> bool:
    """The fixture model: odd tasks succeed only when their record was retrieved."""
    return task % 2 == 0 or any(c["id"] == f"claim-{task:03d}" for c in bundle)


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.2")
def test_lineage_conflict_and_memory_lift(
    scenario_stack: ScenarioStack,  # noqa: F811
) -> None:
    keys = _ingest(scenario_stack)
    stored = _stored(scenario_stack)
    assert check_claim_lineage(stored) == RECORDS
    check_no_write_lost(keys, [claim["id"] for claim in stored])

    ours = filter_claims(stored, source=SOURCE, min_confidence=0.75, valid_at=60)
    assert ours and all(c["source"] == SOURCE and c["confidence"] >= 0.75 for c in ours)
    assert all(c["valid_from"] <= 60 for c in ours)

    first = _call(
        scenario_stack.application,
        "memory.write",
        {"key": "shared-claim", "value": {"v": 1}, "expected_version": 0},
        **{"Idempotency-Key": "w1"},
    )
    second = _call(
        scenario_stack.application,
        "memory.write",
        {"key": "shared-claim", "value": {"v": 2}, "expected_version": 0},
        **{"Idempotency-Key": "w2"},
    )
    first_out = first.json()["result"]["value"]
    second_out = second.json()["result"]["value"]
    check_write_conflict(first_out, second_out)
    kept = scenario_stack.http("memory.read", {"key": "shared-claim"})
    assert kept["value"] == {"v": 1}

    bundle = stored
    without = [_task_passes(task, []) for task in range(TASKS)]
    with_retrieval = [_task_passes(task, bundle) for task in range(TASKS)]
    require_memory_lift(without, with_retrieval)
    run = ScenarioRun(
        scenario_id="p2-reliable-loop",
        phase="P2",
        result="passed",
        commit="0" * 40,
        requirement_ids=("ARBORA-ACCEPTANCE-R006.2",),
    )
    assert run.engine_record()["record"]["phase"] == "P2"


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.2")
def test_a_missing_lineage_conflict_or_lift_fails_the_scenario() -> None:
    bare = {**_claim(1), "source": ""}
    with pytest.raises(ScenarioRefused) as lineage:
        check_claim_lineage([_claim(0), bare])
    assert lineage.value.code == CLAIM_LINEAGE_MISSING
    assert lineage.value.details["missing"] == ["source"]

    with pytest.raises(ScenarioRefused) as lost:
        check_no_write_lost(["claim-000", "claim-001"], ["claim-000"])
    assert lost.value.code == WRITE_LOST

    won = {"swapped": True}
    check_write_conflict(won, {"swapped": False, "version": 1})
    for first, second in (
        (won, won),
        ({"swapped": False}, won),
        (won, {"code": "WRITE_CONFLICT"}),
    ):
        with pytest.raises(ScenarioRefused) as conflict:
            check_write_conflict(first, second)
        assert conflict.value.code == WRITE_CONFLICT_MISSING

    with pytest.raises(ScenarioRefused) as lift:
        require_memory_lift([False] * 20, [True] + [False] * 19)
    assert lift.value.code == NO_MEMORY_LIFT
