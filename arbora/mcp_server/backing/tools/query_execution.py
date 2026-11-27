"""Graph query execution shared by the query tool families: fanout, local runs and result shaping (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic.fields import FieldInfo

from arbora.api.domain.mcp_server.backing import engine_routing
from arbora.api.evidence import EvidenceBundle
from arbora.security.error_surface import public_error_json

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """``json.dumps(default=...)`` helper that dataclass-serializes an
    :class:`~meristem.knowledge_graph.core.epistemic_row.EpistemicRow`
    (or any other dataclass instance) instead of stringifying it (CONCEPT:MERISTEM-KB-CURRENCY).

    Used wherever a tool's result may carry ``include_epistemic=True`` rows —
    plain dicts pass through ``json.dumps`` untouched; only genuinely
    non-serializable values (dataclass instances) reach this hook.
    """
    import dataclasses

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, EvidenceBundle):
        return obj.model_dump(mode="json")
    return str(obj)


def _row_ids(rows: Any) -> list[str]:
    """Distinct node ids in plain query rows, in first-seen order.

    A row names a node either as ``RETURN n.id AS id`` or as a node mapping
    with an ``id`` key under any alias. Other rows add nothing.
    """
    seen: dict[str, None] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        for value in (*row.values(), {"id": row.get("id")}):
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                if value["id"]:
                    seen.setdefault(value["id"], None)
    return list(seen)


_AGG_CALL_RE = re.compile(
    r"\b(?:count|sum|avg|min|max|collect|stdev|stdevp|percentilecont"
    r"|percentiledisc|variance)\s*\(",
    re.IGNORECASE,
)


def is_aggregation_cypher(cypher: str) -> bool:
    """True when ``cypher`` projects an aggregate; quoted literals never count."""
    no_literals = re.sub(r"'[^']*'|\"[^\"]*\"", "", cypher or "")
    return bool(_AGG_CALL_RE.search(no_literals))


def _evidence_bundle_for_rows(engine: Any, rows: list[Any]) -> EvidenceBundle:
    """The :class:`EvidenceBundle` over the nodes a plain query returned.

    One ``ExplainProvenanceByIds`` call resolves every distinct row id. The
    bundle is empty when the engine lacks the method or no row names a node.
    """
    fetch = getattr(getattr(engine, "graph", None), "explain_provenance_by_ids", None)
    ids = _row_ids(rows)
    if fetch is None or not ids:
        return EvidenceBundle.from_engine_wire({"rows": []})
    return EvidenceBundle.from_engine_wire({"rows": list(fetch(ids) or [])})


def _run_graph_query_sql(
    sql: str,
    connection: str,
    graph: str,
    as_of: Any = "",
) -> str:
    """``scope=='sql'`` branch of ``_run_graph_query`` (CONCEPT:MERISTEM-KG.query.read-only-sql-over).

    Statement-shape gate (WD10-A-BACKEND security review) lives in this thin
    wrapper, in FRONT of the original implementation
    (:func:`_run_graph_query_sql_engine`) — kept as a separate function rather
    than added inline so this pre-existing name's own
    complexity does not regress (`verify_both.py`'s per-function, no-
    pre-existing-function-may-get-worse rule): the gate's rejection routes
    through :func:`public_error_json` on a synthetic ``ValueError`` to match
    the EXACT SAME dict-shaped, message-redacted envelope every other
    rejection this branch returns already uses (required for
    ``EvidenceBundle.from_payload`` — this function's sole caller, via
    ``graph_query`` — to stay valid, and for the message-redaction contract
    ``test_sql_scope_surfaces_engine_error`` already asserts on).

    ``as_of`` (the public top-level bitemporal-instant filter) was previously
    accepted by ``graph_query`` and then silently dropped for ``scope='sql'``
    — the engine's ``sql()`` surface (``engine_query.py``) has no bitemporal
    instant parameter at all, so a caller who set ``as_of`` believing it was
    honored got an UNFILTERED result with no signal anything was ignored.
    Mirrors the ``scope='uql'`` precedent immediately below (same silent-drop
    failure mode, same fix): reject a non-empty ``as_of`` explicitly rather
    than silently executing without it.
    """
    if isinstance(as_of, str) and as_of.strip():
        return public_error_json(
            ValueError(
                "scope='sql' does not support as_of: the engine's SQL "
                "surface has no bitemporal instant filter. Omit as_of, or "
                "use scope='local' (Cypher), which honors it."
            ),
            code="invalid_request",
            context={"tool": "graph_query", "scope": "sql"},
        )
    rejection = _reject_unsafe_table_sql(str(sql or ""))
    if rejection is not None:
        return public_error_json(ValueError(rejection))
    return _run_graph_query_sql_engine(sql, connection, graph)


def _run_graph_query_engine(
    query: str,
    connection: str,
    graph: str,
    *,
    run_query: Callable[[Any, str], Any],
) -> str:
    """Run one dialect-specific query across resolved graph targets.

    The caller supplies only the engine method that executes its dialect; target
    resolution, graph selection, authenticated graph binding, fan-out handling,
    error conversion, and response serialization are shared by SQL and SPARQL.
    """
    try:
        entries, errors, fanout = engine_routing._resolve_target_engines(connection)
        entries = engine_routing.resolve_explicit_graph(entries, graph, fanout=fanout)
    except engine_routing.GraphNotFoundError as e:
        return public_error_json(e, code="graph_not_found")
    except engine_routing.GraphSelectionConflictError as e:
        return public_error_json(e, code="graph_selection_conflict")
    except Exception as e:
        logger.warning("operation failed", exc_info=True)
        return public_error_json(e)
    if not fanout:
        name, engine = entries[0]
        try:
            with engine_routing.bound_to_graph(graph):
                rows = run_query(engine, query)
            return json.dumps(
                {"rows": rows, "connection": name, "graph": graph},
                default=str,
            )
        except PermissionError as e:
            return public_error_json(
                e, code="permission_denied" if graph else "operation_failed"
            )
        except Exception as e:
            logger.warning("operation failed", exc_info=True)
            return public_error_json(e)
    results, fan_errors = engine_routing.fanout_execute(
        entries, lambda _name, engine: run_query(engine, query)
    )
    return json.dumps(
        {
            "targets": results,
            "errors": {**errors, **fan_errors},
            "connection": connection,
            "graph": graph,
        },
        default=str,
    )


def _run_graph_query_sql_engine(sql: str, connection: str, graph: str) -> str:
    return _run_graph_query_engine(
        sql,
        connection,
        graph,
        run_query=lambda engine, query: engine.sql(query),
    )


def _run_graph_query_sparql(sparql: str, connection: str, graph: str) -> str:
    """``scope=='sparql'`` branch of ``_run_graph_query`` (CONCEPT:MERISTEM-KG.ingest.mirror-inbound)."""
    return _run_graph_query_engine(
        sparql,
        connection,
        graph,
        run_query=lambda engine, query: engine.sparql(query),
    )


def _run_graph_query_federated(
    query: str,
    reference_id: str,
    parsed_params: dict[str, Any],
    engine_provider: Callable[[], Any],
) -> str:
    """``scope=='federated'`` branch of ``_run_graph_query``."""
    if not reference_id:
        return json.dumps({"error": "reference_id required for federated queries"})
    try:
        # Resolve the verified carrier before acquiring the process-owned
        # registry.  Federation repeats this guard at its lower boundary, but
        # the composition root must not even initialize/read registry state for
        # an unauthenticated request.
        from arbora.security.session import resolve_session

        resolve_session(required_scope="kg:read")
        engine = engine_provider()
        registry = engine_routing.get_connection_registry()
        results = engine.execute_federated_query(
            reference_id, query, parsed_params, registry=registry
        )
        return json.dumps(results, default=str)
    except Exception as e:
        logger.warning("operation failed", exc_info=True)
        return public_error_json(e)


def _run_graph_query_normalize_args(
    graph: str,
    params: Any,
    include_epistemic: bool,
    *,
    strict_params: bool = False,
) -> tuple[str, Any, bool]:
    """Normalize a direct call's raw ``Field``-default bindings.

    A direct call bypassing `_execute_tool` (which resolves `Field` defaults)
    binds omitted ``graph``/``params``/``include_epistemic`` values to raw
    ``pydantic.fields.FieldInfo`` objects rather than their declared defaults.
    Normalize once here so every use below sees a clean value, mirroring the
    SAME defensiveness `ConnectionRegistry.resolve_names` already applies to
    `connection`.
    """
    normalized_graph = graph if isinstance(graph, str) else ""
    parsed_params = _parse_graph_query_params(params, strict=strict_params)
    include_epistemic_flag = (
        include_epistemic if isinstance(include_epistemic, bool) else False
    )
    return normalized_graph, parsed_params, include_epistemic_flag


def _parse_graph_query_params(params: Any, *, strict: bool = False) -> Any:
    """Decode graph-query params while tolerating direct-call defaults.

    MCP clients send the documented JSON string; REST callers and direct unit
    callers occasionally hand us an already-decoded object. A raw pydantic
    ``FieldInfo`` (the omitted direct-call default) is neither and therefore
    means the declared empty object. Strict UQL calls reject malformed JSON,
    non-object JSON, and scalar/non-object Python values; the existing
    non-UQL path deliberately retains its historical permissive behavior.
    """
    if isinstance(params, str):
        if strict and not params.strip():
            raise ValueError("graph_query params must be a JSON object")
        parsed = json.loads(params) if params else {}
        if strict and not isinstance(parsed, dict):
            raise ValueError("graph_query params must decode to a JSON object")
        return parsed
    if isinstance(params, dict):
        return dict(params)
    if isinstance(params, FieldInfo):
        return {}
    if strict:
        raise ValueError("graph_query params must be a JSON object")
    return {}


def _run_graph_query_normalize_for_scope(
    graph: str, params: Any, include_epistemic: bool, scope: Any
) -> tuple[str, Any, bool] | str:
    """Normalize direct-call defaults and expose strict UQL input errors.

    ``graph_query`` historically lets non-UQL callers retain the parser's
    existing behavior, including malformed JSON escaping from the direct
    function call. UQL is a new governed boundary, so its parameter contract
    is fail-closed and returns the same typed invalid-request envelope as its
    query validator.
    """
    try:
        return _run_graph_query_normalize_args(
            graph,
            params,
            include_epistemic,
            strict_params=scope == "uql",
        )
    except (TypeError, ValueError) as exc:
        if scope != "uql":
            raise
        return public_error_json(
            exc,
            code="invalid_request",
            context={"tool": "graph_query", "scope": "uql"},
        )


# CONCEPT:MERISTEM-KG.query.au-engine-execution-path — the MCP graph-query boundary deliberately
# accepts only a bounded, read-only UQL *pipeline*.  The native UQL parser is
# still the source of truth for the complete language; this small gate exists
# before connection resolution so an obviously unbounded/statement-shaped
# request cannot reach an external connection by accident.  UQL has no
# parameter-binding surface, so the query text itself is the only input to the
# engine's governed ``IntelligenceGraphEngine.uql`` method.
# Intentional public bound: 1..1000 keeps every governed UQL read finite while
# avoiding native LIMIT 0's empty-result/no-op semantics. Callers that need an
# empty result should use a predicate, not a zero-row request.
_UQL_MAX_LIMIT = 1000


_UQL_SOURCE_RE = re.compile(r"^\s*MATCH\b", re.IGNORECASE)


_UQL_LIMIT_RE = re.compile(r"\bLIMIT\s+([0-9]+)\b", re.IGNORECASE)


_UQL_MUTATION_RE = re.compile(
    r"(?:^|\|>)\s*(?:CREATE|MERGE|DELETE|REMOVE|DROP|INSERT|UPDATE|SET|LOAD)\b",
    re.IGNORECASE,
)


class _UQLSurfaceUnavailable(RuntimeError):
    """The resolved connection is not an IntelligenceGraphEngine UQL surface."""


def _uql_iri_end(query: str, start: int) -> int | None:
    """Return the closing offset for a native whitespace-free IRI token."""
    close = query.find(">", start + 1)
    if close == -1:
        return None
    body = query[start + 1 : close]
    if not body or ":" not in body or any(char.isspace() for char in body):
        return None
    return close


def _blank_uql_quoted_literal(
    query: str, chars: list[str], start: int, quote: str
) -> tuple[int, bool]:
    """Blank one quoted UQL literal and return ``(next_offset, closed)``."""
    chars[start] = " "
    i = start + 1
    while i < len(chars):
        ch = query[i]
        chars[i] = " "
        if quote == '"' and ch == "\\" and i + 1 < len(chars):
            # Treat escaped bytes as literal content for this lexical pass.
            chars[i + 1] = " "
            i += 2
            continue
        if ch == quote:
            if i + 1 < len(chars) and query[i + 1] == quote:
                # SQL/UQL-style doubled quote escape.
                chars[i + 1] = " "
                i += 2
                continue
            return i + 1, True
        i += 1
    return len(chars), False


def _strip_uql_literals(query: str) -> tuple[str, bool]:
    """Blank quoted UQL literals and IRIs while retaining surrounding grammar.

    Mutation words, semicolons, and ``LIMIT`` are meaningful only outside
    string/IRI literals. UQL uses single- and double-quoted strings and accepts
    doubled quote escapes; accepting a backslash escape here is harmless and
    keeps this lexical guard from flagging a value that the native parser will
    later reject. The boolean reports an unterminated string literal so the
    caller can fail with a stable invalid-request envelope before any engine
    call. Angle-bracketed IRIs mirror the native lexer's whitespace-free
    ``<scheme:...>`` rule and are blanked so punctuation in an IRI is data.
    """

    chars = list(query)
    i = 0
    while i < len(chars):
        ch = query[i]
        if ch in ("'", '"'):
            i, closed = _blank_uql_quoted_literal(query, chars, i, ch)
            if not closed:
                return "".join(chars), True
            continue
        if ch == "<":
            close = _uql_iri_end(query, i)
            if close is not None:
                chars[i : close + 1] = [" "] * (close - i + 1)
                i = close + 1
                continue
        i += 1
    return "".join(chars), False


def _validate_uql_lexical_contract(query: str) -> str:
    """Apply statement/literal safety checks and return the blanked query."""
    lexical, unterminated = _strip_uql_literals(query)
    if unterminated:
        raise ValueError("scope='uql' query contains an unterminated string literal")
    if ";" in lexical:
        raise ValueError("scope='uql' accepts one read-only pipeline, not statements")
    return lexical


def _validate_uql_read_only_source(lexical: str) -> None:
    """Require the native UQL leading source and reject write-shaped stages."""
    if not _UQL_SOURCE_RE.match(lexical):
        raise ValueError("scope='uql' query must start with MATCH")
    if _UQL_MUTATION_RE.search(lexical):
        raise ValueError("scope='uql' accepts read-only pipeline stages only")


def _validate_uql_terminal_limit(lexical: str) -> int:
    """Require every UQL limit to be bounded and the final one to be terminal."""
    lexical_tail = lexical.rstrip()
    limits = list(_UQL_LIMIT_RE.finditer(lexical_tail))
    final_limit = limits[-1] if limits else None
    if final_limit is None or final_limit.end() != len(lexical_tail):
        raise ValueError(f"scope='uql' query must end with LIMIT 1..{_UQL_MAX_LIMIT}")
    if any(not 1 <= int(match.group(1)) <= _UQL_MAX_LIMIT for match in limits):
        raise ValueError(
            f"scope='uql' every LIMIT must be between 1 and {_UQL_MAX_LIMIT}"
        )
    return int(final_limit.group(1))


def _validate_uql_query(query: str) -> int:
    """Validate the graph-query UQL contract and return its bounded LIMIT.

    The native engine remains responsible for parsing/executing UQL.  This
    boundary check only enforces the product contract: one UQL source form,
    no mutation-shaped tokens outside literals, and a terminal ``LIMIT`` in
    ``1.._UQL_MAX_LIMIT``.  Requiring the limit at the end makes the bound
    apply to the final pipeline result even when the query contains ranking,
    fusion, or other intermediate stages.
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("scope='uql' requires a non-empty UQL query")
    lexical = _validate_uql_lexical_contract(query)
    _validate_uql_read_only_source(lexical)
    return _validate_uql_terminal_limit(lexical)


