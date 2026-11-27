"""Engine routing support for arbora's one MCP server.

The served tools, their registry (the host tool table/``ACTION_TOOL_ROUTES``),
the dispatch core (``_execute_tool``), the process session and the engine
binding all live in :mod:`arbora.mcp_server.runtime` and
:mod:`arbora.mcp_server.bootstrap`. This module only holds what those tools
need on top: the multi-graph connection registry, explicit graph selection and
read fan-out, the tabular/extraction services, the tool-toggle batch read
the REST registry API serves, and the fleet-catalog writer bridge.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from arbora.api.errors import not_served
from arbora.core.environment import read_env

logger = logging.getLogger(__name__)


def get_toggle_states_batch(
    engine: Any, items: list[tuple[str, str]]
) -> dict[tuple[str, str], bool]:
    """Resolve many ``(item_type, item_id)`` toggle states in ONE round trip.

    DEFECT B fix: ``get_tools_endpoint`` used to call a single-item toggle read
    once per rendered item — one synchronous Cypher round trip each. Measured
    inventory on the production pod: 254 skill files + 68 skill-graph files +
    31 builtin tools + 66 MCP servers = 350+ sequential engine round trips in
    a single request (it did not return within 90s, nor within 180s). This
    batches every id the caller is about to render into ONE query.

    Engine facts this function must respect (both confirmed live against the
    deployed engine — getting either wrong makes the batch silently match
    nothing):

    1. ``STARTS WITH`` with a ``$param`` operand does not parse on the
       deployed engine. This uses ``IN`` with an explicit id list instead —
       index-servable via the engine's node-id fast path, O(items rendered)
       rather than O(all preferences), and already the pattern used by the
       sibling batching helper :func:`get_existing_disabled_batch`.
    2. The row-governance layer (``secured_reads.row_node_ids``) requires
       every returned row to carry an identity under ``id``/``node_id``/
       ``n.id``/``_id`` — this projects ``p.id AS id`` so a real match is not
       rejected by governance and silently reported as "enabled" (see
       the data-loss note above for what this caused).

    Fail-open on a query error (an id with no resolvable state defaults to
    enabled=True), matching the previous per-item
    default — this function only changes the ROUND-TRIP COUNT and the
    governance projection, not the toggle default semantics.
    """
    seen = list(dict.fromkeys(items))  # de-dupe, preserve order
    if not engine or not seen:
        return dict.fromkeys(seen, True)

    pref_id_by_key = {key: f"preference:toggle:{key[0]}:{key[1]}" for key in seen}
    pref_ids = list(pref_id_by_key.values())
    res = _toggle_batch_query(engine, pref_ids, seen)
    if res is None:
        return dict.fromkeys(seen, True)

    value_by_pref_id: dict[str, Any] = {}
    for row in res:
        if isinstance(row, dict) and row.get("id"):
            value_by_pref_id[str(row["id"])] = row.get("value")

    result: dict[tuple[str, str], bool] = {}
    for key in seen:
        value = value_by_pref_id.get(pref_id_by_key[key])
        result[key] = True if value is None else value == "enabled"
    return result


def _toggle_batch_query(
    engine: Any, pref_ids: list[str], seen: list[tuple[str, str]]
) -> list[Any] | None:
    """Run the batched ``Preference`` lookup for :func:`get_toggle_states_batch`.

    Fail-open: returns ``None`` on any query error, so the caller defaults
    every requested item to ``enabled=True``.
    """
    try:
        res = engine.query_cypher(
            "MATCH (p:Preference) WHERE p.id IN $pref_ids "
            "RETURN p.id AS id, p.value AS value",
            {"pref_ids": pref_ids},
        )
        if not isinstance(res, list):
            raise TypeError(f"expected a list of rows, got {type(res).__name__}")
    except Exception as exc:
        logger.error(
            "get_toggle_states_batch(%d items) failed — defaulting every "
            "item to enabled=True: %s",
            len(seen),
            exc,
            exc_info=True,
        )
        return None
    return res


_TOGGLE_NODE_ID_PREFIX: dict[str, str] = {
    "mcp_server": "mcp_server_",
    "builtin_tool": "native_tool_",
    "skill": "skill_",
    "skill_workflow": "skill_workflow_",
    "skill_graph": "skill_graph_",
}


def _trino_principal_ref() -> str:
    """Resolve the current verified caller's opaque persistence reference."""

    from scion_sdk.privacy import persistence_reference

    from arbora.security.actor_context import current_actor

    actor = current_actor()
    actor.ensure_credential_current()
    if not actor.authenticated or not str(actor.actor_id or "").strip():
        raise PermissionError("verified Trino principal identity is unavailable")
    return persistence_reference("delegated_actor", actor.actor_id)


