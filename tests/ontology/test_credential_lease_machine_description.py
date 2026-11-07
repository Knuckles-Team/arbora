"""The credential lease machine is described in the ontology (ARBORA-IDENTITY-R027.6)."""

from __future__ import annotations

from pathlib import Path

import pyoxigraph as ox
import pytest

from arbora.security.actor_context import (
    LEASE_EDGES,
    TERMINAL_LEASE_STATES,
    LeaseEvent,
    LeaseGuard,
    LeaseState,
)

PACK = Path(__file__).resolve().parents[2] / "arbora/ontology/state_machines.ttl"
CORE = "http://knuckles.team/kg#"
ARBORA = "http://knuckles.team/kg/arbora#"
MACHINE = f"{ARBORA}CredentialLeaseMachine"
PREFIXES = f"""
PREFIX : <{CORE}>
PREFIX arbora: <{ARBORA}>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


@pytest.fixture(scope="module")
def store() -> ox.Store:
    graph = ox.Store()
    graph.load(path=str(PACK), format=ox.RdfFormat.TURTLE)
    return graph


def _select(store: ox.Store, query: str) -> list[tuple[str, ...]]:
    rows = store.query(PREFIXES + query)
    assert isinstance(rows, ox.QuerySolutions)
    return [tuple(str(term.value) for term in row if term is not None) for row in rows]


def _rolls_up(store: ox.Store, cls: str, core: str) -> bool:
    answer = store.query(PREFIXES + f"ASK {{ <{cls}> rdfs:subClassOf+ :{core} }}")
    return bool(answer)


def _by_stored_form(store: ox.Store, parent: str) -> dict[str, str]:
    rows = _select(
        store,
        f"SELECT ?c ?f WHERE {{ ?c rdfs:subClassOf+ {parent} ; :storedForm ?f }}",
    )
    return {form: cls for cls, form in rows}


@pytest.mark.spec("ARBORA-IDENTITY-R027.6")
def test_every_state_and_transition_rolls_up_to_the_core_classes(
    store: ox.Store,
) -> None:
    imports = ox.NamedNode("http://www.w3.org/2002/07/owl#imports")
    assert list(store.quads_for_pattern(None, imports, None)) == []
    assert bool(store.query(PREFIXES + f"ASK {{ <{MACHINE}> a :StateMachine }}"))
    assert _rolls_up(store, MACHINE, "StateMachine")

    states = _by_stored_form(store, "arbora:LeaseState")
    assert set(states) == {state.value for state in LeaseState}
    for state in LeaseState:
        cls = states[state.value]
        assert _rolls_up(store, cls, "State")
        assert _rolls_up(store, cls, "TerminalState") is (
            state in TERMINAL_LEASE_STATES
        )
    initial = _select(store, f"SELECT ?s WHERE {{ <{MACHINE}> :initialState ?s }}")
    assert initial == [(states[LeaseState.LIVE.value],)]
    members = {
        row[0]
        for row in _select(store, f"SELECT ?s WHERE {{ <{MACHINE}> :hasState ?s }}")
    }
    assert members == set(states.values())

    events = _by_stored_form(store, "arbora:LeaseEvent")
    assert set(events) == {event.value for event in LeaseEvent}
    assert all(_rolls_up(store, cls, "TransitionEvent") for cls in events.values())
    guards = _by_stored_form(store, "arbora:LeaseGuard")
    assert set(guards) == {guard.value for guard in LeaseGuard}
    assert all(_rolls_up(store, cls, "TransitionGuard") for cls in guards.values())
    assert {cls.removeprefix(ARBORA) for cls in guards.values()} == {
        "AtHalfLifetime",
        "BeforeExpiry",
        "AtExpiry",
        "IdentityUnchanged",
    }
    assert _rolls_up(store, f"{ARBORA}LeaseTransitionRefused", "TransitionRefused")

    rows = _select(
        store,
        """SELECT ?t ?from ?event ?to ?guard WHERE {
            ?t rdfs:subClassOf+ :Transition ;
               :fromState ?from ; :firedBy ?event ; :toState ?to .
            OPTIONAL { ?t :guardedBy ?guard }
        }""",
    )
    described = {
        (t.removeprefix(f"{MACHINE}_"), f, e, to, g[0] if g else None)
        for t, f, e, to, *g in rows
    }
    expected = {
        (
            edge.edge_id,
            states[edge.source.value],
            events[edge.event.value],
            states[edge.target.value],
            None if edge.guard is None else guards[edge.guard.value],
        )
        for edge in LEASE_EDGES
    }
    assert described == expected
    assert len(described) == len(LEASE_EDGES) == 26


@pytest.mark.spec("ARBORA-IDENTITY-R027.6")
def test_the_next_legal_events_of_a_lease_state_are_a_graph_question(
    store: ox.Store,
) -> None:
    """ "What can happen next to an expired lease?" is one query over the pack."""
    rows = _select(
        store,
        """SELECT DISTINCT ?form WHERE {
            ?t rdfs:subClassOf+ :Transition ;
               :fromState arbora:LeaseExpired ; :firedBy ?event .
            ?event :storedForm ?form .
        }""",
    )
    assert {form for (form,) in rows} == {
        edge.event.value for edge in LEASE_EDGES if edge.source is LeaseState.EXPIRED
    }
