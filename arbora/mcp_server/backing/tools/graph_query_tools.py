"""The graph query, tabular query and projection tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import Field

from arbora.api.errors import not_served
from arbora.api.evidence import EvidenceBundle
from arbora.mcp_server.backing.tools.query_execution import (
    _parse_projection_request,
    _project_node_link,
    _projection_rows,
    _run_graph_query_federated,
    _run_graph_query_local,
    _run_graph_query_normalize_for_scope,
    _run_graph_query_sparql,
    _run_graph_query_sql,
    _run_graph_query_uql,
)
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


def register_graph_query_tools(
    mcp, tools: ToolTable, engine_provider: Callable[[], Any]
) -> None:
    """Register the graph_query_tools tools and their REST twins."""

    def _run_graph_query(
        query: str = Field(
            description=(
                "A read-only query string. scope='local' expects Cypher; scope='uql' "
                "expects a bounded UQL pipeline ending in LIMIT 1..1000; "
                "scope='sql'/'sparql' use their respective dialect. UQL uses the "
                "query text only; leave params as '{}'."
            )
        ),
        params: str = Field(default="{}", description="JSON-encoded query parameters."),
        scope: str = Field(
            default="local",
            description=(
                "'local' for the internal KG (Cypher), 'sql' to run read-only SQL over the "
                "KG + user tables via the engine's DataFusion surface (e.g. SELECT ... FROM "
                "nodes — CONCEPT:MERISTEM-KG.query.read-only-sql-over, same path as the pg-wire listener), 'sparql' to "
                "run a SPARQL 1.1 SELECT/ASK over the engine's RDF projection of the graph "
                "(CONCEPT:MERISTEM-KG.ingest.mirror-inbound), 'uql' to run a bounded, read-only "
                "Unified Query Language pipeline through the governed engine surface, or "
                "'federated' to query an external graph endpoint. For 'sql'/'sparql'/'uql' "
                "the `query` arg carries the selected dialect's query string; UQL must "
                "end in LIMIT 1..1000 and uses params='{}'."
            ),
        ),
        reference_id: str = Field(
            default="",
            description="Required when scope='federated'. The ExternalGraphReference node ID.",
        ),
        as_of: str = Field(
            default="",
            description=(
                "CONCEPT:MERISTEM-KG.query.as-of-instant-filter — optional ISO-8601 instant. When set, rows are filtered to "
                "those whose bi-temporal validity (valid_from <= as_of < valid_to) holds, "
                "answering 'what was true as of date T'. Not supported for scope='uql'; "
                "put temporal semantics in the native UQL pipeline instead."
            ),
        ),
        connection: str = Field(
            default="",
            description=(
                "CONCEPT:MERISTEM-KG.backend.multi-connection-registry — named BACKEND connection to query "
                "(default = primary). Use a registered connection name (e.g. "
                "'prod-neo4j'), or 'all' (or a comma-separated list) to fan out the "
                "same query to several backends and get per-connection labeled "
                "results. This selects WHICH BACKEND, never which physical graph "
                "within it — see `graph` for that."
            ),
        ),
        graph: str = Field(
            default="",
            description=(
                "CONCEPT:MERISTEM-KG.backend.explicit-graph-selection — explicit PHYSICAL engine graph to query "
                "(one of the names `engine_tenants(action='list')`/the engine's own "
                "ListGraphs returns), independent of `connection`. Empty = the "
                "caller's own bound graph (unchanged default behavior). Requires "
                "exactly one resolved `connection` (the default one) — never "
                "combinable with `connection='all'`/a list. Authorization-checked "
                "by the engine's own RBAC/RLS on every call; an unknown graph, or "
                "a `connection` with no physical-graph concept, is a typed error — "
                "never a silent fallback to a default graph and never a union "
                "across graphs. Every response echoes the `connection`/`graph` "
                "actually used."
            ),
        ),
        include_epistemic: bool = Field(
            default=False,
            description=(
                "CONCEPT:MERISTEM-KB-CURRENCY — opt-in for local Cypher or UQL "
                "(ignored on scope='sql'/'sparql'/'federated'). When true, each "
                "result row is currency-upgraded via the "
                "engine's `explain_provenance_by_ids` into a per-row epistemic "
                "envelope — confidence, bitemporal valid/tx time, evidence "
                "provenance, policy labels — alongside the row's own properties "
                "(never fabricated, resolved server-side). The widened rows are "
                "projected into the same EvidenceBundle response. Degrades to an empty "
                "result when the "
                "connected backend has no epistemic primitive."
            ),
        ),
    ) -> str:
        """Execute a read-only graph query against the Knowledge Graph.

        ``scope='local'`` uses Cypher and ``scope='uql'`` uses the bounded
        native Unified Query Language surface; SQL, SPARQL, and federated
        scopes retain their existing dialect-specific routes.
        """
        # A direct call bypassing `_execute_tool` (which resolves `Field`
        # defaults) binds an omitted `graph` to its raw, truthy
        # `pydantic.fields.FieldInfo` rather than `""` — normalize once here so
        # every `if graph`/`resolve_explicit_graph`/`bound_to_graph` use below
        # sees a clean string, mirroring the SAME defensiveness
        # `ConnectionRegistry.resolve_names` already applies to `connection`.
        normalized = _run_graph_query_normalize_for_scope(
            graph, params, include_epistemic, scope
        )
        if isinstance(normalized, str):
            return normalized
        graph, parsed_params, include_epistemic_flag = normalized

        if scope == "uql":
            return _run_graph_query_uql(
                query,
                connection,
                graph,
                include_epistemic_flag,
                parsed_params,
                as_of,
            )

        if scope == "sql":
            # CONCEPT:MERISTEM-KG.query.read-only-sql-over — read-only SQL over the KG via the engine's
            # DataFusion surface (the same path the pg-wire listener uses). The
            # `query` arg carries the SQL string. RLS-governed + read-path-first
            # (engine.sql refuses non-SELECT). Honors `connection` fan-out like
            # Cypher; `graph` (CONCEPT:MERISTEM-KG.backend.explicit-graph-selection) selects a physical engine
            # graph, independent of `connection` — see `resolve_explicit_graph`.
            return _run_graph_query_sql(query, connection, graph, as_of)

        if scope == "sparql":
            # CONCEPT:MERISTEM-KG.ingest.mirror-inbound — SPARQL 1.1 (SELECT/ASK/CONSTRUCT/DESCRIBE) over the
            # engine's RDF projection of the live graph. The `query` arg carries the
            # SPARQL string. RLS-governed (engine.sparql visibility-filters rows) and
            # honors `connection` fan-out like Cypher/SQL; `graph` selects a physical
            # engine graph, independent of `connection`.
            return _run_graph_query_sparql(query, connection, graph)

        if scope == "federated":
            return _run_graph_query_federated(
                query, reference_id, parsed_params, engine_provider
            )

        # Local reads use each backend's server-enforced read-only transaction.
        # The native engine requires an explicit read mode and validates it with
        # the complete parser; external backends without an equivalent contract
        # fail closed. No lexical query filter is an authorization boundary.
        return _run_graph_query_local(
            query,
            parsed_params,
            as_of,
            include_epistemic,
            include_epistemic_flag,
            connection,
            graph,
            engine_provider,
        )

    @mcp.tool(
        name="graph_query",
        description=(
            "Execute a read-only Cypher, bounded UQL, SQL, SPARQL, or federated graph query and "
            "return the sole typed EvidenceBundle response."
        ),
        tags=["arbora", "query"],
    )
    def graph_query(
        query: str = Field(
            description=(
                "A read-only query string; scope='local' expects Cypher and "
                "scope='uql' expects a bounded UQL pipeline ending in LIMIT 1..1000 "
                "with params='{}'."
            )
        ),
        params: str = Field(default="{}", description="JSON-encoded query parameters."),
        scope: str = Field(
            default="local",
            description="local | uql | sql | sparql | federated",
        ),
        reference_id: str = Field(
            default="",
            description="ExternalGraphReference id required for federated queries.",
        ),
        as_of: str = Field(
            default="",
            description=(
                "Optional ISO-8601 bitemporal query instant; rejected for "
                "scope='uql' because the native UQL path cannot honor this "
                "top-level filter."
            ),
        ),
        connection: str = Field(
            default="",
            description=(
                "Named BACKEND connection, 'all', or a connection list. Selects "
                "WHICH BACKEND, never which physical graph — see `graph`."
            ),
        ),
        graph: str = Field(
            default="",
            description=(
                "CONCEPT:MERISTEM-KG.backend.explicit-graph-selection — explicit physical engine graph (see "
                "`engine_tenants(action='list')`), independent of `connection`. "
                "Empty = the caller's own bound graph. Fail-closed: an unknown "
                "graph or a `connection`/fan-out that has no physical-graph "
                "concept is a typed error, never a silent default/union. Echoed "
                "back in the response alongside `connection`."
            ),
        ),
        include_epistemic: bool = Field(
            default=False,
            description="Resolve per-row epistemic data before building the bundle.",
        ),
    ) -> EvidenceBundle:
        raw = _run_graph_query(
            query=query,
            params=params,
            scope=scope,
            reference_id=reference_id,
            as_of=as_of,
            connection=connection,
            graph=graph,
            include_epistemic=include_epistemic,
        )
        return EvidenceBundle.from_payload(raw, operation="graph_query")

    tools["graph_query"] = graph_query

    @mcp.tool(
        name="tabular_query",
        description=(
            "Execute a read-only SQL projection through the governed tabular "
            "query service and return its typed evidence bundle."
        ),
        tags=["arbora", "query", "tabular"],
    )
    def tabular_query(
        sql: str = Field(description="A non-empty read-only SQL statement."),
    ) -> EvidenceBundle:
        raise not_served(
            "knowledge_graph.core.tabular_query_service",
            "MEDULLA-UNIFIED-DATA-PLANE-R612",
        )

    tools["tabular_query"] = tabular_query

    # ══════════════════════════════════════════════════════════════════
    # 1a-quater. graph_projection — CONCEPT:MERISTEM-KG.query.node-link-projection
    # (wD10 Atlas backend gap B): a stable {nodes, links} JSON shape for a
    # graph query result, so the overstory data explorer's 2D/3D renderers
    # never have to re-derive node/edge shape from raw per-dialect rows.
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_projection",
        description=(
            "CONCEPT:MERISTEM-KG.query.node-link-projection — run the SAME query `graph_query` would "
            "and project the result into a stable node-link JSON shape: "
            "{nodes:[{id,label,properties}], links:[{source,target,label,properties}]} — "
            "the D3/force-graph convention every 2D/3D graph renderer already speaks. "
            "A dict-valued result column shaped like a node ({id, ...}) becomes a node; "
            "one shaped like a relationship ({source/start/..., target/end/..., type, ...}) "
            "becomes a link; a plain scalar column is never fabricated into either. "
            "Nodes are de-duplicated by id across all rows. Takes ONE JSON request "
            "object (project rule, wD10 preamble addendum — no new wide MCP-tool "
            "signatures) rather than one Field per argument: request_json is "
            "{query (required), params, scope, reference_id, as_of, connection, "
            "graph} — identical fields/semantics to graph_query's own arguments."
        ),
        tags=["arbora", "query", "visualization"],
    )
    def graph_projection(
        request_json: str = Field(
            description=(
                "JSON object: {query (required), params, scope, reference_id, "
                "as_of, connection, graph} — same fields/semantics as graph_query's "
                "individual arguments."
            )
        ),
    ) -> str:
        run_kwargs, request_error = _parse_projection_request(request_json)
        if request_error is not None:
            return json.dumps(
                {"error": {"code": "invalid_request", "message": request_error}}
            )
        raw = _run_graph_query(**run_kwargs, include_epistemic=False)
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return json.dumps({"error": {"code": "operation_failed"}})
        if isinstance(payload, dict) and payload.get("error"):
            return json.dumps({"error": payload["error"]})
        projection = _project_node_link(_projection_rows(payload))
        projection["connection"] = run_kwargs["connection"] or "default"
        projection["graph"] = run_kwargs["graph"]
        return json.dumps(projection, default=str)

    tools["graph_projection"] = graph_projection

    tools.bind_route("graph_projection", "/graph/projection")
