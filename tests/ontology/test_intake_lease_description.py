"""The intake lease machine is described in the ontology (ARBORA-MESSAGING-R800.1005)."""

from __future__ import annotations

import pytest

from arbora.messaging.lease import (
    _LEASE_TRANSITIONS,
    IntakeLeaseEvent,
    IntakeLeaseState,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-MESSAGING-R800.1005")
def test_classes_roll_up_to_the_core_classes() -> None:
    check_machine(
        load_store(),
        name="IntakeLease",
        states={s.value for s in IntakeLeaseState},
        terminal={IntakeLeaseState.RELEASED.value},
        events={e.value for e in IntakeLeaseEvent},
        edges={(s.value, e.value, t.value) for (s, e), t in _LEASE_TRANSITIONS.items()},
        guards={"WithinTtl": "within_ttl"},
        initial=IntakeLeaseState.HELD.value,
    )
