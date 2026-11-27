"""P5: answers across connectors and across virtual graphs (ARBORA-ACCEPTANCE-R006.7).

The scenario runs on the ephemeral stack in the nightly job. Here the same steps run
through the served ``eg.query.CrossSourceQuestion`` operation over HTTP against an engine
double that answers per verified principal, so the pull-request run proves the refusal
and the coverage checks the nightly run applies to the real answers.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.testing.scenario_stack import (
    COVERAGE_BLOCK_MISSING,
    FACT_PROVENANCE_MISSING,
    UNREADABLE_SOURCE_ANSWERED,
    ScenarioRefused,
    ScenarioRun,
    ScenarioStack,
    check_coverage,
    check_fact_provenance,
)
from tests.api._served_support import RecordingEngine, person, route_methods
from tests.scenarios.conftest import scenario_stack  # noqa: F401
from tests.test_served_operation_api import _call

SOURCES = ("suppliers", "components", "vulnerabilities")
HIDDEN = "vulnerabilities"
QUESTION = "which suppliers ship a component with an open vulnerability"
SCOPES = ("federation:read",)


def _fact(source: str, name: str) -> dict[str, Any]:
    return {
        "id": f"fact:{name}",
        "source": source,
        "mapping": f"{source}-map-1",
        "read_at": "2026-11-26T12:00:00Z",
    }


def answer_for(principal: str) -> dict[str, Any]:
    """The engine double: per-source access rights, a coverage block, no silent drops."""
    readable = SOURCES if principal != "person:limited" else SOURCES[:2]
    facts = [_fact(source, f"{source}-1") for source in readable]
    excluded = [
        {"source": source, "reason": "SOURCE_NOT_READABLE"}
        for source in SOURCES
        if source not in readable
    ]
    return {
        "text": "supplier acme ships component c1"
        + ("" if excluded else " with CVE-1"),
        "facts": facts,
        "citations": [fact["id"] for fact in facts],
        "coverage": {"excluded": excluded},
        "operations": ["find:supplier", "ask:component", "act:patch"],
    }


def route_cross_source(stack: ScenarioStack, monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = RecordingEngine()
    route_methods(
        stack.engine,
        {
            "CrossSourceQuestion": engine.record(
                "CrossSourceQuestion", lambda _p, principal: answer_for(principal)
            )
        },
        monkeypatch,
        {
            "fixture-verified": person("one", extra_scopes=SCOPES),
            "limited": person("limited", extra_scopes=SCOPES),
        },
    )
    return engine


def ask(stack: ScenarioStack, **headers: str) -> dict[str, Any]:
    response = _call(
        stack.application,
        "eg.query.CrossSourceQuestion",
        {"question": QUESTION},
        **headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.7")
def test_answers_join_connectors_and_virtual_graphs_with_coverage(
    scenario_stack: ScenarioStack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = route_cross_source(scenario_stack, monkeypatch)
    full = ask(scenario_stack)
    assert check_fact_provenance(full["facts"]) == 3
    assert {fact["source"] for fact in full["facts"]} == set(SOURCES)
    assert full["operations"] == ["find:supplier", "ask:component", "act:patch"]
    check_coverage(full, unreadable=())

    limited = ask(scenario_stack, Authorization="Bearer limited")
    assert {fact["source"] for fact in limited["facts"]} == set(SOURCES[:2])
    check_coverage(limited, unreadable=(HIDDEN,))
    assert [principal for *_, principal in engine.calls] == [
        "person:one",
        "person:limited",
    ]
    run = ScenarioRun(
        scenario_id="p5-cross-source",
        phase="P5",
        result="passed",
        commit="0" * 40,
        requirement_ids=("ARBORA-ACCEPTANCE-R006.7",),
    )
    assert run.engine_record()["record"]["phase"] == "P5"


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.7")
def test_an_unprovenanced_fact_or_a_silent_drop_fails_the_scenario() -> None:
    with pytest.raises(ScenarioRefused) as bare:
        check_fact_provenance([{**_fact("suppliers", "s"), "mapping": ""}])
    assert bare.value.code == FACT_PROVENANCE_MISSING

    full = answer_for("person:one")
    with pytest.raises(ScenarioRefused) as leaked:
        check_coverage(full, unreadable=(HIDDEN,))
    assert leaked.value.code == UNREADABLE_SOURCE_ANSWERED

    limited = answer_for("person:limited")
    silent = {**limited, "coverage": {"excluded": []}}
    with pytest.raises(ScenarioRefused) as dropped:
        check_coverage(silent, unreadable=(HIDDEN,))
    assert dropped.value.code == COVERAGE_BLOCK_MISSING
    unreasoned = {
        **limited,
        "coverage": {"excluded": [{"source": HIDDEN, "reason": ""}]},
    }
    with pytest.raises(ScenarioRefused):
        check_coverage(unreasoned, unreadable=(HIDDEN,))