def _run_graph_query_uql_rows(
    query: str,
    engine: Any,
    *,
    graph: str = "",
    include_epistemic: bool = False,
) -> list[Any]:
    """Run UQL through the governed engine surface, never its raw client.

    ``IntelligenceGraphEngine.uql`` owns the engine-side parser, ACL/owner
    post-filter, and epistemic enrichment.  Named external registry entries
    normally expose only ``query_cypher``; rejecting them here prevents a
    caller from accidentally bypassing that governed surface by reaching into
    a backend/client object.
    """

    if not callable(getattr(engine, "uql", None)):
        raise _UQLSurfaceUnavailable(
            "the selected connection does not expose governed UQL through "
            "IntelligenceGraphEngine.uql"
        )
    with engine_routing.bound_to_graph(graph):
        if include_epistemic:
            rows = engine.uql(query, include_epistemic=True)
        else:
            # Preserve compatibility with older test doubles/adapters whose
            # governed uql method predates the optional kwarg.
            rows = engine.uql(query)
    return list(rows or [])


def _uql_fanout_evidence_request(
    engine: Any, rows: Any
) -> tuple[Callable[[list[Any]], Any] | None, list[Any]]:
    """Resolve one target's provenance callable and returned-row IDs."""
    if not isinstance(rows, list):
        return None, []
    fetch = getattr(getattr(engine, "graph", None), "explain_provenance_by_ids", None)
    if not callable(fetch):
        return None, []

    return fetch, _row_ids(rows)


