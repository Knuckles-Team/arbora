"""Trust flags, curation review and the glossary hot path served over HTTP and MCP.

ARBORA-OPS-R855.2 / R855.3: the steward role is the engine's ``governance`` answer,
never an Arbora table; a refused review reaches the engine with zero writes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.api._served_support import (
    McpAsk,
    RecordingEngine,
    confirmable_invocation,
    person,
)
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

WRITE = ("kg:write", "kg:read", "query:nl", "query:unified", "mcp:discover")
TARGET = "term:margin"
PROPOSAL: dict[str, Any] = {
    "id": "proposal:1",
    "kind": "description",
    "target": TARGET,
    "state": "proposed",
    "opened_by": "person:two",
    "text": "Revenue less cost of sales",
}
CHANGE: dict[str, Any] = {
    "id": "cr:9",
    "kind": "change_request",
    "target": TARGET,
    "state": "open",
    "reviewers": ["person:one"],
}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    return confirmable_invocation(monkeypatch)


def _who(name: str, *scopes: str) -> Any:
    one = person(name)
    return replace(one, roles=(*one.roles, *scopes))


class Glossary:
    """The engine's glossary state: proposals, governance and approved terms."""

    def __init__(self, stewards: list[str]) -> None:
        self.stewards = stewards
        self.proposals: list[dict[str, Any]] = [dict(PROPOSAL), dict(CHANGE)]
        self.terms: list[dict[str, Any]] = []

    def catalog_read(self, params: dict[str, Any], _principal: str) -> Any:
        op = params["op"]
        if op["op"] == "governance":
            return {
                "kind_counts": [],
                "rows": [],
                "governance": {"owners": [], "stewards": self.stewards},
            }
        rows = [
            r
            for r in self.proposals
            if op.get("target") in (None, r["target"])
            and op.get("state") in (None, r["state"])
        ]
        return {"kind_counts": [], "rows": [], "proposals": rows}

    def glossary(self, params: dict[str, Any], _principal: str) -> Any:
        op = params["op"]
        if op["op"] == "propose_description":
            self.proposals.append({**PROPOSAL, "id": "proposal:new"})
            return {"iri": TARGET, "written": 1}
        assert op["op"] == "review_description"
        for row in self.proposals:
            if row["id"] == op["proposal"]:
                row["state"] = "approved" if op["approve"] else "rejected"
                if op["approve"]:
                    self.terms.append({"asset": TARGET, "name": "Margin"})
        return {"iri": TARGET, "status": "approved", "written": 1}

    def nl_query(self, params: dict[str, Any], _principal: str) -> Any:
        return {"rows": list(self.terms)}


def _serve(client: Any, monkeypatch: pytest.MonkeyPatch, state: Glossary, who: Any):
    engine = RecordingEngine()
    handlers = {
        "CatalogRead": state.catalog_read,
        "Glossary": state.glossary,
        "NlQuery": state.nl_query,
        "Stewardship": lambda p, _: {
            "id": p["op"].get("request_id", "flag"),
            "owners": [],
            "stewards": state.stewards,
        },
    }
    return engine.route(client, monkeypatch, handlers, {"fixture-verified": who})


@pytest.mark.spec("ARBORA-OPS-R855.3")
def test_trust_flag_and_proposal_review(
    stack: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    state = Glossary(stewards=["person:one"])
    engine = _serve(client, monkeypatch, state, _who("one", *WRITE))
    flagged = _call(
        app,
        "catalog.trust_flag.set",
        {"asset": "table:orders", "flag": "deprecation", "reason": "replaced"},
        **{"Idempotency-Key": "flag-1"},
    )
    assert flagged.status_code == 200, flagged.text
    stewardship = next(p for n, p, _ in engine.calls if n == "Stewardship")
    assert stewardship == {
        "op": {
            "op": "set_trust_flag",
            "asset": "table:orders",
            "flag": "deprecation",
            "reason": "replaced",
        }
    }
    listed = _value(_call(app, "curation.proposals.list", {"target": TARGET}))
    assert [row["id"] for row in listed["proposals"]] == ["proposal:1", "cr:9"]
    # A change request is reviewed through Stewardship, a description through Glossary.
    for proposal, method in (("cr:9", "Stewardship"), ("proposal:1", "Glossary")):
        before = engine.names().count(method)
        reply = _call(
            app,
            "curation.proposals.review",
            {"target": TARGET, "proposal_id": proposal, "approve": True},
            **{"Idempotency-Key": f"review-{proposal}"},
        )
        assert reply.status_code == 200, reply.text
        assert engine.names().count(method) == before + 1
    # An unknown proposal is refused before any write.
    missing = _call(
        app,
        "curation.proposals.review",
        {"target": TARGET, "proposal_id": "nope", "approve": True},
        **{"Idempotency-Key": "review-nope"},
    )
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert {principal for *_, principal in engine.calls} == {"person:one"}


@pytest.mark.spec("ARBORA-OPS-R855.3")
def test_trust_flag_refuses_an_unknown_flag_and_a_long_reason(
    stack: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = _serve(client, monkeypatch, Glossary(["person:one"]), _who("one", *WRITE))
    for body in (
        {"asset": "a", "flag": "gold", "reason": "x"},
        {"asset": "a", "flag": "warning", "reason": "x" * 501},
        {"asset": "a", "flag": "warning", "reason": ""},
    ):
        refused = _call(app, "catalog.trust_flag.set", body, **{"Idempotency-Key": "k"})
        assert refused.status_code == 400, refused.text
    assert engine.calls == []


@pytest.mark.spec("ARBORA-OPS-R855.2")
def test_term_review_and_search(
    stack: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    state = Glossary(stewards=["person:one"])
    engine = _serve(client, monkeypatch, state, _who("one", *WRITE))
    # The proposer wrote the description through the engine's proposal write.
    import asyncio

    asyncio.run(
        client._send(
            "Glossary", {"op": {"op": "propose_description", "proposal": {}}}, "g"
        )
    )
    reviewed = _call(
        app,
        "curation.proposals.review",
        {"target": TARGET, "proposal_id": "proposal:1", "approve": True},
        **{"Idempotency-Key": "r1"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert state.terms == [{"asset": TARGET, "name": "Margin"}]
    # The same journey over MCP finds the approved term with ``ask`` routed to query.nl.
    import tests.api._served_support as support

    monkeypatch.setattr(support, "person", lambda *_a, **_k: _who("one", *WRITE))
    ask = McpAsk(stack["invocation"], monkeypatch)
    found = ask("ask", "", {}, intent="which glossary terms exist for margin")
    assert found["ok"] is True, found
    assert found["meta"]["resolved_op"] == "query.nl"
    assert "Margin" in str(found["result"])
    assert "NlQuery" in engine.names()


@pytest.mark.spec("ARBORA-OPS-R855.2")
def test_a_non_steward_review_is_refused_with_zero_engine_writes(
    stack: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    state = Glossary(stewards=["person:other"])
    engine = _serve(client, monkeypatch, state, _who("one", *WRITE))
    refused = _call(
        app,
        "curation.proposals.review",
        {"target": TARGET, "proposal_id": "proposal:1", "approve": True},
        **{"Idempotency-Key": "r2"},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "SUBJECT_ACCESS_DENIED"
    assert "Glossary" not in engine.names() and "Stewardship" not in engine.names()
    assert state.proposals[0]["state"] == "proposed"
