"""The scale execution machine is described in the ontology (ARBORA-FLEET-R024.19)."""

from __future__ import annotations

import pytest

from arbora.api.domain.fleet.scale_records import (
    ScaleExecutionEvent,
    ScaleExecutionRecord,
    ScaleExecutionState,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-FLEET-R024.19")
def test_scale_execution_machine_matches_its_ontology_description() -> None:
    check_machine(
        load_store(),
        name="ScaleExecution",
        states={s.value for s in ScaleExecutionState},
        terminal={s.value for s in ScaleExecutionRecord.TERMINALS},
        events={e.value for e in ScaleExecutionEvent},
        edges={
            (s.value, e.value, t.value)
            for (s, e), t in ScaleExecutionRecord.EDGES.items()
        },
        initial=ScaleExecutionRecord.INITIAL.value,
    )