def _uql_fanout_target_evidence(
    name: str, engine: Any, rows: Any, *, union_read: bool
) -> list[Any]:
    """Fetch one target's provenance, preserving partial-success semantics."""
    fetch, ids = _uql_fanout_evidence_request(engine, rows)
    if fetch is None or not ids:
        return []
    try:
        # Routed content targets need the same verified graph binding for
        # provenance as for the UQL read itself.  The default target is
        # already the ambient authority and needs no retargeting.
        target_graph = name if union_read and name != "default" else ""
        with engine_routing.bound_to_graph(target_graph):
            return list(fetch(ids) or [])
    except Exception as exc:  # evidence is best-effort per target
        logger.warning(
            "UQL fan-out provenance target failed (target=%s exception_type=%s)",
            name,
            type(exc).__name__,
            exc_info=True,
        )
        return []


def _evidence_bundle_for_uql_fanout(
    entries: Sequence[tuple[str, Any]],
    results: dict[str, Any],
    *,
    union_read: bool,
) -> EvidenceBundle:
    """Aggregate per-target UQL provenance into one complete bundle.

    Each target owns its own provenance authority.  Fetching IDs from only the
    default engine would silently drop evidence for routed/content graphs, so
    this helper performs the same governed ``explain_provenance_by_ids`` read
    per target and folds all returned wire rows into one bundle.  A target with
    no primitive contributes no fabricated evidence; failures are logged and
    leave the already-successful target evidence intact.
    """
    wire_rows: list[Any] = []
    for name, engine in entries:
        wire_rows.extend(
            _uql_fanout_target_evidence(
                name, engine, results.get(name), union_read=union_read
            )
        )
    return EvidenceBundle.from_engine_wire({"rows": wire_rows})