def get_tabular_query_service() -> Any:
    """Compose the one tabular query application service for MCP/REST callers.

    Process configuration is bound here, above the backend and application
    layers. Credential providers remain request-scoped callables and are
    resolved by the backend on every dispatch.
    """

    raise not_served(
        "knowledge_graph.backends.trino_backend", "MEDULLA-FEDERATED-QUERY-R053.700"
    )


def _get_extraction_manager(engine: Any) -> Any:
    """Refuse: the extraction job manager is not served here (KG-2.65)."""
    del engine
    raise not_served(
        "knowledge_graph.extraction.job_manager", "MEDULLA-DURABLE-KERNEL-R071"
    )


# ── CONCEPT:MERISTEM-KG.backend.multi-connection-registry — Named multi-connection graph registry ────────────────
def get_connection_registry():
    """Refuse: the named connection registry is not served here.

    The reserved ``"default"`` target resolves only the process-active authority;
    no registry is held at module level.
    """
    raise not_served(
        "knowledge_graph.core.connection_registry",
        "MEDULLA-UNIFIED-DATA-PLANE-R612",
    )


def _resolve_target_engines(
    target: Any,
) -> tuple[list[tuple[str, Any]], dict[str, str], bool]:
    """Resolve a tool ``target`` into live engines for execution.

    Returns ``(entries, errors, fanout)`` where ``entries`` is a list of
    ``(name, engine)`` to run against and ``errors`` maps any name that could not
    be resolved to its error string. For a non-fan-out target, resolution errors
    propagate (fail-loud); for fan-out they are captured into ``errors`` so one
    bad connection never aborts the others (partial-success contract).
    """
    registry = get_connection_registry()
    names, fanout = registry.resolve_names(target)
    entries: list[tuple[str, Any]] = []
    errors: dict[str, str] = {}
    for name in names:
        if fanout:
            engine, err = registry.safe_get_engine(name)
            if err is not None:
                errors[name] = err
            else:
                entries.append((name, engine))
        else:
            entries.append((name, registry.get_engine(name)))
    return entries, errors, fanout


def _resolve_read_engines(
    target: Any,
) -> tuple[list[tuple[str, Any]], dict[str, str], bool]:
    """Resolve a READ tool's ``target`` into engines, unioning content graphs.

    CONCEPT:MERISTEM-KG.ingest.unified-query-routing — preserve unified query under ingestion graph routing. When
    routing is on and the caller did NOT pin an explicit target, content lives
    spread across per-source graphs (``code:*`` / ``src:*`` / …) that the single
    default engine cannot see. This resolver returns one engine per active content
    graph (plus the default) with ``fanout=True``, so the existing fan-out machinery
    unions them and a node written to ``code:X`` stays findable via the normal
    ``graph_search`` / ``graph_query`` path. An explicit ``target`` (a named
    connection, ``"all"``, a list) defers to the standard connection resolver
    unchanged, and with routing off this is byte-for-byte ``_resolve_target_engines``.
    """
    raise not_served(
        "knowledge_graph.core.ingest_routing", "MEDULLA-INGEST-WORKER-R605"
    )


class GraphSelectionError(ValueError):
    """Base for an explicit ``graph`` selection that cannot be honored.

    CONCEPT:MERISTEM-KG.backend.explicit-graph-selection — ``connection`` (a backend
    alias, CONCEPT:MERISTEM-KG.backend.multi-connection-registry, resolved by
    :class:`~meristem.knowledge_graph.core.connection_registry.ConnectionRegistry`)
    and ``graph`` (a physical engine graph, the thing ``engine_tenants(action="list")``/
    the engine's own ``ListGraphs`` returns) are two independent axes. Every raise
    of this family is FAIL-CLOSED: the caller gets a typed error, never a silent
    fallback to a default graph and never a union across graphs.
    """


