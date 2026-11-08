"""The client contract machine is described in the ontology (ARBORA-IDENTITY-R030.4.5)."""

from __future__ import annotations

import pyoxigraph as ox
import pytest

from arbora.identity.client_contract import (
    CONTRACT_EDGES,
    TERMINAL_CONTRACT_STATES,
    ContractEvent,
    ContractState,
)
from tests.ontology.test_credential_lease_machine_description import (
    ARBORA,
    _by_stored_form,
    _rolls_up,
    _select,
    store,
)

MACHINE = f"{ARBORA}ClientContractMachine"
__all__ = ["store"]  # the shared module-scoped fixture


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.5")
def test_classes_roll_up_to_the_core_classes(store: ox.Store) -> None:
    from tests.ontology.test_credential_lease_machine_description import PREFIXES

    assert bool(store.query(PREFIXES + f"ASK {{ <{MACHINE}> a :StateMachine }}"))
    states = _by_stored_form(store, "arbora:ContractState")
    assert set(states) == {state.value for state in ContractState}
    for state in ContractState:
        assert _rolls_up(store, states[state.value], "State")
        assert _rolls_up(store, states[state.value], "TerminalState") is (
            state in TERMINAL_CONTRACT_STATES
        )
    events = _by_stored_form(store, "arbora:ContractEvent")
    assert set(events) == {event.value for event in ContractEvent}
    assert all(_rolls_up(store, cls, "TransitionEvent") for cls in events.values())
    assert _rolls_up(store, f"{ARBORA}ContractMatches", "TransitionGuard")
    assert _rolls_up(store, f"{ARBORA}ContractTransitionRefused", "Concept")

    rows = _select(
        store,
        """SELECT ?t ?from ?event ?to WHERE {
            ?t rdfs:subClassOf+ arbora:ContractTransition ;
               :fromState ?from ; :firedBy ?event ; :toState ?to .
        }""",
    )
    described = {(t.removeprefix(f"{MACHINE}_"), f, e, to) for t, f, e, to in rows}
    expected = {
        (
            edge.edge_id,
            states[edge.source.value],
            events[edge.event.value],
            states[edge.target.value],
        )
        for edge in CONTRACT_EDGES
    }
    assert described == expected and len(described) == 9
    guarded = _select(store, f"SELECT ?g WHERE {{ <{MACHINE}_C02> :guardedBy ?g }}")
    assert guarded == [(f"{ARBORA}ContractMatches",)]