def _run_graph_query_uql_single(
    query: str,
    include_epistemic: bool,
    graph: str,
    name: str,
    engine: Any,
) -> str:
    """Execute one governed UQL target and preserve typed evidence."""

    if not callable(getattr(engine, "uql", None)):
        return public_error_json(
            _UQLSurfaceUnavailable(
                f"connection {name!r} does not expose governed UQL through "
                "IntelligenceGraphEngine.uql"
            ),
            code="dependency_unavailable",
            context={"tool": "graph_query", "scope": "uql"},
        )
    try:
        rows = _run_graph_query_uql_rows(
            query, engine, graph=graph, include_epistemic=include_epistemic
        )
        payload: dict[str, Any] = {
            "rows": rows,
            "connection": name,
            "graph": graph,
        }
        if not include_epistemic:
            with engine_routing.bound_to_graph(graph):
                payload["evidence_bundle"] = _evidence_bundle_for_rows(engine, rows)
        return json.dumps(payload, default=_json_default)
    except engine_routing.GraphSelectionConflictError as exc:
        return public_error_json(exc, code="graph_selection_conflict")
    except PermissionError as exc:
        return public_error_json(
            exc, code="permission_denied" if graph else "operation_failed"
        )
    except Exception as exc:
        logging.getLogger(__name__).debug("public MCP error boundary", exc_info=True)
        return public_error_json(exc)


def _run_graph_query_uql_fanout(
    query: str,
    include_epistemic: bool,
    connection: str,
    graph: str,
    entries: list[tuple[str, Any]],
    errors: dict[str, Any],
    union_read: bool,
) -> str:
    """Execute governed UQL across explicit or routed connection targets."""

    # ExternalGraphConnection intentionally exposes only read Cypher.  Filter
    # unsupported targets before fan-out so the response says exactly which
    # connection lacks the UQL capability instead of flattening that fact into
    # a generic timeout/operation label.
    supported: list[tuple[str, Any]] = []
    unsupported: dict[str, str] = {}
    for name, engine in entries:
        if callable(getattr(engine, "uql", None)):
            supported.append((name, engine))
        else:
            unsupported[name] = "uql_surface_unavailable"
    errors = {**errors, **unsupported}

    def _fanout_query(name: str, engine: Any) -> Any:
        del name
        return _run_graph_query_uql_rows(
            query, engine, graph=graph, include_epistemic=include_epistemic
        )

    def _content_graph_query(name: str, engine: Any) -> Any:
        return _run_graph_query_uql_rows(
            query, engine, graph=name, include_epistemic=include_epistemic
        )

    results, fan_errors = _run_graph_query_fanout_targets(
        supported, union_read, _fanout_query, _content_graph_query
    )
    all_errors = {**errors, **fan_errors}
    if not results and all_errors:
        return _uql_fanout_failure(connection, graph, all_errors)
    payload: dict[str, Any]
    if union_read:
        payload = {
            "rows": _merge_fanout_rows(results),
            "connection": connection,
            "graph": graph,
        }
    else:
        payload = {
            "targets": results,
            "errors": all_errors,
            "connection": connection,
            "graph": graph,
        }
    if not include_epistemic:
        payload["evidence_bundle"] = _evidence_bundle_for_uql_fanout(
            supported, results, union_read=union_read
        ).model_dump()
    if union_read and all_errors:
        # Keep partial-success diagnostics lossless alongside the canonical
        # merged rows.  ``EvidenceBundle.from_payload`` retains this sibling
        # in its reasoning trace when it unwraps the embedded bundle.
        payload["errors"] = all_errors
    return json.dumps(payload, default=_json_default)


def _uql_fanout_failure(connection: str, graph: str, errors: dict[str, Any]) -> str:
    """Return a typed failure when no UQL fan-out target produced a result."""
    payload = json.loads(
        public_error_json(
            RuntimeError(
                "all selected UQL fan-out targets failed or lack governed UQL"
            ),
            code="dependency_unavailable",
            context={"tool": "graph_query", "scope": "uql"},
        )
    )
    # Keep target diagnostics lossless beside the canonical typed error. The
    # public EvidenceBundle retains these siblings in its reasoning trace.
    payload.update(
        {
            "targets": {},
            "errors": errors,
            "connection": connection,
            "graph": graph,
        }
    )
    return json.dumps(payload, default=_json_default)


def _run_graph_query_resolve_local(
    connection: str, graph: str
) -> tuple[list[tuple[str, Any]], dict[str, Any], bool, str | None]:
    """Resolve the local (Cypher) read target set, or a ready-to-return error body.

    Mirrors the identical three-way except clause every other ``_run_graph_query``
    scope branch uses, just returning the error JSON instead of returning it
    directly so the caller can still short-circuit.
    """
    try:
        entries, errors, fanout = engine_routing._resolve_read_engines(connection)
        entries = engine_routing.resolve_explicit_graph(entries, graph, fanout=fanout)
        return entries, errors, fanout, None
    except engine_routing.GraphNotFoundError as e:
        return [], {}, False, public_error_json(e, code="graph_not_found")
    except engine_routing.GraphSelectionConflictError as e:
        return [], {}, False, public_error_json(e, code="graph_selection_conflict")
    except Exception as e:
        logger.warning("read engine resolution failed", exc_info=True)
        return [], {}, False, public_error_json(e)


