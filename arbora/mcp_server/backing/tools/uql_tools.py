"""The natural-language and UQL question tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import Field

from arbora.api.errors import not_served
from arbora.api.evidence import EvidenceBundle
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.error_surface import public_error_json

logger = logging.getLogger(__name__)


def _refuse_nl_option(message: str, operation: str) -> EvidenceBundle:
    """Typed refusal for a natural-language option the engine planner lacks."""
    return EvidenceBundle.from_payload(
        public_error_json(ValueError(message), code="invalid_request"),
        operation=operation,
    )


def _engine_nl_query(text: str, *, limit: int, operation: str) -> EvidenceBundle:
    """Answer ``text`` through medulla's NL planner, never a local one.

    Arbora has no natural-language planner of its own: the request goes
    through the governed ``engine_query`` dispatch to EG
    ``QueryClient.nl_query``, whose planner emits UQL that the engine executes
    on its deterministic UQL pipeline (``ARBORA-HOST-R001.8``).
    """
    from arbora.mcp_server.backing.tools import engine_tools

    methods = set(engine_tools.ENGINE_DOMAINS.get("query", ()))
    if "nl_query" not in methods:
        return EvidenceBundle.from_payload(
            public_error_json(
                RuntimeError("the installed medulla has no nl_query planner"),
                code="dependency_unavailable",
            ),
            operation=operation,
        )
    raw = engine_tools._dispatch(
        "query", methods, "nl_query", json.dumps({"text": str(text)}), ""
    )
    decoded = json.loads(raw)
    if isinstance(decoded, dict):
        return EvidenceBundle.from_payload(raw, operation=operation)
    rows = [row for row in decoded or [] if isinstance(row, dict)]
    bounded = rows[: max(int(limit), 0)]
    return EvidenceBundle.from_nl_query(
        {
            "request": str(text),
            "planner": "medulla",
            "dialect": "uql",
            "results": bounded,
            "row_count": len(bounded),
            "citations": [str(row["id"]) for row in bounded if "id" in row],
        }
    )


def register_uql_tools(
    mcp, tools: ToolTable, engine_provider: Callable[[], Any]
) -> None:
    """Register the uql_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # 1a-bis. graph_ask — CONCEPT:MERISTEM-KG.ingest.mirror-inbound natural-language → query
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_ask",
        description=(
            "CONCEPT:MERISTEM-KG.ingest.mirror-inbound — ask the Knowledge Graph in plain English. "
            "medulla's own NL planner (EG QueryClient.nl_query) turns the question "
            "into one UQL query and runs it on the engine's deterministic UQL pipeline; "
            "Arbora runs no local planner. Returns the result rows and citations (row ids). "
            "dialect must be 'auto'; execute=false and include_epistemic=true are refused "
            "because the engine planner returns rows only."
        ),
        tags=["arbora", "query", "nl"],
    )
    def graph_ask(
        question: str = Field(description="The natural-language question to answer."),
        dialect: str = Field(
            default="auto",
            description="'auto' (model chooses) or 'cypher'|'sql'|'sparql' to force one.",
        ),
        execute: bool = Field(
            default=True,
            description="When false, return only the generated query (preview/dry-run).",
        ),
        limit: int = Field(default=50, description="Max result rows to return."),
        include_epistemic: bool = Field(
            default=False,
            description=(
                "CONCEPT:MERISTEM-KB-CURRENCY — opt-in. Only takes effect when the "
                "generated (or forced) query resolves to the 'cypher' dialect "
                "(sql/sparql have no epistemic-envelope surface, so this is a "
                "silent no-op for those). When true and honored, `results` holds "
                "per-row epistemic envelopes (confidence, bitemporal valid/tx "
                "time, evidence provenance, policy labels) instead of plain rows, "
                "and `citations` degrades to an empty list."
            ),
        ),
    ) -> EvidenceBundle:
        if str(dialect) != "auto":
            return _refuse_nl_option(
                "the engine NL planner always plans UQL; dialect must be 'auto'",
                "graph_ask",
            )
        if not bool(execute):
            return _refuse_nl_option(
                "the engine NL planner returns rows only; preview is unavailable",
                "graph_ask",
            )
        if bool(include_epistemic):
            return _refuse_nl_option(
                "epistemic envelopes are unavailable for engine-planned queries",
                "graph_ask",
            )
        try:
            return _engine_nl_query(question, limit=limit, operation="graph_ask")
        except Exception as e:
            logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
            return EvidenceBundle.from_payload(
                public_error_json(e), operation="graph_ask"
            )

    tools["graph_ask"] = graph_ask

    # ══════════════════════════════════════════════════════════════════
    # 1a-quater. nl_query — CONCEPT:MERISTEM-KG.query.ask-gateway-rest-twin Meristem-fleet-LLM as the engine's NL planner
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="nl_query",
        description=(
            "CONCEPT:MERISTEM-KG.query.ask-gateway-rest-twin — ask the Knowledge Graph in plain "
            "English through medulla's NL planner (EG QueryClient.nl_query, via the "
            "governed engine_query dispatch). The engine planner emits UQL and executes it on "
            "its deterministic UQL pipeline; Arbora runs no local planner. Returns the "
            "result rows and citations. dialect must be 'auto' or 'uql'; execute=false and a "
            "schema_hint are refused. Returns a typed error when the engine has no planner "
            "configured."
        ),
        tags=["arbora", "query", "nl"],
    )
    def nl_query(
        text: str = Field(description="The natural-language request to answer."),
        dialect: str = Field(
            default="auto",
            description="'auto' (model chooses, prefers uql) or 'uql'|'cypher'|'sql'|'sparql'.",
        ),
        schema_hint: str = Field(
            default="",
            description="Optional extra schema/context hint to ground the planner.",
        ),
        execute: bool = Field(
            default=True,
            description="When false, return only the generated query (preview/dry-run).",
        ),
        limit: int = Field(default=50, description="Max result rows to return."),
    ) -> EvidenceBundle:
        if str(dialect) not in {"auto", "uql"}:
            return _refuse_nl_option(
                "the engine NL planner plans UQL; dialect must be 'auto' or 'uql'",
                "nl_query",
            )
        if not bool(execute):
            return _refuse_nl_option(
                "the engine NL planner returns rows only; preview is unavailable",
                "nl_query",
            )
        if str(schema_hint):
            return _refuse_nl_option(
                "the engine NL planner grounds itself; schema_hint is not accepted",
                "nl_query",
            )
        try:
            return _engine_nl_query(text, limit=limit, operation="nl_query")
        except Exception as e:
            logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
            return EvidenceBundle.from_payload(
                public_error_json(e), operation="nl_query"
            )

    tools["nl_query"] = nl_query

    # CONCEPT:MERISTEM-KG.query.ask-gateway-rest-twin — gateway REST twin (W1 exposure; MCP⇄REST parity)
    tools.bind_route("nl_query", "/graph/nl-query")

    # ══════════════════════════════════════════════════════════════════
    # 1a-quinquies. ask_data — CONCEPT:MERISTEM-KG.query.data-gateway-rest-twin DB-GPT-style data-analysis agent loop
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="ask_data",
        description=(
            "CONCEPT:MERISTEM-KG.query.data-gateway-rest-twin — answer a DATA question over the "
            "Knowledge Graph through medulla's NL planner (EG QueryClient.nl_query): "
            "the engine plans one UQL query and executes it on its deterministic pipeline. "
            "Arbora runs no local planner or self-correction loop (corrections_used is "
            "always 0). Returns the evidence bundle with rows and citations."
        ),
        tags=["arbora", "query", "nl", "data-analysis"],
    )
    def ask_data(
        question: str = Field(
            description="The natural-language DATA question to answer."
        ),
        dialect: str = Field(
            default="auto",
            description="'auto' (planner chooses, prefers uql) or 'uql'|'cypher'|'sql'|'sparql'.",
        ),
        max_corrections: int = Field(
            default=2,
            description="Bounded self-correction retries after a failed query (0 disables).",
        ),
        limit: int = Field(default=50, description="Max result rows to return."),
    ) -> str:
        if str(dialect) not in {"auto", "uql"}:
            return public_error_json(
                ValueError(
                    "the engine NL planner plans UQL; dialect must be 'auto' or 'uql'"
                ),
                code="invalid_request",
            )
        try:
            bundle = _engine_nl_query(question, limit=limit, operation="ask_data")
        except Exception as e:
            logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
            return public_error_json(e)
        return json.dumps(
            {
                "question": str(question),
                "planner": "medulla",
                "dialect": "uql",
                "max_corrections": int(max_corrections),
                "corrections_used": 0,
                "evidence_bundle": bundle.model_dump(mode="json"),
            },
            default=str,
        )

    tools["ask_data"] = ask_data

    # CONCEPT:MERISTEM-KG.query.data-gateway-rest-twin — gateway REST twin (W1 exposure; MCP⇄REST parity)
    tools.bind_route("ask_data", "/graph/ask-data")

    # ══════════════════════════════════════════════════════════════════
    # 1a-ter. graph_table — CONCEPT:MERISTEM-KG.ingest.mirror-inbound connector/ETL → native engine SQL tables
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_table",
        description=(
            "CONCEPT:MERISTEM-KG.ingest.mirror-inbound — mirror data into native engine SQL tables (DataFusion + "
            "pg-wire) and manage them. Actions: 'ingest' (mirror a registered source "
            "connector's documents into a table via CREATE TABLE + bulk INSERT — "
            "source=<connector> e.g. rest/database/web/rss/filesystem/reader, "
            "config_json=connector config, table=<name>, replace=true to recreate), "
            "'rows' (bulk-INSERT arbitrary rows from rows_json into table), 'create' "
            "(CREATE TABLE table with columns_json), 'list' (list user tables), 'drop' "
            "(DROP TABLE table), 'query' (run a read-only SELECT via the engine SQL "
            "surface — sql=<SELECT ...>). This is how 'ingest tables from any connector "
            "/ mirror data into our DB' works."
        ),
        tags=["arbora", "ingestion", "table"],
    )
    def graph_table(
        action: str = Field(
            default="list",
            description="'ingest' | 'rows' | 'create' | 'list' | 'drop' | 'query'.",
        ),
        source: str = Field(
            default="", description="Registered connector key (action='ingest')."
        ),
        table: str = Field(default="", description="Target SQL table name."),
        config_json: str = Field(
            default="{}", description="JSON connector config (action='ingest')."
        ),
        columns_json: str = Field(
            default="[]", description="JSON list of column names (action='create')."
        ),
        rows_json: str = Field(
            default="[]", description="JSON list of row dicts (action='rows')."
        ),
        sql: str = Field(
            default="", description="A read-only SELECT statement (action='query')."
        ),
        limit: int = Field(
            default=1000, description="Max rows to mirror (action='ingest')."
        ),
        replace: bool = Field(
            default=False, description="Drop+recreate the table first (ingest/rows)."
        ),
    ) -> str:
        raise not_served(
            "knowledge_graph.core.table_ingest", "MEDULLA-INGEST-WORKER-R606"
        )

    tools["graph_table"] = graph_table
