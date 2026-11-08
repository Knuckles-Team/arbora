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