def _run_graph_query_is_union_read(fanout: bool, connection: str | None) -> bool:
    """Whether this fan-out is the implicit content-graph UNION (no explicit connection)."""
    if not fanout:
        return False
    if connection is None:
        return True
    return isinstance(connection, str) and connection.strip().lower() in (
        "",
        "default",
    )


def _run_graph_query_uql(
    query: str,
    connection: str,
    graph: str,
    include_epistemic: bool,
    params: dict[str, Any],
    as_of: Any,
) -> str:
    """Validate, resolve, and execute one canonical ``graph_query`` UQL call.

    UQL has no parameter-binding surface. Reject a non-empty ``params`` object
    rather than silently accepting inputs that the governed engine cannot apply;
    the text itself remains the sole execution input. The native UQL method
    cannot honor the top-level as_of filter, so a non-empty value is rejected
    explicitly rather than silently ignored. Connection and physical graph
    selection still use the same resolver as local Cypher, so routed content
    graphs and explicit graph authorization keep one policy path.
    """
    try:
        _validate_uql_query(query)
        if params != {}:
            raise ValueError("scope='uql' does not accept query parameters")
        if isinstance(as_of, str) and as_of.strip():
            raise ValueError(
                "scope='uql' does not accept top-level as_of; include an AS OF "
                "stage in the UQL pipeline"
            )
    except ValueError as exc:
        return public_error_json(
            exc,
            code="invalid_request",
            context={"tool": "graph_query", "scope": "uql"},
        )
    entries, errors, fanout, error_response = _run_graph_query_resolve_local(
        connection, graph
    )
    if error_response is not None:
        return error_response
    union_read = _run_graph_query_is_union_read(fanout, connection)
    if not fanout:
        name, engine = entries[0]
        return _run_graph_query_uql_single(
            query, include_epistemic, graph, name, engine
        )
    return _run_graph_query_uql_fanout(
        query,
        include_epistemic,
        connection,
        graph,
        entries,
        errors,
        union_read,
    )


def _run_graph_query_local(
    cypher: str,
    parsed_params: dict[str, Any],
    as_of: str,
    include_epistemic: bool,
    include_epistemic_flag: bool,
    connection: str,
    graph: str,
    engine_provider: Callable[[], Any],
) -> str:
    """Resolve and execute the fall-through local Cypher query path.

    CONCEPT:MERISTEM-KG.backend.multi-connection-registry — resolve the
    connection(s). CONCEPT:MERISTEM-KG.ingest.unified-query-routing — with ingestion
    graph routing on, an implicit-default read fans across active content
    graphs so split content remains queryable as one KG. CONCEPT:MERISTEM-KG.backend.
    explicit-graph-selection — ``graph`` is a physical graph axis separate from
    ``connection`` and is rejected by the resolver when fan-out would make it
    ambiguous.
    """
    entries, errors, fanout, error_response = _run_graph_query_resolve_local(
        connection, graph
    )
    if error_response is not None:
        return error_response

    # Whether this fan-out is the implicit content-graph UNION (no explicit
    # connection). Those rows are merged into the canonical ``rows`` field; an
    # explicit ``connection='all'``/list keeps the per-target map.
    union_read = _run_graph_query_is_union_read(fanout, connection)

    # CONCEPT:MERISTEM-KG.query.query-aggregation — an aggregation (count/sum/group-by) under the implicit
    # content-graph union CANNOT be fanned: aggregate rows carry no node id to
    # dedup on, so id-dedup leaves one copy of every group row PER
    # graph (for example, one aggregate row repeated per graph). Summing generically is
    # unsafe (wrong for avg/min/max/distinct). Run the aggregation against the
    # canonical default graph only — control-plane/aggregate reads resolve there.
    if union_read and is_aggregation_cypher(cypher):
        return _run_graph_query_union_aggregate(
            cypher,
            parsed_params,
            as_of,
            include_epistemic_flag,
            graph,
            engine_provider,
        )

    if not fanout:
        # Single connection (default or one named).
        name, engine = entries[0]
        return _run_graph_query_single(
            cypher, parsed_params, as_of, include_epistemic, graph, name, engine
        )

    return _run_graph_query_fanout(
        cypher,
        parsed_params,
        as_of,
        include_epistemic_flag,
        connection,
        graph,
        entries,
        errors,
        union_read,
    )


def _run_graph_query_union_aggregate(
    cypher: str,
    parsed_params: dict[str, Any],
    as_of: str,
    include_epistemic_flag: bool,
    graph: str,
    engine_provider: Callable[[], Any],
) -> str:
    """CONCEPT:MERISTEM-KG.query.query-aggregation — an aggregation under the implicit content-graph
    union runs against the canonical default graph only (see the caller for why).
    """
    engine = engine_provider()
    try:
        if include_epistemic_flag:
            results = engine.query_cypher(
                cypher, parsed_params, as_of=as_of or None, include_epistemic=True
            )
        else:
            results = engine.query_cypher(cypher, parsed_params, as_of=as_of or None)
        return json.dumps(
            {"rows": results, "connection": "default", "graph": graph},
            default=str,
        )
    except Exception as e:
        logger.warning("operation failed", exc_info=True)
        return public_error_json(e)


