"""Query families served over HTTP and MCP against a recording engine (ARBORA-OPS-R018.2)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from arbora.security import request_identity
from tests.api._served_support import McpAsk, RecordingEngine, route_methods
from tests.api._served_support import person as _person
from tests.test_served_operation_api import (  # noqa: F401
    _call,
    _value,
    served,
)

EXTRA = ("query:sql", "query:nl", "timeseries:read", "compute:semantic")


def _caller(*scopes: str) -> Any:
    one = _person("one")
    kept = tuple(r for r in one.roles if r not in EXTRA)
    return replace(one, roles=(*kept, *scopes))


Engine = RecordingEngine


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the composed invocation (request before ``served``)."""
    import tests.test_served_operation_api as harness

    held: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def capturing(**kwargs: Any) -> Any:
        held["invocation"] = compose(**kwargs)
        return held["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", capturing)
    return held


def _route(client: Any, monkeypatch: pytest.MonkeyPatch, *scopes: str) -> Engine:
    engine = Engine()
    route_methods(
        client,
        {
            "Sql": engine.record("Sql", {"rows": [{"n": 1}]}),
            "TsRange": engine.record("TsRange", [[1, 2.0]]),
        },
        monkeypatch,
        {"fixture-verified": _caller(*scopes)},
    )
    return engine


