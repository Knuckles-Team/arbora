"""The execution step machine (ARBORA-OPS-R021.4.8.3).

``ExecutionStep.transition`` is the only writer of a step's ``status``.
"""

from __future__ import annotations

from itertools import product

import pytest

from arbora.api.ops.run_graph import (
    TASK_STATES,
    ExecutionStep,
    StepEvent,
    StepStatus,
    StepTransitionRefused,
)
from tests.architecture.single_writer import field_writers

S, E = StepStatus, StepEvent


def _step(status: StepStatus = StepStatus.UNOBSERVED) -> ExecutionStep:
    return ExecutionStep(key="run:one:first", task_id="first", status=status)


@pytest.mark.spec("ARBORA-OPS-R021.4.8.3")
@pytest.mark.parametrize(
    ("source", "event", "target"),
    [
        pytest.param(
            S.UNOBSERVED, E.TASK_STARTED, S.RUNNING, id="unobserved-task_started"
        ),
        pytest.param(
            S.UNOBSERVED, E.TASK_BLOCKED, S.BLOCKED, id="unobserved-task_blocked"
        ),
        pytest.param(
            S.RUNNING, E.TASK_COMPLETED, S.COMPLETED, id="running-task_completed"
        ),
        pytest.param(S.RUNNING, E.TASK_FAILED, S.FAILED, id="running-task_failed"),
        pytest.param(
            S.RUNNING, E.TASK_CANCELLED, S.CANCELLED, id="running-task_cancelled"
        ),
    ],
)
def test_step_legal_edge_succeeds(
    source: StepStatus, event: StepEvent, target: StepStatus
) -> None:
    step = _step(source)
    step.transition(event)
    assert step.status is target
    assert TASK_STATES[event] is target


@pytest.mark.spec("ARBORA-OPS-R021.4.8.3")
def test_step_every_illegal_pair_is_refused_and_the_status_is_unchanged() -> None:
    legal = set(ExecutionStep.EDGES)
    refused = 0
    for status, event in product(StepStatus, StepEvent):
        if (status, event) in legal:
            continue
        step = _step(status)
        with pytest.raises(StepTransitionRefused) as raised:
            step.transition(event)
        assert (raised.value.from_state, raised.value.event) == (status, event)
        assert raised.value.machine == "execution_step"
        assert raised.value.code == "STEP_TRANSITION_ILLEGAL"
        expected = (
            "missing start evidence"
            if status is S.UNOBSERVED
            and event in {E.TASK_COMPLETED, E.TASK_FAILED, E.TASK_CANCELLED}
            else "conflicting lifecycle transition"
        )
        assert str(raised.value) == expected
        assert step.status is status
        refused += 1
    assert refused == len(StepStatus) * len(StepEvent) - len(legal) == 25


@pytest.mark.spec("ARBORA-OPS-R021.4.8.3")
def test_step_terminal_states_have_no_exit_and_the_wire_values_are_unchanged() -> None:
    exits = {source for source, _ in ExecutionStep.EDGES}
    assert exits == {S.UNOBSERVED, S.RUNNING}
    assert set(ExecutionStep.TERMINALS) == set(StepStatus) - exits
    assert ExecutionStep.INITIAL is S.UNOBSERVED
    assert [status.value for status in StepStatus] == [
        "unobserved",
        "running",
        "completed",
        "failed",
        "cancelled",
        "blocked",
    ]
    assert [event.value for event in StepEvent] == [
        "task_started",
        "task_completed",
        "task_failed",
        "task_cancelled",
        "task_blocked",
    ]
    assert _step(S.RUNNING).model_dump(mode="json")["status"] == "running"


@pytest.mark.spec("ARBORA-OPS-R021.4.8.3")
def test_only_the_step_transition_writes_the_status_field() -> None:
    writers = {
        pair
        for pair in field_writers("arbora/api/ops", {"status"})
        if pair[0].endswith("run_graph.py")
    }
    assert writers == {("arbora/api/ops/run_graph.py", "transition")}
