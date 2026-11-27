"""ARBORA-FLEET-R056: the intent surface routes the five job, downtime and plan questions.

Each question reaches its engine method; arbora composes no answer and an op the
caller cannot see is never routed to.
"""

from __future__ import annotations

import pytest

from arbora.api.mcp.resolve import Descriptor, IntentResolver

_OPS = (
    Descriptor(
        id="eg.query.Decide",
        verb="ask",
        summary="Ask a decision question",
        params_schema={"required": ["request"]},
    ),
    Descriptor(
        id="eg.query.UnifiedQuery",
        verb="ask",
        summary="Run a unified query plan",
        params_schema={"required": ["plan"]},
    ),
    Descriptor(
        id="eg.graph.Sparql",
        verb="ask",
        summary="Run a SPARQL query",
        params_schema={"required": ["query"]},
    ),
    Descriptor(id="query.nl", verb="ask", summary="Plan a natural-language query"),
)

QUESTIONS = (
    (
        "Which tools and skills should be loaded for job J?",
        "eg.query.Decide",
        "load_set",
    ),
    ("Which downtime jobs run next?", "eg.graph.Sparql", "dw:Selection"),
    (
        "Which is the best virtual query plan across connectors A and B?",
        "eg.query.UnifiedQuery",
        None,
    ),
    (
        "How and when should an agent use connector X for job J?",
        "eg.query.Decide",
        "usage_guidance",
    ),
    (
        "Which seed facts did connector X contribute and which parts changed?",
        "eg.graph.Sparql",
        "seed:SeedImportReceipt",
    ),
)


@pytest.mark.spec("ARBORA-FLEET-R056")
def test_five_questions_through_intent_surface() -> None:
    resolver = IntentResolver()
    for question, op, marker in QUESTIONS:
        resolution = resolver.resolve("ask", question, _OPS, scope_ref=None)
        assert resolution.op == op, question
        assert resolution.preview is False
        body = str(resolution.params)
        assert marker is None or marker in body
        # The question carries no answer: the engine composes it with its provenance.
        assert "answer" not in resolution.params


@pytest.mark.spec("ARBORA-FLEET-R056")
def test_an_invisible_engine_method_is_never_routed_to() -> None:
    resolver = IntentResolver()
    without = tuple(d for d in _OPS if d.id != "eg.graph.Sparql")
    resolution = resolver.resolve(
        "ask", "Which downtime jobs run next?", without, scope_ref=None
    )
    assert resolution.op != "eg.graph.Sparql"
