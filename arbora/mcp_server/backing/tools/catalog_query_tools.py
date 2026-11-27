"""The catalog, context and code navigation tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field

from arbora.api.domain.mcp_server.backing import engine_routing
from arbora.core.event_loop import run_blocking_ordered
from arbora.mcp_server import runtime
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.error_surface import public_error_json

logger = logging.getLogger(__name__)


#: Code-symbol nav actions over the resolved graph (CONCEPT:MERISTEM-KG.backend.declared-columns-so-schema).
CODE_NAV_ACTIONS = frozenset(
    {"find_definition", "find_references", "trace_call_graph", "impact_of_change"}
)


#: Symbol→symbol path action (CONCEPT:MEDULLA-KG.compute.handled-outside-single-anchor) — handled outside the single-anchor
#: Cypher template because it resolves TWO endpoints and runs a native path search.
PATH_ACTIONS = frozenset({"connects"})


# Columns returned for a :Code node (kept identical across actions for a stable shape).
_CODE_COLS = (
    "{var}.id AS id, {var}.name AS name, {var}.file_path AS file_path, "
    "{var}.line AS line, {var}.language AS language, {var}.kind_detail AS kind, "
    "{var}.instance AS instance, {var}.source_system AS source_system"
)


def build_code_nav_query(
    *,
    action: str,
    symbol: str = "",
    node_id: str = "",
    source_system: str = "",
    depth: int = 3,
    limit: int = 200,
) -> tuple[str, dict[str, Any]]:
    """Build the (read-only) Cypher + params for a ``graph_code_nav`` action.

    Pure and side-effect-free so the templates are unit-tested without an engine.
    Operates on the resolved code graph: ``:Code`` symbols joined by ``calls``
    (symbol→symbol) edges. ``depth`` is validated and inlined (Cypher forbids a
    parameterized variable-length bound); everything else is parameterized.
    """
    if action not in CODE_NAV_ACTIONS:
        raise ValueError(
            f"unknown action '{action}'; expected one of {sorted(CODE_NAV_ACTIONS)}"
        )
    if not symbol and not node_id:
        raise ValueError("provide 'symbol' or 'node_id'")
    try:
        depth = max(1, min(10, int(depth)))
        limit = max(1, min(5000, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("depth/limit must be integers") from exc

    params: dict[str, Any] = {}
    # Match clause for the anchor :Code node (id wins over name), plus an optional
    # source_system scope, built as a WHERE so it composes across actions.
    conds: list[str] = []
    if node_id:
        conds.append("{var}.id = $node_id")
        params["node_id"] = node_id
    else:
        conds.append("{var}.name = $symbol")
        params["symbol"] = symbol
    if source_system:
        conds.append("{var}.source_system = $src")
        params["src"] = source_system

    def where_for(var: str) -> str:
        return " AND ".join(c.format(var=var) for c in conds)

    cols = _CODE_COLS

    if action == "find_definition":
        cypher = (
            f"MATCH (c:Code) WHERE {where_for('c')} "
            f"RETURN {cols.format(var='c')} LIMIT {limit}"
        )
    elif action == "find_references":
        # Callers: incoming `calls` edges to the anchor symbol.
        cypher = (
            f"MATCH (caller:Code)-[:calls]->(def:Code) WHERE {where_for('def')} "
            f"RETURN DISTINCT {cols.format(var='caller')} LIMIT {limit}"
        )
    elif action == "trace_call_graph":
        # Transitive callees reachable from the anchor (downstream).
        cypher = (
            f"MATCH (s:Code)-[:calls*1..{depth}]->(callee:Code) WHERE {where_for('s')} "
            f"RETURN DISTINCT {cols.format(var='callee')} LIMIT {limit}"
        )
    else:  # impact_of_change — transitive callers (upstream blast radius).
        cypher = (
            f"MATCH (caller:Code)-[:calls*1..{depth}]->(t:Code) WHERE {where_for('t')} "
            f"RETURN DISTINCT {cols.format(var='caller')} LIMIT {limit}"
        )
    return cypher, params


def _resolve_symbol_id(engine, *, symbol: str, node_id: str) -> dict[str, Any] | None:
    """Resolve a symbol name (or exact id) to its best :Code node row."""
    if node_id:
        cypher, params = build_code_nav_query(
            action="find_definition", node_id=node_id, limit=1
        )
    else:
        cypher, params = build_code_nav_query(
            action="find_definition", symbol=symbol, limit=1
        )
    rows = engine.query_cypher(cypher, params)
    return rows[0] if rows else None


def code_connects(
    engine,
    *,
    symbol: str = "",
    node_id: str = "",
    target_symbol: str = "",
    target_node_id: str = "",
) -> dict[str, Any]:
    """CONCEPT:MEDULLA-KG.compute.handled-outside-single-anchor — "what connects A to B": the shortest path between two
    :Code symbols, rendered hop-by-hop with the relation + confidence of each edge.

    Resolves both endpoints, runs the engine's native path search (BFS over the
    resolved graph; tries A→B then B→A so an undirected connection is found), and
    annotates each consecutive pair with the connecting edge. This is the durable,
    KG-native equivalent of Graphify's ``path`` command.
    """
    src = _resolve_symbol_id(engine, symbol=symbol, node_id=node_id)
    dst = _resolve_symbol_id(engine, symbol=target_symbol, node_id=target_node_id)
    if not src:
        return {"error": f"could not resolve source symbol '{symbol or node_id}'"}
    if not dst:
        return {
            "error": f"could not resolve target symbol '{target_symbol or target_node_id}'"
        }
    src_id, dst_id = src.get("id"), dst.get("id")
    if src_id == dst_id:
        return {"error": "source and target resolve to the same symbol", "id": src_id}

    path = engine.get_shortest_path(src_id, dst_id) or engine.get_shortest_path(
        dst_id, src_id
    )
    if not path:
        return {
            "source": src_id,
            "target": dst_id,
            "connected": False,
            "path": [],
        }

    hops = _annotate_path_hops(engine, path)

    return {
        "source": src_id,
        "target": dst_id,
        "connected": True,
        "length": len(path) - 1,
        "path": path,
        "hops": hops,
    }


def _annotate_path_hops(engine: Any, path: list[Any]) -> list[dict[str, Any]]:
    """Annotate each hop with the connecting edge (undirected match for the
    relation); best-effort per hop."""
    hops: list[dict[str, Any]] = []
    for a, b in zip(path, path[1:], strict=False):
        rel, conf = None, None
        try:
            erows = engine.query_cypher(
                "MATCH (x {id: $a})-[r]-(y {id: $b}) "
                "RETURN type(r) AS rel, r.confidence AS confidence LIMIT 1",
                {"a": a, "b": b},
            )
            if erows:
                rel = erows[0].get("rel")
                conf = erows[0].get("confidence")
        except Exception:  # annotation is best-effort
            logger.debug("edge annotation lookup failed", exc_info=True)
        hops.append({"from": a, "to": b, "rel": rel, "confidence": conf})
    return hops


def _capability_entry(
    available: bool,
    *,
    reason: str = "",
    methods: list[str] | None = None,
    items: Any = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"available": available}
    if not available:
        entry["reason"] = reason or "not available on the connected engine build"
        return entry
    if methods is not None:
        entry["methods"] = methods
    if items is not None:
        entry["items"] = items
    return entry


def _engine_domain_methods(domain: str) -> list[str] | None:
    """The introspected method list for one ``engine_tools.ENGINE_DOMAINS``
    entry, or ``None`` when that domain's client class is absent from the
    installed engine build (never guessed — see module comment above)."""
    try:
        from arbora.mcp_server.backing.tools.engine_tools import ENGINE_DOMAINS
    except ImportError:  # engine_tools itself unavailable
        return None
    return ENGINE_DOMAINS.get(domain)


def _catalog_unavailable_domain(domain: str, hint: str) -> dict[str, Any]:
    """A modality reported by capability alone (compiled in or not) — no
    listing call attempted, so ``items`` is intentionally absent rather than
    guessed at from a method NAME (e.g. picking whichever ``TimeSeriesClient``
    method sounds like "list" would risk calling a mutating/expensive method
    that merely has a plausible name)."""
    methods = _engine_domain_methods(domain)
    if not methods:
        return _capability_entry(False, reason=hint)
    return _capability_entry(True, methods=methods)


async def _catalog_graphs(tools: ToolTable) -> dict[str, Any]:
    """Physical graphs — via the already-registered ``engine_tenants`` tool
    (CONCEPT reference: `engine_tenants(action='list')`, referenced by name
    in several docstrings across this codebase but never centralized behind
    one discoverable call before this). ``tenants`` is an MERISTEM-P0-6
    ADMIN_DOMAINS entry (arbora/mcp_server/backing/tools/engine_tools/py) — a
    non-admin caller is correctly DENIED here, reported as ``denied`` rather
    than conflated with "not compiled in"."""
    methods = _engine_domain_methods("tenants")
    if not methods:
        return _capability_entry(
            False, reason="no MultiTenantClient on the installed engine client"
        )
    listing_fn = tools.get("engine_tenants")
    if listing_fn is None:
        return _capability_entry(True, methods=methods)
    try:
        # Explicit kwargs for every parameter — a direct call bypassing
        # FastMCP's Field-default resolution binds an omitted arg to its raw,
        # truthy `pydantic.fields.FieldInfo` rather than its intended default
        # (the SAME documented gotcha `engine_tools._dispatch` and
        # `query_tools._run_graph_query` both already guard against).
        raw = await listing_fn(action="list", params_json="{}", graph="")
    except PermissionError:
        entry = _capability_entry(True, methods=methods)
        entry["denied"] = True
        entry["reason"] = (
            "tenants is an ADMIN-scoped domain (MERISTEM-P0-6); caller lacks the "
            "required scope"
        )
        return entry
    except Exception as exc:
        logging.getLogger(__name__).debug("best-effort listing", exc_info=True)
        entry = _capability_entry(True, methods=methods)
        entry["list_error"] = type(exc).__name__
        return entry
    payload = json.loads(raw) if isinstance(raw, str) else raw
    return _capability_entry(True, methods=methods, items=payload)


async def _catalog_sql() -> dict[str, Any]:
    """Delegate to the already-landed, no-caller-SQL ``/graph/sql-schema``
    projection (``arbora/mcp_server/backing/tools/graph_tools/sql_schema`` —
    confirmed live and already consumed by the sibling WD10-A-SQL lane's own
    Table Explorer) rather than re-deriving table/column introspection here.
    Richer (catalog/schema nesting, primary keys) and safer (server-authored
    ``information_schema`` constants only — see
    ``graph_tools.CATALOG_STATEMENTS`` — never a per-table caller-shaped
    query) than a bespoke per-table probe would be."""
    from arbora.mcp_server.backing.tools import graph_tools

    try:
        projection = await graph_tools.sql_schema()
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "covers SqlSchemaUnavailable + any other failure", exc_info=True
        )
        return _capability_entry(
            False,
            reason=f"sql-schema introspection unavailable: {type(exc).__name__}",
        )
    return _capability_entry(True, items=projection)


async def _safe_catalog_entry(awaitable: Any) -> dict[str, Any]:
    """One modality's introspection failure never breaks the whole catalog."""
    try:
        return await awaitable
    except Exception as exc:
        logging.getLogger(__name__).debug("see docstring", exc_info=True)
        return _capability_entry(
            False, reason=f"introspection failed: {type(exc).__name__}"
        )


