"""The scale authority execution machine (ARBORA-FLEET-R024.20).

``ScaleExecution.transition`` is the only writer of ``state`` and returns the
next execution revision.
"""

from __future__ import annotations

import inspect
from itertools import product

import pytest

from arbora.api.domain.fleet.scaling.scaling_authority import (
    AuthorityExecutionEvent,
    AuthorityExecutionState,
    AuthorityExecutionTransitionRefused,
    ScaleExecution,
)
from tests.architecture.single_writer import field_writers
from tests.fleet.scaling.test_scaling_authority_contract import EXECUTION, LATER

S, E = AuthorityExecutionState, AuthorityExecutionEvent
_EVIDENCE = {
    S.SUCCEEDED: {"completed_at": LATER},
    S.FAILED: {"completed_at": LATER, "failure_code": "actuator-error"},
    S.ROLLED_BACK: {"completed_at": LATER, "rollback_target_replicas": 2},
}


def _execution(state: AuthorityExecutionState) -> ScaleExecution:
    base = {**EXECUTION, "state": state, "completed_at": None}
    return ScaleExecution.model_validate({**base, **_EVIDENCE.get(state, {})})


@pytest.mark.spec("ARBORA-FLEET-R024.20")
@pytest.mark.parametrize(
    ("source", "event", "target"),
    [
        pytest.param(S.PREPARED, E.START, S.STARTED, id="prepared-start"),
        pytest.param(S.PREPARED, E.SIMULATE, S.SIMULATED, id="prepared-simulate"),
        pytest.param(S.STARTED, E.SUCCEED, S.SUCCEEDED, id="started-succeed"),
        pytest.param(S.STARTED, E.FAIL, S.FAILED, id="started-fail"),
        pytest.param(S.SUCCEEDED, E.ROLL_BACK, S.ROLLED_BACK, id="succeeded-roll_back"),
        pytest.param(S.FAILED, E.ROLL_BACK, S.ROLLED_BACK, id="failed-roll_back"),
    ],
)
def test_authority_execution_legal_edge_succeeds(
    source: AuthorityExecutionState,
    event: AuthorityExecutionEvent,
    target: AuthorityExecutionState,
) -> None:
    record = _execution(source)
    after = record.transition(event, **_EVIDENCE.get(target, {}))
    assert after.state == target.value
    assert after.execution_revision == record.execution_revision + 1
    assert record.state == source.value


@pytest.mark.spec("ARBORA-FLEET-R024.20")
def test_authority_execution_every_illegal_pair_is_refused_and_unchanged() -> None:
    legal = set(ScaleExecution.EDGES)
    refused = 0
    for state, event in product(AuthorityExecutionState, AuthorityExecutionEvent):
        if (state, event) in legal:
            continue
        record = _execution(state)
        with pytest.raises(AuthorityExecutionTransitionRefused) as raised:
            record.transition(event)
        assert raised.value.code == "SCALE_AUTHORITY_TRANSITION_ILLEGAL"
        assert raised.value.machine == "scale_authority_execution"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        assert record.state == state.value
        refused += 1
    assert (
        refused
        == len(AuthorityExecutionState) * len(AuthorityExecutionEvent) - len(legal)
        == 24
    )


@pytest.mark.spec("ARBORA-FLEET-R024.20")
def test_authority_execution_terminals_and_stored_values() -> None:
    exits = {source for source, _ in ScaleExecution.EDGES}
    assert set(ScaleExecution.TERMINALS) == set(AuthorityExecutionState) - exits
    assert ScaleExecution.INITIAL is S.PREPARED
    assert [s.value for s in AuthorityExecutionState] == [
        "prepared",
        "simulated",
        "started",
        "succeeded",
        "failed",
        "rolled_back",
    ]
    assert _execution(S.STARTED).model_dump(mode="json")["state"] == "started"


@pytest.mark.spec("ARBORA-FLEET-R024.20")
def test_authority_execution_transition_keeps_the_record_validators() -> None:
    with pytest.raises(ValueError, match="failure_code"):
        _execution(S.STARTED).transition(E.FAIL, completed_at=LATER)


@pytest.mark.spec("ARBORA-FLEET-R024.20")
def test_only_the_authority_execution_transition_writes_the_state_field() -> None:
    # The record is frozen: no attribute, copy or setattr write of ``state`` exists in the
    # package, and ``transition`` rebuilds the record through the validators.
    writers = {
        pair
        for pair in field_writers("arbora/api/domain/fleet/scaling", {"state"})
        if pair[0].endswith("scaling_authority.py")
    }
    assert writers == set()
    source = inspect.getsource(ScaleExecution.transition)
    assert source.count('"state"') == 1 and "model_validate" in source