def _run_graph_query_single(
    cypher: str,
    parsed_params: dict[str, Any],
    as_of: str,
    include_epistemic: bool,
    graph: str,
    name: str,
    engine: Any,
) -> str:
    """Single connection (default or one named) local Cypher read."""
    # Same raw-call defensive normalization as `envelope`: a direct call
    # bypassing FastMCP schema resolution binds an omitted bool Field to its
    # FieldInfo, not the `False` default.
    include_epistemic_flag = (
        include_epistemic if isinstance(include_epistemic, bool) else False
    )
    try:
        with engine_routing.bound_to_graph(graph):
            if include_epistemic_flag:
                # Only pass the new kwarg when actually requested — keeps the
                # default call shape byte-identical for any `query_cypher`
                # implementation (real or test double) that predates this
                # parameter and doesn't accept it.
                results = engine.query_cypher(
                    cypher,
                    parsed_params,
                    as_of=as_of or None,
                    include_epistemic=True,
                )
                # Per-row epistemic envelope takes precedence over `envelope`
                # (there is no aggregate-bundle-of-epistemic-rows shape).
                return json.dumps(
                    {
                        "rows": results,
                        "connection": name,
                        "graph": graph,
                    },
                    default=_json_default,
                )
            results = engine.query_cypher(
                cypher,
                parsed_params,
                as_of=as_of or None,
                include_epistemic=include_epistemic_flag,
            )
        if include_epistemic_flag:
            return json.dumps(
                {"rows": results, "connection": name, "graph": graph},
                default=_json_default,
            )
        return json.dumps(
            {
                "rows": results,
                "connection": name,
                "graph": graph,
                "evidence_bundle": _evidence_bundle_for_rows(engine, results),
            },
            default=str,
        )
    except engine_routing.GraphSelectionConflictError as e:
        return public_error_json(e, code="graph_selection_conflict")
    except PermissionError as e:
        # Only an EXPLICIT `graph` request classifies its own denial as
        # `permission_denied` — the pre-existing behavior for a plain
        # backend-level PermissionError (e.g. a write rejected on the
        # read-only query path) with no `graph` selection in play is
        # unchanged (`operation_failed`, BUG-048's ambiguous default).
        return public_error_json(
            e, code="permission_denied" if graph else "operation_failed"
        )
    except Exception as e:
        logger.warning("operation failed", exc_info=True)
        return public_error_json(e)


def _run_graph_query_fanout_primary(
    primary: list[tuple[str, Any]],
    fanout_query: Any,
    content_graph_query: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    results: dict[str, Any] = {}
    fan_errors: dict[str, Any] = {}
    for name, engine in primary:
        # Same partial-success contract `fanout_execute` gives every
        # target below (and the ACL/tenant denial path relies on): a
        # backend that legitimately REJECTS this query (e.g. a
        # permission/ACL denial from `engine.query_cypher`'s own
        # enforcement) must land in `fan_errors`, not crash the whole
        # tool call — the primary is queried directly (not through
        # `fanout_execute`) only to skip its concurrency/timeout
        # machinery, not its error handling.
        try:
            # B-18: the robustness fallback above can promote a real
            # content graph into `primary` when no "default" entry was
            # resolved — bind it too, never just the literal-"default"
            # entry (which needs no narrowing since it already targets
            # the ambient session's own graph).
            fn = fanout_query if name == "default" else content_graph_query
            results[name] = fn(name, engine)
        except Exception as exc:  # mirrors fanout_execute's per-target catch below (same non-leaking, BUG-048-classified label): a denied/failed primary must surface as a labeled error, never propagate raw or silently vanish
            logger.warning(
                "Graph fan-out primary target failed (exception_type=%s)",
                type(exc).__name__,
                exc_info=True,
            )
            fan_errors[name] = engine_routing.fanout_error_label(exc)
    return results, fan_errors


def _run_graph_query_fanout_targets(
    entries: list[tuple[str, Any]],
    union_read: bool,
    fanout_query: Any,
    content_graph_query: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not union_read:
        # Explicit cross-repo fan-out (`target='all'`/list) — per-target
        # timeout at the full budget so one slow backend can't stall the set.
        return engine_routing.fanout_execute(entries, fanout_query)

    # CONCEPT:MERISTEM-KG.ingest.unified-query-routing — mirror `graph_search`'s
    # primary/supplementary split (see the `graph_search` implementation
    # above). Before this, an implicit-default `graph_query` shared ONE
    # `DEFAULT_FANOUT_TIMEOUT_S` (30s) budget across every resolved
    # content-graph entry, including the `default` connection. Under a
    # wide implicit fan-out (dozens of idle/unreachable `code:*`/`src:*`
    # graphs) the primary was queued behind the hung ones on the same
    # 8-worker pool and timed out too — a plain Cypher read against the
    # live default graph could take minutes even though the engine
    # itself (`engine_query action=cypher`, which bypasses this router)
    # answers instantly. The primary/`default` connection now always
    # grounds at the full budget, run separately and first; only the
    # SUPPLEMENTARY content connections take the short
    # `DEFAULT_CONTENT_FANOUT_TIMEOUT_S` skip-budget. An explicit
    # `target='all'`/list is a deliberate cross-repo request and is
    # NEVER `_union_read` (see its definition above), so it is
    # unaffected and keeps the full per-target budget below.
    primary = [(n, e) for n, e in entries if n == "default"]
    supplementary = [(n, e) for n, e in entries if n != "default"]
    # Robustness: if the resolver produced no `default` entry, treat the
    # first as primary so SOMETHING always grounds at the full budget.
    if not primary and entries:
        primary, supplementary = [entries[0]], entries[1:]
    results, fan_errors = _run_graph_query_fanout_primary(
        primary, fanout_query, content_graph_query
    )
    if supplementary:
        sup_results, sup_errors = engine_routing.fanout_execute(
            supplementary,
            content_graph_query,
            timeout=engine_routing.DEFAULT_CONTENT_FANOUT_TIMEOUT_S,
        )
        results.update(sup_results)
        fan_errors.update(sup_errors)
    return results, fan_errors


def _run_graph_query_fanout_response(
    results: dict[str, Any],
    fan_errors: dict[str, Any],
    errors: dict[str, Any],
    connection: str,
    graph: str,
    union_read: bool,
) -> str:
    if union_read:
        merged = _merge_fanout_rows(results)
        return json.dumps(
            {"rows": merged, "connection": connection, "graph": graph},
            default=_json_default,
        )
    return json.dumps(
        {
            "targets": results,
            "errors": {**errors, **fan_errors},
            "connection": connection,
            "graph": graph,
        },
        default=_json_default,
    )


def _merge_fanout_rows(results: dict[str, Any]) -> list[Any]:
    """Merge the per-graph row lists into one id-deduped canonical row set."""
    merged: list[Any] = []
    seen_ids: set[str] = set()
    for _name in results:
        for row in results[_name] or []:
            rid = row.get("id") if isinstance(row, dict) else None
            if rid is not None:
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
            merged.append(row)
    return merged


def _run_graph_query_fanout(
    cypher: str,
    parsed_params: dict[str, Any],
    as_of: str,
    include_epistemic_flag: bool,
    connection: str,
    graph: str,
    entries: list[tuple[str, Any]],
    errors: dict[str, Any],
    union_read: bool,
) -> str:
    """Fan-out — per-target timeout so one slow backend can't stall the set."""

    def _fanout_query(name: str, engine: Any) -> Any:
        del name
        # Same defensive kwarg omission as the single-connection branch
        # above: only pass `include_epistemic` when actually requested,
        # so a `query_cypher` implementation (real or test double) that
        # predates this parameter and doesn't accept it keeps working.
        if include_epistemic_flag:
            return engine.query_cypher(
                cypher, parsed_params, as_of=as_of or None, include_epistemic=True
            )
        return engine.query_cypher(cypher, parsed_params, as_of=as_of or None)

    def _content_graph_query(name: str, engine: Any) -> Any:
        # B-18 fix: within the implicit content-graph UNION fan-out (never
        # the explicit multi-connection fan-out below, which never calls
        # this), `name` IS the physical graph `engine` is scoped to (see
        # `engine_routing._resolve_read_engines`'s
        # `ingest_routing.safe_engine_for_graph(gname)` — a `for_graph()`
        # view that fixes the outbound wire request's `graph` field but
        # never rebinds `session.graph` to match). The wire layer's own
        # mismatch lock (`_SessionRoutedAsyncClient._send`) then rejects
        # every non-"default" leg with `PermissionError`, degrading the
        # union to per-target errors. Narrow the verified session to the
        # SAME graph via the sanctioned `bound_to_graph` primitive
        # (CONCEPT:MERISTEM-KG.backend.explicit-graph-selection) so the request
        # is self-consistent instead of inventing a second mechanism.
        with engine_routing.bound_to_graph(name):
            return _fanout_query(name, engine)

    results, fan_errors = _run_graph_query_fanout_targets(
        entries, union_read, _fanout_query, _content_graph_query
    )
    return _run_graph_query_fanout_response(
        results, fan_errors, errors, connection, graph, union_read
    )


# ══════════════════════════════════════════════════════════════════
# WD10-A-BACKEND security review (plans/semantic-indexing/DESIGN-embedding-
# bindings.md §4): defense-in-depth statement-shape gate in FRONT of every
# raw-SQL call site this file owns (`graph_table` action='query' and
# `graph_query` scope='sql'). `QueryMixin.sql()`
# (meristem/knowledge_graph/orchestration/engine_query.py — owned by a
# sibling lane this wave, NOT this file) already rejects anything whose first
# 8 characters are not SELECT/WITH/EXPLAIN, but that is a HEAD-TOKEN check
# only: it does not parse the statement. Probed empirically (see the
# WD10-A-BACKEND lane report) — all of the following reach that guard's
# ADMIT branch unexamined:
#   * a writable CTE: "WITH x AS (INSERT ... RETURNING id) SELECT * FROM x"
#   * a ';'-stacked second statement: "SELECT 1; DROP TABLE nodes;"
#   * "EXPLAIN ANALYZE <mutation>" (ANALYZE mode EXECUTES the wrapped
#     statement in every SQL engine that implements it)
# This gate is additive and strictly narrower than the engine-side one — it
# never admits anything the engine-side guard would reject, only rejects
# more. It does NOT and cannot defend against a side-effecting/volatile
# function called from an otherwise-legal SELECT list (e.g.
# "SELECT pg_read_file(...)"-class builtins) — that requires a function
# ALLOWLIST enforced by the engine's own SQL executor (eg repo, out of this
# lane's ownership this wave); filed as a finding in the lane report rather
# than reached into.
_TABLE_QUERY_HEAD_RE = re.compile(r"^\s*(SELECT|WITH|EXPLAIN)\b", re.IGNORECASE)


_TABLE_QUERY_EXPLAIN_ANALYZE_RE = re.compile(r"^\s*EXPLAIN\s+ANALYZE\b", re.IGNORECASE)


_TABLE_QUERY_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_TABLE_QUERY_WRITE_TOKENS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "create",
        "grant",
        "revoke",
        "copy",
        "merge",
        "call",
        "exec",
        "execute",
        "vacuum",
        "reindex",
        "attach",
        "detach",
        "pragma",
        "analyze",
    }
)


