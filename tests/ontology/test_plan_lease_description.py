"""The plan lease machine is described in the ontology (ARBORA-OPS-R903)."""

from __future__ import annotations

from pathlib import Path

import pyoxigraph as ox
import pytest

from arbora.api.invoke.plan import (
    PLAN_EDGES,
    TERMINAL_PLAN_STATES,
    PlanEvent,
    PlanState,
)

PACK = Path(__file__).resolve().parents[2] / "arbora/ontology/state_machines.ttl"
CORE = "https://knuckles-team.github.io/ontology/core#"
ARBORA = "https://knuckles-team.github.io/ontology/admission#"
MACHINE = f"{ARBORA}PlanLeaseMachine"
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


def _rows(store: ox.Store, query: str) -> list[tuple[str, ...]]:
    rows = store.query(PREFIXES + query)
    assert isinstance(rows, ox.QuerySolutions)
    return [tuple(str(term.value) for term in row if term is not None) for row in rows]


def _rolls_up(store: ox.Store, cls: str, core: str) -> bool:
    return bool(store.query(PREFIXES + f"ASK {{ <{cls}> rdfs:subClassOf+ :{core} }}"))


def _by_form(store: ox.Store, parent: str) -> dict[str, str]:
    rows = _rows(
        store, f"SELECT ?c ?f WHERE {{ ?c rdfs:subClassOf+ {parent} ; :storedForm ?f }}"
    )
    return {form: cls for cls, form in rows}


@pytest.mark.spec("ARBORA-OPS-R903")
def test_classes_roll_up_to_the_core_classes(store: ox.Store) -> None:
    imports = ox.NamedNode("http://www.w3.org/2002/07/owl#imports")
    assert list(store.quads_for_pattern(None, imports, None)) == []
    assert _rolls_up(store, MACHINE, "StateMachine")
    states = _by_form(store, "arbora:PlanState")
    assert set(states) == {state.value for state in PlanState}
    for state in PlanState:
        assert _rolls_up(store, states[state.value], "State")
        assert _rolls_up(store, states[state.value], "TerminalState") is (
            state in TERMINAL_PLAN_STATES
        )
    events = _by_form(store, "arbora:PlanEvent")
    assert set(events) == {event.value for event in PlanEvent}
    assert all(_rolls_up(store, cls, "TransitionEvent") for cls in events.values())
    assert _rolls_up(store, f"{ARBORA}BeforeHardExpiry", "TransitionGuard")
    assert _rolls_up(store, f"{ARBORA}PlanTransitionRefused", "Concept")
    rows = _rows(
        store,
        """SELECT ?t ?from ?event ?to WHERE {
            ?t rdfs:subClassOf+ arbora:PlanTransition ;
               :fromState ?from ; :firedBy ?event ; :toState ?to . }""",
    )
    described = {(t.removeprefix(f"{MACHINE}_"), f, e, to) for t, f, e, to in rows}
    expected = {
        (
            f"{s.value}-{e.value}-{t.value}",
            states[s.value],
            events[e.value],
            states[t.value],
        )
        for (s, e), t in PLAN_EDGES.items()
    }
    assert described == expected
    initial = _rows(store, f"SELECT ?s WHERE {{ <{MACHINE}> :initialState ?s }}")
    assert initial == [(states["issued"],)]
