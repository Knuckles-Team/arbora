"""The execution step machine is described in the ontology (ARBORA-OPS-R021.4.8.3)."""

from __future__ import annotations

import pytest

from arbora.api.ops.run_graph import ExecutionStep, StepEvent, StepStatus
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-OPS-R021.4.8.3")
def test_execution_step_machine_matches_its_ontology_description() -> None:
    check_machine(
        load_store(),
        name="ExecutionStep",
        states={s.value for s in StepStatus},
        terminal={s.value for s in ExecutionStep.TERMINALS},
        events={e.value for e in StepEvent},
        edges={
            (s.value, e.value, t.value) for (s, e), t in ExecutionStep.EDGES.items()
        },
        initial=ExecutionStep.INITIAL.value,
    )
