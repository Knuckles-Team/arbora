"""The scale authority execution machine is described in the ontology (ARBORA-FLEET-R024.20)."""

from __future__ import annotations

import pytest

from arbora.api.domain.fleet.scaling.scaling_authority import (
    AuthorityExecutionEvent,
    AuthorityExecutionState,
    ScaleExecution,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-FLEET-R024.20")
def test_scale_authority_execution_machine_matches_its_ontology_description() -> None:
    check_machine(
        load_store(),
        name="AuthorityExecution",
        states={s.value for s in AuthorityExecutionState},
        terminal={s.value for s in ScaleExecution.TERMINALS},
        events={e.value for e in AuthorityExecutionEvent},
        edges={
            (s.value, e.value, t.value) for (s, e), t in ScaleExecution.EDGES.items()
        },
        initial=ScaleExecution.INITIAL.value,
    )