@pytest.mark.spec("ARBORA-OPS-R018.2")
def test_uql_sql_series_and_index_ops_reach_engine(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = _route(client, monkeypatch, *EXTRA)
    uql = _value(_call(app, "query.uql", {"text": "MATCH (n) RETURN n"}))
    assert uql == {"rows": [{"id": "node:one"}]}
    assert [m for m, *_ in client.sent if m == "Uql"] == ["Uql"]
    sql = _call(app, "query.sql", {"query": "SELECT 1 AS n"})
    assert sql.status_code == 200, sql.text
    series = _call(
        app,
        "eg.storage.TsRange",
        {"series_id": "series:cpu", "from": 0, "to": 10},
    )
    assert series.status_code == 200, series.text
    # The semantic search method is service-only: a person never reaches it.
    index = _call(
        app,
        "eg.ingestion.SemanticSearch",
        {"query_embedding": [0.1, 0.2], "n_results": 3},
    )
    assert index.status_code == 403
    assert index.json()["error"]["code"] == "PRINCIPAL_NOT_ALLOWED"
    assert [name for name, *_ in engine.calls] == ["Sql", "TsRange"]
    # Every call runs as the verified person, never as a request-chosen identity.
    assert {principal for *_, principal in engine.calls} == {"person:one"}
    # Arbora adds no tenant or budget field the caller did not send.
    assert engine.calls[1][1] == {"series_id": "series:cpu", "from": 0, "to": 10}


@pytest.mark.spec("ARBORA-OPS-R018.2")
def test_query_ops_are_served_over_mcp(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.api._served_support as support

    _app, client = served
    engine = _route(client, monkeypatch, *EXTRA)
    monkeypatch.setattr(support, "person", lambda *_a, **_k: _caller(*EXTRA))
    ask = McpAsk(stack["invocation"], monkeypatch)
    reply = ask("ask", "query.sql", {"query": "SELECT 1 AS n"})
    assert reply["ok"] is True, reply
    assert [name for name, *_ in engine.calls] == ["Sql"]


@pytest.mark.spec("ARBORA-OPS-R018.2.2")
def test_missing_scope_refuses_before_any_engine_call(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served

    async def credential_owner(_credential: str) -> Any:
        return _caller()

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    refused = _call(app, "query.nl", {"text": "which deployments failed"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "SCOPE_REQUIRED"
    assert client.sent == []


def _route_extra(
    client: Any, monkeypatch: pytest.MonkeyPatch, handlers: dict[str, Any], *scopes: str
) -> Engine:
    engine = Engine()
    route_methods(
        client,
        {name: engine.record(name, answer) for name, answer in handlers.items()},
        monkeypatch,
        {"fixture-verified": _caller(*scopes)},
    )
    return engine


SOURCE = {
    "source_id": "operator",
    "origin": "operator",
    "shapes_sha256": None,
    "ontology_sha256": "ab" * 32,
    "shapes_bytes": 0,
    "ontology_bytes": 12,
    "attached_at_ms": 5,
}
SOURCES = {
    "schema_version": 1,
    "graph": "tenant:one",
    "core_catalog_digest": "c0",
    "composed_digest": "c1",
    "core_sources": [],
    "dynamic_sources": [SOURCE],
}
DOCUMENTS = {
    "schema_version": 1,
    "graph": "tenant:one",
    "composed_digest": "c1",
    "source": SOURCE,
    "ontology_ttl": "@prefix : <x#> .",
}


@pytest.mark.spec("ARBORA-OPS-R018.1")
def test_schema_list_and_get_reach_engine_under_owl_read(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = _route_extra(
        client,
        monkeypatch,
        {"GraphSchemaList": SOURCES, "GraphSchemaGet": DOCUMENTS},
        "owl:read",
    )
    listed = _value(_call(app, "ontology.schema.list", {}))
    assert listed == SOURCES
    got = _value(_call(app, "ontology.schema.graph", {"source_id": "operator"}))
    assert got == DOCUMENTS
    assert [(n, p) for n, p, _ in engine.calls] == [
        ("GraphSchemaList", {}),
        ("GraphSchemaGet", {"source_id": "operator"}),
    ]
    assert {principal for *_, principal in engine.calls} == {"person:one"}
    # The view reaches the caller as the engine sent it: no importer or lease key.
    assert "importer" not in str(listed) and "lease" not in str(got)


@pytest.mark.spec("ARBORA-OPS-R018.1")
def test_schema_reads_refuse_without_owl_read_before_the_engine(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served

    async def credential_owner(_credential: str) -> Any:
        return _caller()

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    for op, params in (
        ("ontology.schema.list", {}),
        ("ontology.schema.graph", {"source_id": "operator"}),
    ):
        refused = _call(app, op, params)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "SCOPE_REQUIRED"
    assert client.sent == []


@pytest.mark.spec("ARBORA-OPS-R018.1")
def test_schema_get_is_unavailable_on_an_engine_without_the_method(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    app, client = served
    _route_extra(client, monkeypatch, {"GraphSchemaList": SOURCES}, "owl:read")
    declared = frozenset(generated.METHOD_IDS) - {"GraphSchemaGet"}
    monkeypatch.setattr(generated, "METHOD_IDS", declared)
    refused = _call(app, "ontology.schema.graph", {"source_id": "operator"})
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "UNAVAILABLE"


BUDGET = {"max_rows": 10_000_000, "max_visits": 10_000_000, "max_cost_units": 10**12}
COST = {
    "applied_budget": {"max_rows": 100, "max_visits": 200, "max_cost_units": 300},
    "authorization_revision": 1,
    "cost_used": 3,
    "operation_ids": [],
    "premises": [],
    "remaining": {"max_rows": 97, "max_visits": 197, "max_cost_units": 297},
    "rows_visited": 3,
    "visits": 3,
}


@pytest.mark.spec("ARBORA-OPS-R018.2.2")
def test_over_ceiling_budget_reaches_engine_unchanged(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    answer = {"rows": [{"id": "node:one"}], "cost": COST}
    engine = _route_extra(client, monkeypatch, {"Uql": answer}, "query:unified")
    reply = _call(app, "query.uql", {"text": "MATCH (n) RETURN n", "budget": BUDGET})
    assert reply.status_code == 200, reply.text
    # The engine clamps; Arbora forwards the caller's budget as sent.
    assert engine.calls[0][1]["budget"] == BUDGET
    # The applied budget is returned as the engine reported it.
    assert _value(reply)["cost"]["applied_budget"] == COST["applied_budget"]
    # No budget sent means none added.
    _call(app, "query.uql", {"text": "MATCH (n) RETURN n"})
    assert "budget" not in engine.calls[1][1]


@pytest.mark.spec("ARBORA-OPS-R018.2.2")
def test_exhausted_budget_refusal_keeps_the_engine_code(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla import EngineResponseError

    app, client = served

    def exhausted(_params: Any, _principal: str) -> Any:
        raise EngineResponseError(
            "INDEXLESS_BUDGET_EXHAUSTED", f"INDEXLESS_BUDGET_EXHAUSTED: {COST}"
        )

    route_methods(
        client,
        {"Uql": exhausted},
        monkeypatch,
        {"fixture-verified": _caller("query:unified")},
    )
    refused = _call(app, "query.uql", {"text": "MATCH (n) RETURN n", "budget": BUDGET})
    assert refused.json()["error"]["code"] == "INDEXLESS_BUDGET_EXHAUSTED", refused.text


@pytest.mark.spec("ARBORA-OPS-R018.2.2")
def test_unavailable_planner_refuses_without_fallback(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla import EngineResponseError

    app, client = served

    def unconfigured(_params: Any, _principal: str) -> Any:
        raise EngineResponseError(
            "NL_PLANNER_UNCONFIGURED", "NL_PLANNER_UNCONFIGURED: no planner"
        )

    route_methods(
        client,
        {"NlQuery": unconfigured},
        monkeypatch,
        {"fixture-verified": _caller("query:nl")},
    )
    refused = _call(app, "query.nl", {"text": "which deployments failed"})
    assert refused.json()["error"]["code"] == "UNAVAILABLE", refused.text
    assert refused.status_code == 503
