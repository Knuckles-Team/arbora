"""Browser graph reads map onto existing operations (ARBORA-OPS-R904).

There is no ``graph.query`` operation and no operation takes Cypher text.
Each browser graph route maps to one served operation; this table is the map
of the row, and ``ENGINE_SCOPE_GAPS`` is its missing-construct list.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke.bootstrap import dispatch_eg_method
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import EgMethod
from arbora.api.registry.spec import PrincipalRule
from tests.api.test_query_ops import RecordingClient, context

# route -> (operation id, scope the row names)
BROWSER_GRAPH_MAP: dict[str, tuple[tuple[str, str], ...]] = {
    "GET /graph/nodes": (("eg.graph.GetNodesByLabel", "node:read"),),
    "GET /graph/relationships": (("eg.graph.GetEdgesPage", "edge:read"),),
    "GET /graph/stats": (
        ("eg.graph.NodeCount", "node:read"),
        ("eg.graph.EdgeCount", "edge:read"),
        ("eg.graph.Metrics", "service:control"),
    ),
    "GET /graph/graph3d/clusters": (
        ("eg.compute.ClusterHierarchyClusters", "compute:graph-read"),
    ),
    "GET /graph/graph3d/expand": (
        ("eg.compute.ClusterHierarchyExpand", "compute:graph-read"),
    ),
    "GET /graph/graph3d": (("eg.graph.GetSubgraph", "node:read"),),
    "GET /graph/impact/{symbol}": (
        ("eg.compute.GetBlastRadius", "compute:graph-read"),
    ),
    "GET /graph/node-types": (("query.uql", "query:unified"),),
    "GET /graph/search": (("retrieval.search", "memory:read"),),
    "POST /graph/viz/render": (("eg.ingestion.Viz", "viz:render"),),
    "POST /graph/query": (("query.uql", "query:unified"),),
}

# Engine methods whose scope class is service-only in the pinned engine
# contract, so a browser session cannot call them.  The pinned engine (3.3.0)
# serves every mapped method to a browser session, so the list is empty.
ENGINE_SCOPE_GAPS: frozenset[str] = frozenset()

# Methods the default engine build omits (a cargo feature); the route binds
# whenever the installed engine's catalog carries them.
FEATURE_GATED = frozenset({"eg.ingestion.Viz"})

pytestmark = pytest.mark.spec("ARBORA-OPS-R904")


def test_every_mapped_operation_exists_in_the_registry_with_the_scope_named() -> None:
    registry = get_registry()
    ids = {op.id for op in registry}
    assert "graph.query" not in ids
    for route, targets in BROWSER_GRAPH_MAP.items():
        for op_id, scope in targets:
            if op_id in FEATURE_GATED and op_id not in ids:
                continue
            assert op_id in ids, f"{route}: {op_id} is not served"
            op = registry[op_id]
            assert op.scopes == frozenset({scope}), f"{route}: {op_id} scope"
            browser_callable = op.principals is not PrincipalRule.SERVICE_ONLY
            assert browser_callable is (op_id not in ENGINE_SCOPE_GAPS), (
                f"{route}: {op_id} browser callability differs from the gap list"
            )


def test_no_operation_accepts_cypher_text() -> None:
    for op in get_registry():
        assert "cypher" not in op.id.lower()
        assert "cypher" not in op.summary.lower()


class _EngineReply(RecordingClient):
    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__()
        self.reply = reply

    async def _send(
        self, method: str, params: Any, graph: str, *, idempotency_key: Any = None
    ) -> Any:
        await super()._send(method, params, graph, idempotency_key=idempotency_key)
        return self.reply


@pytest.mark.spec("ARBORA-OPS-R904.1")
@pytest.mark.parametrize("method", ["Uql", "GetNodesByLabel", "GetEdgesPage"])
def test_degraded_graphs_and_truncation_pass_through(method: str) -> None:
    reply = {
        "rows": [{"id": "n1"}],
        "truncated": True,
        "next_cursor": "c2",
        "degraded_graphs": ["__commons__"],
    }
    client = _EngineReply(reply)
    result = asyncio.run(
        dispatch_eg_method(
            EgMethod(service=method, op=method), {"limit": 100}, context(client)
        )
    )
    assert result == reply
    assert "graph" not in client.sent[0][1]


class _EngineRefuses(RecordingClient):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__()
        self.refusal = (code, detail)

    async def _send(
        self, method: str, params: Any, graph: str, *, idempotency_key: Any = None
    ) -> Any:
        from medulla import EngineResponseError

        await super()._send(method, params, graph, idempotency_key=idempotency_key)
        raise EngineResponseError(*self.refusal)


@pytest.mark.spec("ARBORA-OPS-R904.1")
@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("UQL_UNEXPECTED_TOKEN", "expected one of: MATCH, TRAVERSE, RETURN"),
        ("UQL_BUDGET_EXCEEDED", "row budget 10000 exceeded"),
    ],
)
def test_uql_parse_error_lists_expected_tokens(code: str, detail: str) -> None:
    """The engine's parse and budget refusals reach the caller with their detail intact."""
    from medulla import EngineResponseError

    client = _EngineRefuses(code, detail)
    with pytest.raises(EngineResponseError) as refused:
        asyncio.run(
            dispatch_eg_method(
                EgMethod(service="Uql", op="Uql"), {"text": "MATCH ("}, context(client)
            )
        )
    assert refused.value.code == code
    assert detail in str(refused.value.detail)