async def _catalog_sources() -> dict[str, Any]:
    """Project the process-owned source/connection registries for Atlas.

    The provider catalogue is synchronous and side-effect free, but wrapping it
    in this async adapter keeps the existing ``graph_catalog`` fan-in shape and
    ensures one malformed optional profile degrades only the ``sources`` leg.
    """

    from arbora.ingest.source_catalog import build_source_catalog
    from arbora.security.session import resolve_session

    # ``graph_catalog`` verifies this at its public boundary; retain the same
    # check here for direct in-process calls before touching the registry.
    resolve_session(required_scope="kg:read")
    registry = engine_routing.get_connection_registry()
    return build_source_catalog(registry=registry)


async def _build_graph_catalog(tools: ToolTable) -> dict[str, Any]:
    catalog: dict[str, Any] = {
        "graphs": await _safe_catalog_entry(_catalog_graphs(tools)),
        "sql": await _safe_catalog_entry(_catalog_sql()),
        # Atlas's governed external-source view is one more modality in this
        # catalog.  It projects graph_configure's named connection registry,
        # the source connector registry, and reference-only AgentConfig
        # declarations; it never opens a provider or exposes resolved secrets.
        "sources": await _safe_catalog_entry(_catalog_sources()),
        # KV namespaces and vector indexes have NO listing surface anywhere in
        # this codebase today (verified: no domain class in ENGINE_DOMAINS,
        # no `list_*` helper in meristem) — reported honestly rather
        # than fabricated, per the WD10-A-BACKEND brief's explicit
        # instruction not to claim a compiled-out modality is live.
        "kv": _capability_entry(
            False,
            reason=(
                "no KVClient / namespace-listing surface on the engine client; "
                "graph_context provides KV-like storage via graph nodes, not a "
                "named-namespace system"
            ),
        ),
        "vector": _capability_entry(
            False,
            reason=(
                "no VectorClient / list-collections method on the engine "
                "client; embeddings are stored as node properties, not named "
                "collections (see WD10-A-BACKEND lane report)"
            ),
        ),
        "ontology": _catalog_unavailable_domain(
            "reasoning",
            "no ReasoningClient (Method::OwlReason) on the installed engine client",
        ),
        "timeseries": _catalog_unavailable_domain(
            "timeseries", "no TimeSeriesClient on the installed engine client"
        ),
        "blob": _catalog_unavailable_domain(
            "blob", "no BlobClient on the installed engine client"
        ),
        "broker": _catalog_unavailable_domain(
            "broker", "no BrokerClient on the installed engine client"
        ),
        "saved_queries": _capability_entry(
            False,
            reason=(
                "no durable saved/continuous-query store on the AU side today "
                "(only ad hoc engine-level continuous-query primitives "
                "referenced in code comments, not wired to a listing surface)"
            ),
        ),
    }
    return catalog