def _table_query_statements(sql: str) -> list[str]:
    """Split on top-level ``;`` — a stacked second statement is the classic
    defeat of a head-token-only guard (a trailing ``;`` alone is harmless)."""
    return [part.strip() for part in sql.split(";") if part.strip()]


def _reject_unsafe_table_sql(sql: str) -> str | None:
    """Return an error string when ``sql`` fails this gate's stricter shape
    check, else ``None``. See the module comment above for what this closes
    and what it deliberately does NOT (function-call side effects)."""
    statements = _table_query_statements(sql)
    if len(statements) != 1:
        return "query must be a single statement (no ';'-stacked statements)"
    statement = statements[0]
    if not _TABLE_QUERY_HEAD_RE.match(statement):
        return "query must start with SELECT, WITH, or EXPLAIN"
    if _TABLE_QUERY_EXPLAIN_ANALYZE_RE.match(statement):
        return "EXPLAIN ANALYZE executes the wrapped statement; use plain EXPLAIN"
    tokens = {t.lower() for t in _TABLE_QUERY_TOKEN_RE.findall(statement)}
    banned = sorted(tokens & _TABLE_QUERY_WRITE_TOKENS)
    if banned:
        return f"query contains a disallowed keyword: {banned[0]}"
    return None


_NODE_LINK_SOURCE_KEYS: tuple[str, ...] = ("source", "start", "_start", "from")


_NODE_LINK_TARGET_KEYS: tuple[str, ...] = ("target", "end", "_end", "to")


_NODE_LINK_ID_KEYS: tuple[str, ...] = ("id", "node_id", "_id")


_NODE_LINK_RESERVED_EDGE_KEYS: frozenset[str] = frozenset(
    _NODE_LINK_SOURCE_KEYS
    + _NODE_LINK_TARGET_KEYS
    + ("type", "label", "properties", "id")
)