class GraphNotFoundError(GraphSelectionError, LookupError):
    """The requested ``graph`` is absent from the engine's own catalog."""


class GraphSelectionConflictError(GraphSelectionError):
    """The ``graph``/``connection`` combination is ambiguous or unsupported.

    Raised instead of silently picking one interpretation — e.g. an explicit
    ``graph`` alongside a fan-out ``connection`` (``'all'``/a list), or an
    explicit ``graph`` alongside a non-native (external/read-only) connection
    that has no physical-graph concept of its own.
    """


def resolve_explicit_graph(
    entries: list[tuple[str, Any]], graph: str, *, fanout: bool
) -> list[tuple[str, Any]]:
    """Validate an explicit ``graph`` against the already-resolved connection(s).

    Returns ``entries`` unchanged (no-op) when ``graph`` is empty — existing
    ``connection``-only behavior is untouched. When ``graph`` is set:

    * a fan-out ``connection`` (``entries`` has more than one, or ``fanout`` is
      True) is rejected — an explicit graph requires exactly one connection,
      never a union (CONCEPT:MERISTEM-KG.backend.explicit-graph-selection).
    * a non-``"default"`` connection is rejected — only the native (epistemic-
      graph) connection has a multi-graph concept; naming a graph for e.g. an
      external Neo4j connection would otherwise be silently ignored (a silent
      pick of that connection's own implicit graph), which is exactly the
      defect this function exists to prevent.
    * existence is checked, best-effort, against the SAME catalog
      ``engine_tenants(action="list")``/``ListGraphs`` already exposes
      (``engine.graph_compute.client.tenants.list()``) — never a parallel
      authorization mechanism. The actual per-call authorization remains the
      engine's own RBAC/RLS (``crates/eg-core/src/isolation.rs::check_access``),
      evaluated server-side on every request this binds into, regardless of
      whether this probe ran.
    """
    graph = graph.strip() if isinstance(graph, str) else ""
    if not graph:
        return entries
    if fanout or len(entries) != 1:
        raise GraphSelectionConflictError(
            "an explicit graph requires exactly one resolved connection, "
            "never a fan-out/union"
        )
    name, engine = entries[0]
    if name != "default":
        raise GraphSelectionConflictError(
            f"connection {name!r} has no physical-graph concept; explicit "
            "graph selection is supported only on the default connection"
        )
    _validate_graph_exists_in_catalog(engine, graph)
    return entries


def resolve_graph_targets(
    resolve: Any, connection: Any, graph: str
) -> tuple[list[tuple[str, Any]], Any, bool] | str:
    """``resolve(connection)`` then :func:`resolve_explicit_graph`.

    Returns ``(entries, errors, fanout)``, or the public error text a tool
    returns as-is: ``graph_not_found``/``graph_selection_conflict`` for a bad
    explicit graph, otherwise the resolution failure.
    """
    from arbora.security.error_surface import public_error_text

    try:
        entries, errors, fanout = resolve(connection)
        entries = resolve_explicit_graph(entries, graph, fanout=fanout)
    except GraphNotFoundError as e:
        return public_error_text(e, code="graph_not_found")
    except GraphSelectionConflictError as e:
        return public_error_text(e, code="graph_selection_conflict")
    except Exception as e:
        logger.warning("operation failed", exc_info=True)
        return public_error_text(e)
    return entries, errors, fanout