async def _graph_catalog_response(
    tools: ToolTable,
    action: str,
    *,
    source: str,
    mode: str,
    ids_json: str,
    connection: str,
    graph: str,
    engine_provider: Callable[[], Any],
) -> str:
    normalized_action = action if isinstance(action, str) else "list"
    normalized_action = normalized_action.strip().lower()
    if normalized_action == "preview_sync":
        try:
            from arbora.ingest.source_catalog import normalize_source_sync_preview

            return json.dumps(
                normalize_source_sync_preview(
                    source=source,
                    mode=mode,
                    ids_json=ids_json,
                    connection=connection,
                    graph=graph,
                ),
                default=str,
            )
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "deterministic client error", exc_info=True
            )
            return public_error_json(exc, code="invalid_request")
    if normalized_action != "list":
        return public_error_json(
            ValueError("action must be 'list' or 'preview_sync'"),
            code="invalid_request",
        )
    try:
        engine_provider()
    except Exception as exc:
        logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
        return public_error_json(exc, code="dependency_unavailable")
    catalog = await _build_graph_catalog(tools)
    return json.dumps(catalog, default=str)


async def _graph_context_put(
    engine: Any,
    content: str,
    context_id: str,
    session_id: str,
    key: str,
    ttl_s: int,
) -> str:
    import contextlib
    import time
    import uuid as _uuid

    if not content:
        return json.dumps({"error": "content required for put"})
    sid = session_id or _uuid.uuid4().hex
    cid = context_id or f"ctx:{sid}:{key or _uuid.uuid4().hex}"
    snode = f"session:{sid}"

    def _persist_context_blob() -> None:
        engine.add_node(
            cid,
            "ContextBlob",
            properties={
                "id": cid,
                "content": content,
                "session_id": sid,
                "key": key,
                "ttl_s": int(ttl_s),
                "created_at": time.time(),
                "producer": runtime._SESSION_ID,
            },
        )
        # CONCEPT:MERISTEM-ORCH.session.session-anchored-collections-native — session-anchored collection: upsert the id-addressable
        # Session node and link it, so "list by session" is a reliable id-anchored
        # traversal (the engine has no property index; property scans are unreliable).
        with contextlib.suppress(Exception):
            engine.add_node(
                snode, "Session", properties={"id": snode, "session_id": sid}
            )
            engine.add_edge(snode, cid, "HAS_CONTEXT")

    await run_blocking_ordered(_persist_context_blob)
    return json.dumps({"context_id": cid, "session_id": sid})