_NODE_LINK_RESERVED_NODE_KEYS: frozenset[str] = frozenset(
    _NODE_LINK_ID_KEYS + ("label", "type", "properties")
)


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _node_link_endpoint_id(value: Any) -> Any:
    """A relationship endpoint may itself be a nested node dict or a bare id."""
    if isinstance(value, dict):
        return _first_present(value, _NODE_LINK_ID_KEYS)
    return value


def _node_link_edge_endpoints(value: dict[str, Any]) -> tuple[Any, Any] | None:
    source = _first_present(value, _NODE_LINK_SOURCE_KEYS)
    target = _first_present(value, _NODE_LINK_TARGET_KEYS)
    if source is None or target is None:
        return None
    source_id = _node_link_endpoint_id(source)
    target_id = _node_link_endpoint_id(target)
    if source_id is None or target_id is None:
        return None
    return source_id, target_id


def _node_link_edge_properties(value: dict[str, Any]) -> dict[str, Any]:
    properties = value.get("properties")
    if isinstance(properties, dict):
        return properties
    return {k: v for k, v in value.items() if k not in _NODE_LINK_RESERVED_EDGE_KEYS}


def _as_node_link_edge(value: dict[str, Any]) -> dict[str, Any] | None:
    endpoints = _node_link_edge_endpoints(value)
    if endpoints is None:
        return None
    source_id, target_id = endpoints
    return {
        "source": source_id,
        "target": target_id,
        "label": value.get("type") or value.get("label") or "",
        "properties": _node_link_edge_properties(value),
    }


def _as_node_link_node(value: dict[str, Any]) -> dict[str, Any] | None:
    node_id = _first_present(value, _NODE_LINK_ID_KEYS)
    if node_id is None:
        return None
    properties = value.get("properties")
    if not isinstance(properties, dict):
        properties = {
            k: v for k, v in value.items() if k not in _NODE_LINK_RESERVED_NODE_KEYS
        }
    return {
        "id": node_id,
        "label": value.get("label") or value.get("type") or "",
        "properties": properties,
    }


def _absorb_node_link_value(
    value: Any, nodes: dict[Any, dict[str, Any]], links: list[dict[str, Any]]
) -> None:
    if not isinstance(value, dict):
        return
    edge = _as_node_link_edge(value)
    if edge is not None:
        links.append(edge)
        return
    node = _as_node_link_node(value)
    if node is not None:
        nodes.setdefault(node["id"], node)


def _parse_projection_request_object(request_json: str) -> dict[str, Any] | str:
    """Parse+validate ``request_json``'s outer JSON shape; the parsed dict, or
    an error string."""
    try:
        request = json.loads(request_json) if request_json else {}
    except (TypeError, ValueError):
        return "request_json must be a JSON object"
    if not isinstance(request, dict):
        return "request_json must be a JSON object"
    return request


_PROJECTION_REQUEST_STRING_FIELDS: tuple[str, ...] = (
    "scope",
    "reference_id",
    "as_of",
    "connection",
    "graph",
)


def _projection_request_params(request: dict[str, Any]) -> str:
    params = request.get("params")
    return params if isinstance(params, str) else json.dumps(params or {})


def _projection_request_kwargs(request: dict[str, Any], query: str) -> dict[str, str]:
    kwargs = {
        name: str(request.get(name) or "") for name in _PROJECTION_REQUEST_STRING_FIELDS
    }
    kwargs["scope"] = kwargs["scope"] or "local"
    kwargs["query"] = query
    kwargs["params"] = _projection_request_params(request)
    return kwargs


def _parse_projection_request(request_json: str) -> tuple[dict[str, str], str | None]:
    """Parse ``graph_projection``'s single JSON request object into
    ``_run_graph_query`` kwargs, or return ``(_, error_message)``.

    Collapses what would otherwise be 7 individual ``Field`` parameters
    (mirroring ``graph_query``'s own signature) into ONE typed request
    object — this file's `graph_table` already uses the identical
    "bundle a multi-field payload into one JSON-string param" idiom
    (``config_json``/``columns_json``/``rows_json``), so this follows the
    SAME established convention rather than introducing a new one.
    """
    request = _parse_projection_request_object(request_json)
    if isinstance(request, str):
        return {}, request
    query = str(request.get("query") or "")
    if not query:
        return {}, "request_json.query is required"
    return _projection_request_kwargs(request, query), None


def _projection_rows(payload: Any) -> list[Any]:
    """Extract the raw row list from a ``_run_graph_query`` JSON payload.

    Deliberately bypasses ``EvidenceBundle.from_payload`` here: that class's
    dict branch (``_from_embedded_bundle``) prioritizes an EMBEDDED
    ``evidence_bundle`` when present — and ``_run_graph_query_single`` always
    embeds one (``_evidence_bundle_for_rows``), whose ``claims`` are the
    epistemic CURRENCY-UPGRADE summary (id/text/kind only, and EMPTY whenever
    the connected engine has no ``explain_provenance_by_ids`` primitive —
    verified empirically, see the WD10-A-BACKEND lane report), not the raw
    per-row node/edge properties this projection needs. This mirrors
    ``EvidenceBundle._dict_payload_claims``'s own rows/results extraction
    instead, plus the fan-out ``targets`` shape ``_run_graph_query_fanout``/
    ``_run_graph_query_sql`` use.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if isinstance(rows, list):
        return rows
    results = payload.get("results")
    if isinstance(results, list):
        return results
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        return []
    merged: list[Any] = []
    for value in targets.values():
        if isinstance(value, list):
            merged.extend(value)
    return merged


def _project_node_link(rows: list[Any]) -> dict[str, Any]:
    """Project arbitrary graph-query result rows into a stable
    ``{nodes:[{id,label,properties}], links:[{source,target,label,properties}]}``
    shape — the D3/force-graph convention every 2D/3D renderer already speaks,
    so a frontend adapter never has to re-derive node/edge shape from raw
    per-dialect rows itself.
    """
    nodes: dict[Any, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            _absorb_node_link_value(value, nodes, links)
    return {"nodes": list(nodes.values()), "links": links}