def _validate_graph_exists_in_catalog(engine: Any, graph: str) -> None:
    """Best-effort existence check for ``graph`` against the engine's catalog.

    A degraded/unavailable catalog probe must never itself deny or (worse)
    silently permit — the engine's own RBAC/RLS is the real authorization
    boundary either way, so this just skips the check in that case and lets
    the actual call surface whatever the engine decides.
    """
    tenants = getattr(
        getattr(getattr(engine, "graph_compute", None), "client", None),
        "tenants",
        None,
    )
    list_graphs = getattr(tenants, "list", None)
    if not callable(list_graphs):
        return
    try:
        catalog = list_graphs() or []
    except Exception:
        logging.getLogger(__name__).debug(
            "best-effort probe; see docstring", exc_info=True
        )
        return
    names = {
        row.get("name") for row in catalog if isinstance(row, dict) and row.get("name")
    }
    if graph not in names:
        raise GraphNotFoundError(
            f"graph {graph!r} is not present in the engine catalog"
        )


@contextlib.contextmanager
def bound_to_graph(graph: str) -> Any:
    """Bind an explicit physical graph onto the verified session for one call.

    A no-op when ``graph`` is empty. Otherwise reuses the SAME sanctioned
    session-retargeting primitive already used by
    ``meristem.knowledge_graph.pipeline`` and
    ``arbora.access.approval`` —
    :meth:`~arbora.security.session.GraphSession.with_graph`
    scoped for the call's duration via
    :func:`~arbora.security.session.use_session`. This
    introduces no new authority: the engine's own RBAC/RLS
    (``crates/eg-core/src/isolation.rs::check_access``) evaluates
    ``(agent_id, graph, action)`` on every RPC issued while bound, independent
    of what this function does — binding only says which graph the request
    NAMES, never what it is ALLOWED to touch.
    """
    graph = graph.strip() if isinstance(graph, str) else ""
    if not graph:
        yield
        return
    from arbora.security.session import (
        current_session,
        use_session,
    )

    session = current_session()
    if session is None:
        raise GraphSelectionConflictError(
            "a verified session is required to bind an explicit graph"
        )
    with use_session(session.with_graph(graph)):
        yield


#: Per-target wall-clock budget (seconds) for a fan-out (``target='all'`` or a
#: multi-target list). One slow/unreachable backend must not stall the whole set;
#: override live via ``graph_config set GRAPH_FANOUT_TIMEOUT`` (KG-2.63).
DEFAULT_FANOUT_TIMEOUT_S = 30.0

#: Per-target wall-clock budget (seconds) for an IMPLICIT-default read fan-out —
#: a ``graph_search``/``graph_query`` call with no explicit ``target`` that
#: resolves (CONCEPT:MERISTEM-KG.ingest.unified-query-routing) to the routed
#: content-graph union: ``default`` + every active ``code:*``/``src:*`` graph,
#: which can be dozens of per-repo connections and often includes idle/
#: unreachable ones. Using the full ``DEFAULT_FANOUT_TIMEOUT_S`` there means one
#: unreachable ``code:<repo>`` backend blocks the common no-target call for up to
#: 30s each, flooding the result with "timed out" entries. A short budget keeps
#: the default call fast — an unreachable graph is skipped, not waited on — while
#: an explicit ``target='all'``/list (a deliberate cross-repo search) keeps the
#: full ``DEFAULT_FANOUT_TIMEOUT_S``.
DEFAULT_CONTENT_FANOUT_TIMEOUT_S = 3.0


def fanout_error_label(exc: BaseException) -> str:
    """Classify a per-target fan-out failure as retryable-degraded or rejected.

    BUG-048: ``fanout_execute`` (below) and the "primary target" call sites
    that deliberately bypass its concurrency machinery but must mirror its
    error handling (e.g. ``graph_query``'s ``default``-connection fast path
    in ``query_tools.py``) reduce a raised exception to a bare string outside
    the ``OperationResult``/``OperationError`` schema ``public_error_payload``
    covers. Without this, a breaker-open on one fan-out target collapsed to
    the SAME ``"target_operation_failed"`` label as a genuinely rejected
    query — indistinguishable to a caller deciding whether to retry, exactly
    the defect BUG-048 fixed at the single-target boundary. Reuses
    :func:`arbora.security.error_surface.is_engine_degraded` — the
    one place this breaker-vs-rejection vocabulary is defined — rather than
    reimplementing it a third time.
    """
    from arbora.security.error_surface import is_engine_degraded

    return "target_degraded" if is_engine_degraded(exc) else "target_operation_failed"


