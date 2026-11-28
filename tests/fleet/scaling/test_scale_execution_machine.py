"""The scale execution record machine (ARBORA-FLEET-R024.19).

``ScaleExecutionRecord.transition`` is the only writer of ``state``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

import pytest

from arbora.api.domain.fleet.scale_records import (
    RESULT_EVENTS,
    SETTLED_STATES,
    ScaleExecutionEvent,
    ScaleExecutionRecord,
    ScaleExecutionState,
    ScaleExecutionTransitionRefused,
    ScaleIntentConflict,
    ScaleIntentState,
    ScaleTargetBinding,
)
from tests.architecture.single_writer import field_writers

S, E = ScaleExecutionState, ScaleExecutionEvent
NOW = datetime(2026, 11, 28, tzinfo=UTC)


def _record(state: ScaleExecutionState) -> ScaleExecutionRecord:
    return ScaleExecutionRecord(
        execution_id="execution-one",
        intent_id="intent-one",
        intent_revision=1,
        execution_key="key-one",
        target=ScaleTargetBinding(
            cluster_id="cluster-one",
            runtime_id="runtime-one",
            workload_id="workload-one",
            target_uid="uid-one",
            target_resource_version="1",
        ),
        state=state,
        attempt=0,
        identity_digest="a" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.spec("ARBORA-FLEET-R024.19")
@pytest.mark.parametrize(
    ("source", "event", "target"),
    [
        pytest.param(S.PERSISTED, E.START, S.STARTED, id="persisted-start"),
        pytest.param(S.STARTED, E.START, S.STARTED, id="started-start"),
        pytest.param(S.FAILED, E.START, S.STARTED, id="failed-start"),
        pytest.param(S.PERSISTED, E.SIMULATE, S.SIMULATED, id="persisted-simulate"),
        pytest.param(S.STARTED, E.SUCCEED, S.SUCCEEDED, id="started-succeed"),
        pytest.param(S.STARTED, E.FAIL, S.FAILED, id="started-fail"),
        pytest.param(S.SIMULATED, E.VERIFY, S.VERIFIED, id="simulated-verify"),
        pytest.param(S.SUCCEEDED, E.VERIFY, S.VERIFIED, id="succeeded-verify"),
        pytest.param(S.FAILED, E.VERIFY, S.VERIFIED, id="failed-verify"),
    ],
)
def test_execution_legal_edge_succeeds(
    source: ScaleExecutionState, event: ScaleExecutionEvent, target: ScaleExecutionState
) -> None:
    record = _record(source)
    after = record.transition(event, attempt=3)
    assert after.state == target.value
    assert after.attempt == 3
    assert record.state == source.value and record.attempt == 0


@pytest.mark.spec("ARBORA-FLEET-R024.19")
def test_execution_every_illegal_pair_is_refused_and_the_record_is_unchanged() -> None:
    legal = set(ScaleExecutionRecord.EDGES)
    refused = 0
    for state, event in product(ScaleExecutionState, ScaleExecutionEvent):
        if (state, event) in legal:
            continue
        record = _record(state)
        with pytest.raises(ScaleExecutionTransitionRefused) as raised:
            record.transition(event)
        assert isinstance(raised.value, ScaleIntentConflict)
        assert raised.value.code == "SCALE_EXECUTION_TRANSITION_ILLEGAL"
        assert raised.value.machine == "scale_execution"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        assert record.state == state.value
        refused += 1
    assert (
        refused
        == len(ScaleExecutionState) * len(ScaleExecutionEvent) - len(legal)
        == 21
    )


@pytest.mark.spec("ARBORA-FLEET-R024.19")
def test_execution_terminal_state_stored_values_and_result_events() -> None:
    exits = {source for source, _ in ScaleExecutionRecord.EDGES}
    assert set(ScaleExecutionRecord.TERMINALS) == set(ScaleExecutionState) - exits
    assert ScaleExecutionRecord.INITIAL is S.PERSISTED
    assert [state.value for state in ScaleExecutionState] == [
        "persisted",
        "started",
        "simulated",
        "succeeded",
        "failed",
        "verified",
    ]
    assert _record(S.STARTED).model_dump(mode="json")["state"] == "started"
    assert RESULT_EVENTS == {
        ScaleIntentState.SIMULATED.value: E.SIMULATE,
        ScaleIntentState.SUCCEEDED.value: E.SUCCEED,
        ScaleIntentState.FAILED.value: E.FAIL,
    }
    assert SETTLED_STATES == {"simulated", "succeeded", "verified"}


@pytest.mark.spec("ARBORA-FLEET-R024.19")
def test_only_the_execution_transition_writes_the_state_field() -> None:
    domain = {
        pair
        for pair in field_writers("arbora/api/domain/fleet", {"state"})
        if pair[0].endswith("scale_records.py")
    }
    ledgers = field_writers("arbora/fleet/scaling", {"state"})
    assert domain == {("arbora/api/domain/fleet/scale_records.py", "transition")}
    assert ledgers == set()
