"""``schema.context`` over HTTP and MCP against a recorded engine (ARBORA-DATA-MARKET-R005)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest

from tests.api._market_support import MarketServed, captured, plain  # noqa: F401
from tests.test_served_operation_api import _value, served  # noqa: F401

SCOPES = ("graph:read",)
BODY = {
    "source_id": "src-1",
    "schema": "sample_app",
    "object": "customer",
    "intent": "joins",
    "limit": 2,
}


def _answer(tenant: str = "tenant:one", **extra: Any) -> dict[str, Any]:
    return {
        "source_id": "src-1",
        "tenant_id": tenant,
        "object_id": "obj-customer",
        "catalog_revision": "rev-7",
        "as_of": "2026-11-01T00:00:00Z",
        "facts": [{"join": "order.customer_id"}] * 3,
        "proposals": [{"join": "invoice.customer_ref", "evidence": "name overlap"}],
        "dependency_edges": [{"to": "view.v_orders"}],
        "citations": ["rec-1"],
        "truncated": False,
        **extra,
    }


def _served(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
    scopes: Any = SCOPES,  # noqa: F811
) -> MarketServed:
    return MarketServed(
        captured, served, monkeypatch, {"SchemaContext": answer}, scopes=scopes
    )


@pytest.mark.spec("ARBORA-DATA-MARKET-R005")
def test_each_intent_over_http_and_mcp_and_unsupported_source(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    ask = _served(captured, served, monkeypatch, lambda _p, _u: _answer())
    for intent in ("definition", "joins", "ontology", "owner", "impact"):
        body = {**BODY, "intent": intent}
        value = _value(ask.http("schema.context", body))
        assert value["catalog_revision"] == "rev-7"
        # Bounded to the request limit, flagged, and an inferred join is a proposal.
        assert len(value["facts"]) == 2 and value["truncated"] is True
        assert value["proposals"][0]["status"] == "proposal"
        assert ask.mcp("schema.context", body)["ok"] is True
    sent = plain(ask.engine.calls[0][1])
    assert sent == {
        "intent": "definition", "object": "customer", "schema": "sample_app", "source_id": "src-1",
    }  # fmt: skip
    # An answer with no approved catalog revision is unavailable, never an empty graph.
    bare = _served(captured, served, monkeypatch, lambda _p, _u: {"source_id": "src-1"})
    refused = bare.http("schema.context", BODY)
    assert (
        refused.status_code == 503 and refused.json()["error"]["code"] == "UNAVAILABLE"
    )


@pytest.mark.spec("ARBORA-DATA-MARKET-R005")
def test_bad_requests_and_foreign_sources_make_no_engine_call_or_read_as_absent(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    other = _served(captured, served, monkeypatch, lambda _p, _u: _answer("tenant:two"))
    absent = other.http("schema.context", BODY)
    assert absent.status_code == 404 and absent.json()["error"]["code"] == "NOT_FOUND"
    before = len(other.engine.calls)
    for bad in (
        {**BODY, "intent": "everything"},
        {**BODY, "limit": 101},
        {**BODY, "depth": 4},
        {**BODY, "tenant": "tenant:two"},
        {**BODY, "extra": 1},
    ):
        assert other.http("schema.context", bad).status_code == 400
    assert len(other.engine.calls) == before
    denied = _served(captured, served, monkeypatch, lambda _p, _u: _answer(), scopes=())
    assert denied.http("schema.context", BODY).status_code == 403
    assert denied.engine.calls == []


@pytest.mark.spec("ARBORA-DATA-MARKET-R005")
def test_an_engine_without_the_method_refuses_unavailable(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import medulla.generated as generated
    from medulla.generated import _runtime

    ask = _served(captured, served, monkeypatch, {})
    kept = frozenset(generated.METHOD_IDS) - {"SchemaContext"}
    monkeypatch.setattr(generated, "METHOD_IDS", kept)
    monkeypatch.setattr(_runtime, "METHOD_IDS", kept)
    refused = ask.http("schema.context", BODY)
    assert (
        refused.status_code == 503 and refused.json()["error"]["code"] == "UNAVAILABLE"
    )
    assert ask.engine.calls == []
