"""Shared check that an ontology machine matches its Python enums and edge table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyoxigraph as ox

PACK = Path(__file__).resolve().parents[2] / "arbora/ontology/state_machines.ttl"
CORE = "https://knuckles-team.github.io/ontology/core#"
ARBORA = "https://knuckles-team.github.io/ontology/admission#"
PREFIXES = f"""
PREFIX : <{CORE}>
PREFIX arbora: <{ARBORA}>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


def load_store() -> ox.Store:
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


def check_machine(
    store: ox.Store,
    *,
    name: str,
    states: set[str],
    terminal: set[str],
    events: set[str],
    edges: set[tuple[str, str, str]],
    guards: dict[str, str] | None = None,
    initial: str | None = None,
    machine_name: str | None = None,
) -> None:
    """Assert ``arbora:<name>Machine`` equals the given Python machine.

    ``edges`` holds ``(from, event, to)`` stored forms; ``guards`` maps a guard
    class local name to the guard it must roll up as.
    """
    imports = ox.NamedNode("http://www.w3.org/2002/07/owl#imports")
    assert list(store.quads_for_pattern(None, imports, None)) == []
    machine = f"{ARBORA}{machine_name or name + 'Machine'}"
    assert _rolls_up(store, machine, "StateMachine")
    by_state = _by_form(store, f"arbora:{name}State")
    assert set(by_state) == states
    for state in states:
        assert _rolls_up(store, by_state[state], "State")
        assert _rolls_up(store, by_state[state], "TerminalState") is (state in terminal)
    by_event = _by_form(store, f"arbora:{name}Event")
    assert set(by_event) == events
    assert all(_rolls_up(store, cls, "TransitionEvent") for cls in by_event.values())
    for guard in guards or {}:
        assert _rolls_up(store, f"{ARBORA}{guard}", "TransitionGuard")
    assert _rolls_up(store, f"{ARBORA}{name}TransitionRefused", "Concept")
    rows = _rows(
        store,
        f"""SELECT ?t ?from ?event ?to WHERE {{
            ?t rdfs:subClassOf+ arbora:{name}Transition ;
               :fromState ?from ; :firedBy ?event ; :toState ?to . }}""",
    )
    described = {(f, e, t) for _, f, e, t in rows}
    expected = {(by_state[f], by_event[e], by_state[t]) for f, e, t in edges}
    assert described == expected
    if initial is not None:
        got = _rows(store, f"SELECT ?s WHERE {{ <{machine}> :initialState ?s }}")
        assert got == [(by_state[initial],)]


__all__: list[Any] = ["check_machine", "load_store"]