#: The three consumer views, each with its UQL text and the columns it expects. The
#: overstory table ``UQL_VIEWS`` holds the same texts; an engine column the form cannot
#: produce yet is listed with the medulla row that owns it (ARBORA-OPS-R904.2).
UQL_VIEWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "GraphView.nodes": ("MATCH (n:Person) |> LIMIT 100", ("id", "label", "properties")),
    "GraphView.edges": (
        "TRAVERSE (a:Person)-[:knows]->(b) |> LIMIT 100",
        ("source", "target", "rel"),
    ),
    "TemporalGraphView.nodes": (
        "MATCH (n:Person) |> LIMIT 100 AS OF 2026-01-01T00:00:00Z",
        ("id", "label", "properties"),
    ),
    "TemporalGraphView.edges": (
        "TRAVERSE (a:Person)-[:knows]->(b) |> LIMIT 100 AS OF 2026-01-01T00:00:00Z",
        ("source", "target", "rel"),
    ),
}
UQL_MISSING_COLUMNS = {
    "per-label counts and degrees": "MEDULLA-FEDERATED-QUERY-R733",
    "stable paging (order and skip)": "MEDULLA-FEDERATED-QUERY-R734",
    "edge rows from an edge source": "MEDULLA-FEDERATED-QUERY-R735",
}


@pytest.mark.spec("ARBORA-OPS-R904.2")
@pytest.mark.parametrize("view", sorted(UQL_VIEWS))
def test_each_view_text_parses_with_the_engine_grammar_and_returns_the_expected_columns(
    view: str,
) -> None:
    """Each view text reaches the engine as UQL, never Cypher, and answers its columns."""
    text, columns = UQL_VIEWS[view]
    assert "RETURN" not in text and "MATCH (" in text or text.startswith("TRAVERSE")
    reply = {"columns": list(columns), "rows": [[f"{c}-1" for c in columns]]}
    client = _EngineReply(reply)
    result = asyncio.run(
        dispatch_eg_method(
            EgMethod(service="Uql", op="Uql"), {"text": text}, context(client)
        )
    )
    method, params, _graph = client.sent[0]
    assert method == "Uql" and params["text"] == text
    assert tuple(result["columns"]) == columns
    assert set(UQL_MISSING_COLUMNS.values()) == {
        "MEDULLA-FEDERATED-QUERY-R733",
        "MEDULLA-FEDERATED-QUERY-R734",
        "MEDULLA-FEDERATED-QUERY-R735",
    }