async def _graph_context_get(engine: Any, context_id: str) -> str:
    import time

    if not context_id:
        return json.dumps({"error": "context_id required for get"})
    try:
        rows = await run_blocking_ordered(
            engine.query_cypher,
            "MATCH (c:ContextBlob) WHERE c.id = $id "
            "RETURN c.id AS id, c.content AS content, "
            "c.session_id AS session_id, "
            "c.created_at AS created_at, c.ttl_s AS ttl_s",
            {"id": context_id},
        )
        if not rows:
            return json.dumps({})
        row = rows[0]
        # TTL: treat an expired blob as gone (created_at + ttl_s < now).
        _ttl = row.get("ttl_s") or 0
        _created = row.get("created_at") or 0
        if _ttl and _created and (float(_created) + float(_ttl) < time.time()):
            return json.dumps({"error": "context expired", "expired": True})
        return json.dumps(row, default=str)
    except Exception as exc:
        logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
        return public_error_json(exc)


async def _graph_context_prune(engine: Any) -> str:
    import contextlib
    import time

    # Delete expired ContextBlobs (CONCEPT:MERISTEM-ORCH.session.invoker-agent-handoff lifecycle).
    try:

        def _prune_expired() -> tuple[int, int]:
            rows = engine.query_cypher(
                "MATCH (c:ContextBlob) WHERE c.ttl_s > 0 AND "
                "(c.created_at + c.ttl_s) < $now RETURN c.id AS id",
                {"now": time.time()},
            )
            count = 0
            _del = getattr(engine, "delete_node", None) or getattr(
                getattr(engine, "backend", None), "delete_node", None
            )
            for r in rows or []:
                if callable(_del):
                    with contextlib.suppress(Exception):
                        _del(r["id"])
                        count += 1
            return count, len(rows or [])

        pruned, expired = await run_blocking_ordered(_prune_expired)
        return json.dumps({"pruned": pruned, "expired": expired})
    except Exception as exc:
        logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
        return public_error_json(exc)


