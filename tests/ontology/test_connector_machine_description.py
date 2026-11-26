"""The connector desired-status machine is described in the ontology (ARBORA-FLEET-R082.3)."""

from __future__ import annotations

import pytest

from arbora.control_plane.connectors.machine import (
    DESIRED_EDGES,
    DESIRED_STATES,
    DesiredEvent,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-FLEET-R082.3")
def test_classes_roll_up_to_the_core_classes() -> None:
    check_machine(
        load_store(),
        name="ConnectorDesired",
        machine_name="ConnectorDesiredStatusMachine",
        states=set(DESIRED_STATES),
        terminal=set(),
        events={e.value for e in DesiredEvent},
        edges={(s, e.value, t) for (s, e), t in DESIRED_EDGES.items()},
        initial="disabled",
    )
