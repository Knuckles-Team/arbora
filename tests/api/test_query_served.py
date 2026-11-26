"""Query families served over HTTP and MCP against a recording engine (ARBORA-OPS-R018.2)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from arbora.security import request_identity
from tests.api._served_support import McpAsk, route_methods
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


class Engine:
    """Records every method with the verified principal; answers canned rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str]] = []

    def record(self, name: str, answer: Any) -> Any:
        def handle(params: Any, principal: str) -> Any:
            self.calls.append((name, params, principal))
            return answer

        return handle


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
