"""The ``ask`` routing table from the 2026-10-24 validation run (G8).

Every one of the 26 questions is a data, catalog, lineage or ownership
question, so ``ask`` routes it to the engine planner (``query.nl``). The
assertion is the chosen operation, not the answer. The descriptors are the
production registry's ``ask`` operations, as the served door projects them.
"""

from __future__ import annotations

import pytest

from arbora.api.mcp.discovery import describe_op
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.ops import get_registry

QUESTIONS = (
    ("Q1", "Who owns ServiceNow incidents, and where does that data come from?"),
    ("Q2", "Show open issues across GitHub, GitLab and Plane."),
    ("Q3", "Which prompts can act as a system prompt for incident triage?"),
    ("CQ-a", "Who owns ServiceNow incidents, and where does that data come from?"),
    ("CQ-b", "Show open issues across GitHub, GitLab and Plane."),
    ("CQ-c", "Which prompts can act as a system prompt for incident triage?"),
    (
        "CMDB1",
        "What is the blast radius of a db01 outage: which applications, business services and owner groups are affected?",
    ),
    (
        "CMDB2",
        "What is the blast radius of an app02 outage, including application-to-application dependencies?",
    ),
    (
        "CMDB3",
        "Which business services depend on retired or prohibited technology and have open incidents?",
    ),
    (
        "CMDB4",
        "Which technologies in use have an open TRM request, and where are they deployed?",
    ),
    ("CMDB5", "Which change requests touch CIs with open P1 or P2 incidents?"),
    (
        "CMDB6",
        "What is the change risk by technology state for each change request, joining touched CIs to technology and TRM phase?",
    ),
    ("CMDB7", "What is the technology debt by owner group of the dependent service?"),
    (
        "CMDB8",
        "Which CIs are orphaned or isolated, such as servers that host no application?",
    ),
    (
        "CMDB9",
        "Which GitLab or GitHub repositories and Plane work items relate to the Billing Engine and Payments applications affected by incident INC-B?",
    ),
    (
        "CMDB10",
        "Which prompts, skills or agents could triage incident INC-A, a PostgreSQL 11 database outage on db01?",
    ),
    (
        "CQ-d1",
        "Blast radius of a server outage: which applications run on it, which business services depend on them, who owns those services?",
    ),
    (
        "CQ-d2",
        "Blast radius of an application outage, including application-to-application dependency.",
    ),
    (
        "CQ-d3",
        "Which business services depend on retired technology and have open incidents?",
    ),
    ("CQ-d4", "Which technologies in use have an open TRM request, and where?"),
    ("CQ-d5", "Which change requests touch CIs with open P1/P2 incidents?"),
    (
        "CQ-d6",
        "Change risk by technology state: touched CIs joined to technology and TRM phase.",
    ),
    ("CQ-d7", "Technology debt by owner group of the dependent service."),
    ("CQ-d8", "Orphan and isolated CIs: servers that host no application."),
    ("CQ-d9", "Cross-source: repositories and work items for an affected application."),
    ("CQ-d10", "Which prompts, skills or agents could triage this incident."),
)


@pytest.fixture(scope="module")
def ask_descriptors() -> list[dict]:
    return [
        describe_op(op)
        for op in get_registry()
        if str(getattr(op.verb, "value", op.verb)) == "ask"
    ]


def test_the_table_holds_the_26_validation_questions() -> None:
    assert len(QUESTIONS) == 26


@pytest.mark.spec("ARBORA-FLEET-R014.3")
@pytest.mark.parametrize(("name", "question"), QUESTIONS, ids=[n for n, _ in QUESTIONS])
def test_validation_question_routes_to_the_engine_planner(
    ask_descriptors, name: str, question: str
) -> None:
    resolution = IntentResolver().resolve(
        "ask", question, ask_descriptors, scope_ref=None
    )
    assert resolution.op == "query.nl", (name, resolution.op, resolution.why)
    assert resolution.params == {"text": question}


@pytest.mark.spec("ARBORA-FLEET-R014.3")
@pytest.mark.parametrize(
    ("question", "op"),
    [
        ("show my fleet multiplexer status", "fleet.status"),
        ("list my agent tasks", "agents.tasks.list"),
        ("show the current security posture", "security.posture.read"),
        ("show usage for this tenant", "usage.read"),
    ],
)
def test_a_question_that_names_an_op_still_routes_to_it(
    ask_descriptors, question: str, op: str
) -> None:
    resolution = IntentResolver().resolve(
        "ask", question, ask_descriptors, scope_ref=None
    )
    assert resolution.op == op


@pytest.mark.spec("ARBORA-FLEET-R014.3")
def test_a_plain_question_never_resolves_to_a_generated_engine_method(
    ask_descriptors,
) -> None:
    resolution = IntentResolver().resolve(
        "ask",
        "what is the blast radius of a db01 outage",
        ask_descriptors,
        scope_ref=None,
    )
    assert resolution.op == "query.nl"
    assert "eg.compute.GetBlastRadius" in resolution.alternatives


@pytest.mark.spec("ARBORA-FLEET-R014.3")
def test_explicit_params_keep_the_ranked_op(ask_descriptors) -> None:
    resolution = IntentResolver().resolve(
        "ask",
        "show the status of this agent task",
        ask_descriptors,
        scope_ref=None,
        params={"task_id": "t-1"},
    )
    assert resolution.op != "query.nl"
