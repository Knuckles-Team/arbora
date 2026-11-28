"""The onboarding machine is described in the ontology (ARBORA-FLEET-R080.3)."""

from __future__ import annotations

import pytest

from arbora.fleet.onboarding_machine import (
    ONBOARDING_EDGES,
    OnboardingEvent,
    OnboardingState,
)
from tests.ontology.machine_description import check_machine, load_store


@pytest.mark.spec("ARBORA-FLEET-R080.3")
def test_classes_roll_up_to_the_core_classes() -> None:
    check_machine(
        load_store(),
        name="Onboarding",
        machine_name="FleetOnboardingMachine",
        states={s.value for s in OnboardingState},
        terminal=set(),
        events={e.value for e in OnboardingEvent},
        edges={
            (s.value, e.value, t.value) for (s, e, _, _), t in ONBOARDING_EDGES.items()
        },
        guards={"AttemptsLeft": "attempts_left", "OnboardingLeaseLive": "lease_live"},
        initial=OnboardingState.IDLE.value,
    )
