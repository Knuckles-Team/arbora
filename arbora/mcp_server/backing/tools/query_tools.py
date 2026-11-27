"""Auto-extracted arbora MCP tools: query_tools (register_query_tools).

Split out of runtime._build_server to deepen the MCP surface into focused
modules without changing tool behavior or names.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from arbora.mcp_server.backing.tools.catalog_query_tools import (
    register_catalog_query_tools,
)
from arbora.mcp_server.backing.tools.evidence_tools import register_evidence_tools
from arbora.mcp_server.backing.tools.graph_query_tools import register_graph_query_tools
from arbora.mcp_server.backing.tools.query_execution import (
    _reject_unsafe_table_sql,
)
from arbora.mcp_server.backing.tools.uql_tools import register_uql_tools
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


def _parse_ranges(spec: str) -> list[tuple[int, int]]:
    """Parse a char-range spec like '96..208,300..420' into (lo, hi) pairs.

    CONCEPT:MERISTEM-KG.retrieval.tree-navigation — the fetch half of map-then-fetch.
    """
    out: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ".." not in part:
            raise ValueError(f"invalid range {part!r}: use 'start..end'")
        lo_s, hi_s = part.split("..", 1)
        try:
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            raise ValueError("invalid range") from None
        if hi < lo:
            raise ValueError(f"invalid range {part!r}: end < start")
        out.append((lo, hi))
    if not out:
        raise ValueError("no ranges given (use 'start..end,start..end')")
    return out


def _persist_sections(
    engine: Any,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> bool:
    """Write section-tree nodes/edges through the engine's own API (CONCEPT:MERISTEM-KG.retrieval.section-tree).

    Uses ``engine.add_node(id, type, props)`` / ``engine.add_edge(src, tgt, rel)``
    — the same convenience write path ``write_ingest_tools`` uses — so the tree
    persists regardless of the backend's keyword-writer support. Best-effort.
    """
    ok = False
    for n in nodes:
        if _persist_section_node(engine, n):
            ok = True
    for e in edges:
        if _persist_section_edge(engine, e):
            ok = True
    return ok


def _persist_section_node(engine: Any, n: dict[str, Any]) -> bool:
    props = {k: v for k, v in n.items() if k not in ("id", "type")}
    try:
        engine.add_node(n["id"], n["type"], props)
        return True
    except Exception:
        logging.getLogger(__name__).debug("best-effort per node", exc_info=True)
        return False


def _persist_section_edge(engine: Any, e: dict[str, Any]) -> bool:
    props = {k: v for k, v in e.items() if k not in ("source", "target", "type")}
    try:
        engine.add_edge(e["source"], e["target"], e["type"], **props)
        return True
    except Exception:
        logging.getLogger(__name__).debug("best-effort per edge", exc_info=True)
        return False


def _graph_table_ingest(
    engine: Any,
    table_ingest: Any,
    source: str,
    table: str,
    config_json: str,
    limit: int,
    replace: bool,
) -> str:
    if not source:
        return json.dumps({"error": "ingest needs a source connector"})
    return json.dumps(
        table_ingest.ingest_connector_to_table(
            engine,
            str(source),
            table=table or None,
            config=json.loads(config_json) if config_json else None,
            limit=int(limit),
            replace=bool(replace),
        ),
        default=str,
    )


def _graph_table_rows(
    engine: Any, table_ingest: Any, table: str, rows_json: str, replace: bool
) -> str:
    if not table:
        return json.dumps({"error": "rows needs a table"})
    return json.dumps(
        table_ingest.ingest_rows_to_table(
            engine,
            str(table),
            json.loads(rows_json) if rows_json else [],
            replace=bool(replace),
        ),
        default=str,
    )


def _graph_table_create(
    engine: Any, table_ingest: Any, table: str, columns_json: str
) -> str:
    if not table:
        return json.dumps({"error": "create needs a table"})
    cols = json.loads(columns_json) if columns_json else []
    if not cols:
        return json.dumps({"error": "create needs columns_json"})
    return json.dumps(table_ingest.ensure_table(engine, str(table), cols), default=str)


def _graph_table_drop(engine: Any, table_ingest: Any, table: str) -> str:
    if not table:
        return json.dumps({"error": "drop needs a table"})
    return json.dumps(table_ingest.drop_table(engine, str(table)), default=str)


def _graph_table_query(engine: Any, sql: str) -> str:
    if not sql:
        return json.dumps({"error": "query needs a sql SELECT"})
    return _graph_table_query_checked(engine, sql)


def _graph_table_query_checked(engine: Any, sql: str) -> str:
    """The statement-shape-gated execute step (WD10-A-BACKEND security
    review) — split from :func:`_graph_table_query` so that pre-existing
    name's own complexity does not regress (`verify_both.py`)."""
    rejection = _reject_unsafe_table_sql(str(sql))
    if rejection is not None:
        return json.dumps({"error": rejection})
    return json.dumps(engine.sql(str(sql)), default=str)


# ══════════════════════════════════════════════════════════════════
# WD10-A-BACKEND — node-link JSON projection (wD10 Atlas backend gap B).
#
# No first-class {nodes, links} serialization exists today for a graph query
# RESULT (as opposed to `GraphComputeEngine.to_json()`'s whole-graph dump,
# meristem/knowledge_graph/core/graph_compute.py, which uses
# {"source","target","properties"} for the FULL graph, not a query
# projection). Every dialect (Cypher/SQL/SPARQL) can return dict-valued
# columns shaped like a node ({id, ...}) or a relationship
# ({source/start/..., target/end/..., type, ...}) depending on backend and
# RETURN clause; this projector is deliberately generic across that
# variance and NEVER fabricates a node/edge from a value it can't identify
# — a scalar/flat column contributes nothing (see class docstring).
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# WD10-A-BACKEND — unified capability/catalog introspection (wD10 Atlas
# backend gap A). Schema discovery is fragmented per protocol today (SQL via
# information_schema, GraphQL via live __schema, SPARQL via
# `SELECT DISTINCT ?type`, ontology via Method::OwlReason, KV only via
# `scan`, vector indexes with no listing method at all) — a frontend had to
# make 4-6 protocol-specific calls and guess. This assembles ONE
# capability-shaped response: every modality entry says whether it is
# available, and an ABSENT backend (a modality compiled out of this engine
# build) yields {"available": false, "reason": ...} rather than an error —
# matching the overstory `src/lib/gateway.ts` {ok, data, unavailable,
# error} envelope convention one layer up.
#
# Capability detection reuses `engine_tools.ENGINE_DOMAINS`
# (arbora/mcp_server/backing/tools/engine_tools/py, unowned this wave) — the SAME
# live introspection of the installed `medulla` client's sub-client
# classes every `engine_<domain>` tool is generated from (see that module's
# `_discover_domains()` docstring) — rather than re-deriving "what's
# compiled in" by hand. A domain absent from `ENGINE_DOMAINS` (its client
# class not present in the installed engine build) is reported unavailable;
# NEVER fabricated as live.
# ══════════════════════════════════════════════════════════════════


def register_query_tools(mcp, tools: ToolTable, engine_provider: Callable[[], Any]):
    """Register the query_tools group on the given FastMCP server."""

    register_graph_query_tools(mcp, tools, engine_provider)
    register_uql_tools(mcp, tools, engine_provider)
    register_catalog_query_tools(mcp, tools, engine_provider)
    register_evidence_tools(mcp, tools, engine_provider)
