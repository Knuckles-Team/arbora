"""The child runtime and breaker machines are described in the ontology (ARBORA-FLEET-R091)."""

from __future__ import annotations

import pytest

from arbora.fleet import child_machines as m
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-FLEET-R091")
def test_child_runtime_machine_matches_its_ontology_description() -> None:
    check_machine(
        load_store(),
        name="ChildRuntime",
        states={s.value for s in m.ChildState},
        terminal={s.value for s in m.CHILD_TERMINALS},
        events={e.value for e in m.ChildEvent},
        edges={(s.value, e.value, t.value) for (s, e), t in m.CHILD_EDGES.items()},
        initial=m.CHILD_INITIAL.value,
    )


@pytest.mark.spec("ARBORA-FLEET-R091.1")
def test_child_breaker_machine_matches_its_ontology_description() -> None:
    check_machine(
        load_store(),
        name="ChildBreaker",
        states={s.value for s in m.BreakerState},
        terminal={s.value for s in m.BREAKER_TERMINALS},
        events={e.value for e in m.BreakerEvent},
        edges={(s.value, e.value, t.value) for (s, e), t in m.BREAKER_EDGES.items()},
        initial=m.BREAKER_INITIAL.value,
    )