async def _graph_context_list(engine: Any, session_id: str) -> str:
    try:
        # CONCEPT:MERISTEM-ORCH.session.session-anchored-collections-native — id-anchored traversal from the Session node (the engine's
        # reliable, fast O(degree) path; the index-less backend can't serve property
        # scans). The traversal reader returns whole nodes (`RETURN c`), so project +
        # sort + limit client-side.
        rows = await run_blocking_ordered(
            engine.query_cypher,
            "MATCH (s {id: $snode})-[:HAS_CONTEXT]->(c:ContextBlob) RETURN c",
            {"snode": f"session:{session_id}"},
        )
        items = []
        for r in rows or []:
            c = r.get("c") if isinstance(r, dict) else None
            if isinstance(c, dict) and str(c.get("id", "")).startswith("ctx:"):
                items.append(
                    {
                        "context_id": c.get("id"),
                        "key": c.get("key"),
                        "created_at": c.get("created_at"),
                    }
                )
        items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return json.dumps(items[:50], default=str)
    except Exception as exc:
        logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
        return public_error_json(exc)


def register_catalog_query_tools(
    mcp, tools: ToolTable, engine_provider: Callable[[], Any]
) -> None:
    """Register the catalog_query_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # 1a-quinquies. graph_catalog — CONCEPT:MERISTEM-KG.query.unified-capability-catalog
    # (wD10 Atlas backend gap A): one capability-shaped call across every
    # modality instead of 4-6 protocol-specific ones. See the
    # `_build_graph_catalog` module docstring for the full design rationale.
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_catalog",
        description=(
            "CONCEPT:MERISTEM-KG.query.unified-capability-catalog — one capability-shaped call across "
            "every KG modality (graphs, sql, kv, vector, ontology, timeseries, blob, "
            "broker, saved_queries) instead of making a separate protocol-specific "
            "introspection call per modality. Each entry reports "
            "{available: bool, reason?, methods?, items?, denied?}: an absent/"
            "not-compiled-in backend yields available=false with a reason, NEVER "
            "an error — a frontend source-tree view can render every modality's "
            "state from this one response. 'graphs' lists physical engine graphs "
            "(ADMIN-scoped; a non-admin caller sees denied=true, not an error). "
            "'sql' lists user SQL tables with a best-effort per-table column probe "
            "(capped). Other modalities report compiled-in capability + their "
            "engine method list where the installed engine build has no safe "
            "generic listing method — never a guessed/fabricated item list. "
            "The 'sources' entry is Atlas's governed provider catalogue: it "
            "classifies PostgreSQL/database, registered Neo4j/AGE/Ladybug/"
            "medulla backends, generic OpenCypher and "
            "PuppyGraph, GraphQL, virtual graphs, Spark (compute-only), "
            "Iceberg/Trino, S3/object stores, and unsupported Teradata. It "
            "returns only neutral connection/profile references and truthful "
            "availability/reason/queryMode/dialects/sync/capabilities. "
            "action='preview_sync' validates one source_sync request and "
            "returns a typed non-executable normalization; it never runs a "
            "connector."
        ),
        tags=["arbora", "query", "introspection"],
    )
    async def graph_catalog(
        action: Literal["list", "preview_sync"] = Field(
            default="list",
            description=(
                "'list' (default) returns modality and provider catalogues; "
                "'preview_sync' validates one source_sync request without executing it."
            ),
        ),
        source: str = Field(
            default="",
            description="For preview_sync: exactly one neutral source identifier.",
        ),
        mode: str = Field(
            default="delta",
            description="For preview_sync: 'delta', 'full', or 'reconcile'.",
        ),
        ids_json: str = Field(
            default="[]",
            description="For preview_sync: JSON list of bounded source record ids.",
        ),
        connection: str = Field(
            default="",
            description="For preview_sync: one neutral named backend connection.",
        ),
        graph: str = Field(
            default="",
            description="For preview_sync: one neutral physical graph name.",
        ),
    ) -> str:
        try:
            # ``_execute_tool`` already scopes served calls, but this explicit
            # check protects direct MCP-function invocation and documents the
            # metadata boundary: profile/connection status is a governed read.
            from arbora.security.session import resolve_session

            resolve_session(required_scope="kg:read")
        except Exception as e:
            logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
            return public_error_json(e)
        return await _graph_catalog_response(
            tools,
            action,
            source=source,
            mode=mode,
            ids_json=ids_json,
            connection=connection,
            graph=graph,
            engine_provider=engine_provider,
        )

    tools["graph_catalog"] = graph_catalog

    runtime.ACTION_TOOL_ROUTES["graph_catalog"] = "/graph/catalog"

    # ══════════════════════════════════════════════════════════════════
    # 1b. graph_context — CONCEPT:MERISTEM-ORCH.session.invoker-agent-handoff cross-process curated-context store
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_context",
        description=(
            "CONCEPT:MERISTEM-ORCH.session.invoker-agent-handoff — store/fetch curated context for invoker→spawned-agent "
            "handoff, persisted in the medulla so a SEPARATELY-spawned agent can read "
            "it by id. Actions: 'put' (store content, returns context_id), 'get' (fetch by "
            "context_id), 'list' (by session_id). Pass the returned context_id to "
            "graph_orchestrate(context_ref=...)."
        ),
        tags=["arbora", "orchestrate", "context"],
    )
    async def graph_context(
        action: str = Field(default="put", description="put | get | list"),
        content: str = Field(
            default="", description="Context text to store (action=put)."
        ),
        context_id: str = Field(default="", description="ContextBlob id (action=get)."),
        session_id: str = Field(default="", description="Session scope key."),
        key: str = Field(
            default="", description="Optional sub-key within the session."
        ),
        ttl_s: int = Field(
            default=0, description="Optional time-to-live in seconds (0 = persistent)."
        ),
    ) -> str:
        engine = engine_provider()
        if not engine:
            return json.dumps({"error": "IntelligenceGraphEngine not active."})
        if action == "put":
            return await _graph_context_put(
                engine, content, context_id, session_id, key, ttl_s
            )
        if action == "get":
            return await _graph_context_get(engine, context_id)
        if action == "prune":
            return await _graph_context_prune(engine)
        if action == "list":
            return await _graph_context_list(engine, session_id)
        return json.dumps({"error": f"unknown action: {action}"})

    tools["graph_context"] = graph_context

    # ══════════════════════════════════════════════════════════════════
    # graph_code_nav — CONCEPT:MERISTEM-KG.backend.declared-columns-so-schema code-symbol navigation over the
    # RESOLVED graph (`:Code` + `calls`/`depends_on`/`IMPLEMENTS`). Templated
    # so agents (and Duo-style flows) get gkg's find-def / find-refs / trace /
    # impact as first-class tools instead of hand-written Cypher.
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_code_nav",
        description=(
            "Navigate the resolved code graph (CONCEPT:MERISTEM-KG.backend.declared-columns-so-schema). action: "
            "'find_definition' (locate a symbol's :Code node), 'find_references' "
            "(callers of a symbol), 'trace_call_graph' (transitive callees), "
            "'impact_of_change' (transitive callers = blast radius), 'connects' "
            "(shortest path between TWO symbols — set symbol/node_id AND "
            "target_symbol/target_node_id — rendered hop-by-hop with each edge's "
            "relation + confidence, CONCEPT:MEDULLA-KG.compute.handled-outside-single-anchor). Start from a symbol name or "
            "an exact node_id; optionally scope to a source_system "
            "(e.g. 'gitlab:gitlab.com')."
        ),
        tags=["arbora", "query", "code"],
    )
    def graph_code_nav(
        action: str = Field(
            description="find_definition | find_references | trace_call_graph | impact_of_change | connects"
        ),
        symbol: str = Field(
            default="", description="Symbol name to start from (function/class/method)."
        ),
        node_id: str = Field(
            default="", description="Exact :Code node id (overrides 'symbol' when set)."
        ),
        target_symbol: str = Field(
            default="",
            description="For action='connects': the destination symbol name.",
        ),
        target_node_id: str = Field(
            default="",
            description="For action='connects': the destination :Code node id.",
        ),
        source_system: str = Field(
            default="",
            description="Optional source_system filter, e.g. 'gitlab:gitlab.com'.",
        ),
        depth: int = Field(
            default=3,
            description="Max hops for trace_call_graph / impact_of_change (1-10).",
        ),
        limit: int = Field(default=200, description="Max rows to return."),
    ) -> str:
        """Templated code-symbol navigation over the resolved KG code graph."""
        engine = engine_provider()
        if not engine:
            return json.dumps({"error": "IntelligenceGraphEngine not active"})

        # 'connects' resolves two endpoints + runs a native path search — it lives
        # outside the single-anchor Cypher template.
        if action in PATH_ACTIONS:
            try:
                return json.dumps(
                    {
                        "action": action,
                        "results": code_connects(
                            engine,
                            symbol=symbol,
                            node_id=node_id,
                            target_symbol=target_symbol,
                            target_node_id=target_node_id,
                        ),
                    },
                    default=str,
                )
            except Exception as e:
                logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
                return public_error_json(e)

        try:
            cypher, qparams = build_code_nav_query(
                action=action,
                symbol=symbol,
                node_id=node_id,
                source_system=source_system,
                depth=depth,
                limit=limit,
            )
        except ValueError as e:
            return public_error_json(e)

        try:
            rows = engine.query_cypher(cypher, qparams)
            return json.dumps({"action": action, "results": rows}, default=str)
        except Exception as e:
            logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
            return public_error_json(e)

    tools["graph_code_nav"] = graph_code_nav