def fanout_execute(entries, fn, *, timeout=None):
    """Run ``fn(name, engine)`` for every fan-out target CONCURRENTLY under a shared
    per-target wall-clock timeout, so one slow/unreachable backend can't stall the
    others (CONCEPT:MERISTEM-KG.backend.multi-connection-registry).

    Returns ``(results, errors)`` keyed by connection name. A target that exceeds the
    budget (or raises) lands in ``errors`` while the rest still return — the
    partial-success contract the sequential loop violated by blocking on the slowest.
    A raised target is labeled via :func:`fanout_error_label` — ``"target_degraded"``
    for a breaker-open/transport-down failure (retryable), ``"target_operation_failed"``
    for everything else (BUG-048) — never the exception text itself.

    B-18: ``concurrent.futures.ThreadPoolExecutor.submit`` does NOT propagate the
    calling thread's :mod:`contextvars` context — unlike ``asyncio.to_thread``
    (which the async tool endpoints above use to reach this synchronous helper
    in the first place), a plain worker thread starts with a FRESH, empty
    context. Without this, the ambient authenticated ``GraphSession`` (and any
    narrowing a caller applies via ``bound_to_graph`` around ``fn``) is
    invisible inside every fan-out target, and each one fails closed with
    ``SessionRequiredError`` regardless of which graph it targets. Capturing
    ``contextvars.copy_context()`` once PER submission (never reused across
    concurrent ``Context.run`` calls, which is not reentrant) and running
    ``fn`` inside that copy restores the ambient session per target while
    keeping each target's own narrowing (if any) isolated from its siblings.
    """
    import concurrent.futures
    import contextvars

    if timeout is None:
        timeout = float(read_env("GRAPH_FANOUT_TIMEOUT", DEFAULT_FANOUT_TIMEOUT_S))
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if not entries:
        return results, errors
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(entries)))
    futures = {
        ex.submit(contextvars.copy_context().run, fn, name, engine): name
        for name, engine in entries
    }
    done, not_done = concurrent.futures.wait(futures, timeout=timeout)
    for fut in done:
        name = futures[fut]
        try:
            results[name] = fut.result()
        except Exception as exc:  # partial-success contract
            logger.warning(
                "Graph fan-out target failed (exception_type=%s)",
                type(exc).__name__,
                exc_info=True,
            )
            errors[name] = fanout_error_label(exc)
    for fut in not_done:
        errors[futures[fut]] = "target_timeout"
    # Never block on a hung backend's thread; let it finish in the background.
    ex.shutdown(wait=False, cancel_futures=True)
    return results, errors


def _ontology_system():
    """Return an OntologySystem bound to the live engine store (or offline).

    Module-level so the ontology/object tool group registers from
    mcp/tools/ontology_tools.py instead of a _build_server closure.
    """
    raise not_served("knowledge_graph.facade", "MEDULLA-CONTRACT-R005")

    # Fleet tool schemas stay lazy.  Startup has already materialized each MCP
    # server declaration above; probing every child here would launch the whole
    # fleet and contend with an operator's targeted ``list_catalog`` call.
    # Explicit ``source_sync(source="fleet")`` remains the governed full-scan
    # path when an operator wants every live tool schema elevated into the KG.


# ── Boot hydration plan (ingestion-hydration-program.md §3) ─────────────────
#
# ``_ingest_capabilities`` above (mcp_config.json / native tools / skills) is
# the ORIGINAL boot hydration; the two helpers below extend it with the
# capability legs Phases C and E built but never wired to a boot call. Each is
# its own best-effort, exception-isolated step — same shape as the ontology
# federation sync already nested inside :func:`_start_engine_bootstrap`'s
# background thread — so a failure in one never skips, or blocks serving for,
# the others. Fleet tool-schema ingestion (Phase A) and the mcp_config.json
# router (Phase B) need no boot call here: A rides its own hourly
# ``deploy/schedules.yml`` cadence (``fleet-tool-schema-sync``) and B rides the
# always-on codebase sweep's ``_route_classified_artifacts`` fan-out.


# ══════════════════════════════════════════════════════════════════
