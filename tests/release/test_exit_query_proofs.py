"""Exit obligation ``query_proofs``: UQL and natural-language questions answer from ingested facts (ARBORA-RELEASE-R003.3)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import RecordingEngine, route_methods
from tests.release._stack import SCOPES, caller, stack  # noqa: F401
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

PROVENANCE = {"source": "recorded-connector", "receipt": "job-1"}


@pytest.mark.spec("ARBORA-RELEASE-R003.3")
def test_uql_and_nl_questions_answer_from_ingested_facts(
    stack: dict[str, Any],  # noqa: F811
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = RecordingEngine()
    route_methods(
        client,
        {
            "Uql": engine.record(
                "Uql", {"rows": [{"id": "fact:issue-1"}], "provenance": PROVENANCE}
            ),
            "NlQuery": engine.record(
                "NlQuery",
                lambda params, principal: (
                    []
                    if "unknown" in params["text"]
                    else [["fact:issue-1", 0.9, PROVENANCE]]
                ),
            ),
        },
        monkeypatch,
        {"fixture-verified": caller(*SCOPES)},
    )
    synced = _call(
        app,
        "ingest.sources.sync",
        {"source_id": "recorded-connector", "mode": "incremental"},
        **{"Idempotency-Key": "sync-q1"},
    )
    assert _value(synced)["status"] == "queued"

    uql = _value(_call(app, "query.uql", {"text": "MATCH (n:Issue) RETURN n"}))
    assert uql["rows"] == [{"id": "fact:issue-1"}] and uql["provenance"] == PROVENANCE

    nl = _call(app, "query.nl", {"text": "which issues did the connector sync"})
    assert nl.status_code == 200, nl.text
    assert "fact:issue-1" in nl.text and "recorded-connector" in nl.text

    # No supporting fact: the engine answer returns unchanged; Arbora adds no text.
    empty = _call(app, "query.nl", {"text": "what is unknown to the graph"})
    assert empty.status_code == 200, empty.text
    assert "fact:" not in empty.text
    assert "provenance" not in empty.text
    assert [name for name, *_ in engine.calls] == ["Uql", "NlQuery", "NlQuery"]
    assert {principal for *_, principal in engine.calls} == {"person:one"}
