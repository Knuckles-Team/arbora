"""New engine-surface MCP tools — CONCEPT:MERISTEM-KG.coordination.engine-message-broker.

medulla v2.2.0 grew several engine capabilities that meristem
should expose to the fleet as first-class, purpose-shaped MCP tools rather than
leaving them reachable only through the generic ``engine_<domain>`` 1:1 surface
(``engine_tools.py``). This module ADDS those ergonomic wrappers:

* ``graph_broker``          — the message broker (declare exchange/queue, bind,
  publish, consume, stats — AMQP-style).
* ``graph_kvcache``         — the shared, content-addressed KV-cache over the
  MEDULLA-187 HTTP surface, driven through the KG-2.306
  :class:`~meristem.kvcache.MedullaKVBackend` connector.
* ``graph_federated_search``— federated search fanned across registered external
  graph references.
* ``graph_promql``          — PromQL instant/range metric queries (observability).
* ``graph_traces``          — distributed-trace search / fetch (observability).
* ``graph_gis``             — geospatial route / tile / geo-task ops.
* ``graph_viz``             — GOC-88 (D-VZ-1 lane V5): native visualization —
  ``plot_from_query`` (SQL → InlineColumns → render), ``export_chart`` (an
  already-built ViewSpec/dataset pass-through), ``describe_chart`` (an
  LLM-facing summary of a ``ViewResult``), ``capability_matrix``. The
  ergonomic layer over the raw ``engine_viz`` 1:1 passthrough.
* ``graph_memory``          — the MEDULLA-318 memory surface: episodic→semantic memory
  (create-summary / consolidate / maintain), the spatial scene graph
  (add-scene-object / world-transform), and RL trajectories (start-trajectory /
  append-step / discounted-return), plus their reads.

Design (matches the rest of ``arbora/mcp_server/backing/tools``):

* **Reuse the existing engine transport** — every client-backed tool resolves the
  same :class:`~medulla.client.SyncMedullaClient` that
  ``engine_tools`` uses (via :func:`engine_tools._client_for`); the KV-cache tool
  reuses the KG-2.306 HTTP connector. No new transport is invented.
* **Additive + gated / graceful degradation** — the v2.2.0 surfaces may not be
  present in the connected engine build (or wired into the ``medulla``
  client yet). Each tool probes a small set of candidate ``client.<sub>.<method>``
  paths and, when none resolve, returns a clean ``degraded`` payload instead of
  raising — so the fleet can call these today and they light up automatically once
  the engine ships the capability.
* **Two surfaces** — each tool registers a ``/graph/<name>`` REST twin in
  ``ACTION_TOOL_ROUTES`` (auto-mounted by the generic factory), keeping MCP⇄REST
  parity like every other tool.

CONCEPT:MERISTEM-KG.coordination.engine-message-broker — MCP surface for the new engine ops (broker / kvcache /
federated-search / promql / traces / gis).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arbora.api.errors import not_served

# CONCEPT:MERISTEM-KG.mining.dsm-forecast-delegation — the fleet write-side connector (call
# a named MCP server's tool once, synchronously, decoded) is the SAME primitive the
# governed ``fleet.write_record`` ontology Action uses
# (``meristem/knowledge_graph/actions/fleet_writeback.py``) — reused here,
# not reinvented, so ``graph_mine_deep`` reaches data-science-mcp exactly like every
# other fleet-delegation call site. Neither module imports torch/transformers.
from arbora.mcp_server.backing.tools.deep_tools import register_deep_tools
from arbora.mcp_server.backing.tools.engine_surface_common import (
    _invoke,
    _json_default,
    _surface_error,
)
from arbora.mcp_server.backing.tools.kv_cache_tools import register_kv_cache_tools
from arbora.mcp_server.backing.tools.mining_tools import register_mining_tools
from arbora.mcp_server.backing.tools.trace_tools import register_trace_tools
from arbora.mcp_server.backing.tools.viz_tools import register_viz_tools
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


class _EngineComputeAdapter:
    """Minimal ``compute`` shape (``._client`` + ``.graph_name``) the MediaStore-style
    graph-resource stores expect, built from the low-level engine client this module
    already resolves (CONCEPT:MERISTEM-KG.memory.kv-checkpoint-resource). No new transport —
    reuses the same :func:`_client` every other tool in this module rides."""

    def __init__(self, client: Any, graph_name: str) -> None:
        self._client = client
        self.graph_name = graph_name


def _checkpoint_failure(exc: Exception, action: str) -> str:
    """The surface error for a ``KVCheckpointError`` raised by ``action``."""
    return _surface_error(
        exc, surface="kv_checkpoint", action=action, code=_checkpoint_error_code(exc)
    )


def _checkpoint_outcome(action: str, outcome: Any, *, advisory: bool = False) -> str:
    """The JSON response for one checkpoint ``outcome``."""
    body: dict[str, Any] = {
        "surface": "kv_checkpoint",
        "action": action,
        "result": outcome.model_dump(mode="json", exclude={"recommendation"}),
    }
    if advisory:
        body["advisory"] = (
            outcome.recommendation.as_advisory() if outcome.recommendation else ""
        )
    return json.dumps(body, default=_json_default)


def _checkpoint_error_code(exc: Exception) -> str:
    """Map a :class:`~meristem.kvcache.KVCheckpointError` to one of the
    platform's registered public error codes (CONCEPT:MERISTEM-KG.memory.kv-checkpoint-resource).

    Deliberately coarse: :func:`~arbora.security.error_surface.public_error_payload`
    only recognizes a small fixed code vocabulary and collapses anything else to
    ``operation_failed`` — and collapsing here is itself the correct security
    posture, not a limitation. Distinguishing "not found" from "cross-tenant" from
    "stale" on the PUBLIC surface would let a caller enumerate which checkpoint ids
    exist under a tenant they don't own; only ``CrossTenantCheckpointError`` gets
    its own (still generic) ``permission_denied`` code, everything else falls to
    the default ``operation_failed``.
    """
    raise not_served("kvcache", "MEDULLA-DURABLE-KERNEL-R800")


def _bundle_object(raw: str) -> dict[str, Any]:
    """Decode a caller-supplied bundle, refusing anything that is not an object."""
    bundle = json.loads(raw)
    if not isinstance(bundle, dict):
        raise ValueError("bundle JSON must be an object")
    return bundle


def _measured_fields(observation: Any) -> dict[str, Any]:
    """The axes a bundle adapter actually populated.

    ``exclude_defaults``/``exclude_none`` together mean an axis the bundle carried no
    evidence for stays *unset* rather than being merged in as a spurious zero — the same
    not-measured-is-not-zero rule the scorers depend on.
    """
    return observation.model_dump(exclude_defaults=True, exclude_none=True)


def _session_bound_tenant(declared: str) -> str:
    """Return the tenant a KV-checkpoint operation runs under, bound to the session.

    CONCEPT:MERISTEM-OS.governance.authority-derived-persistence-eligibility. When a verified
    ``GraphSession`` is bound (every served boundary), ITS tenant is authoritative and a
    payload tenant that disagrees is REFUSED rather than quietly preferred — a caller
    may not name someone else's tenancy. With no session bound (an isolated in-process
    harness) the declared value is used for RAM-tier isolation only; durable persistence
    is refused regardless, because the eligibility gate has no authority to read.
    """
    try:
        from arbora.security.session import current_session

        session = current_session()
    except Exception:
        logging.getLogger(__name__).debug(
            "no bound session is a legitimate RAM-only case", exc_info=True
        )
        return declared
    if session is None:
        # ``current_session`` returns None (rather than raising) when authority has
        # been explicitly suspended — the same RAM-only case as the exception path.
        return declared
    verified = session.tenant
    if declared and declared != verified:
        raise ValueError(
            f"tenant {declared!r} does not match the verified session tenant "
            f"{verified!r}; a checkpoint operation may not name another tenancy"
        )
    return verified


def _declared_source_entries(sources_json: str) -> tuple[list[Any], list[str]]:
    """Split an explicit ``sources_json`` list into (labelled objects, bare refs).

    A bare ref string is left for the :class:`SourceLabelResolver`; a labelled
    object is taken as given. Anything else is a caller error.
    """
    raise not_served("kvcache.eligibility", "MEDULLA-DURABLE-KERNEL-R800")


def _citation_refs(citation: Any) -> list[str]:
    """One citation's ``source_refs``, as strings; ``[]`` for a non-object citation."""
    if not isinstance(citation, dict):
        return []
    return [str(r) for r in (citation.get("source_refs") or [])]


def _bundle_citation_refs(context_bundle_json: str) -> list[str]:
    """Every citation's ``source_refs`` in a context bundle, in order.

    The bundle's citations ARE the sources that actually contributed, so a
    caller that hands over its context bundle automatically hands over its
    provenance.
    """
    bundle_raw = (context_bundle_json or "").strip()
    if not bundle_raw or bundle_raw in {"{}", "[]"}:
        return []
    bundle = json.loads(bundle_raw)
    if not isinstance(bundle, dict):
        return []
    refs: list[str] = []
    for citation in bundle.get("citations") or []:
        refs.extend(_citation_refs(citation))
    return refs


def _contributing_sources(
    sources_json: str, context_bundle_json: str, *, tenant: str
) -> tuple[Any, ...]:
    """Resolve the sources that contributed to the checkpointed context.

    CONCEPT:MERISTEM-OS.governance.authority-derived-persistence-eligibility — the material
    half of the derivation. Two inputs, in precedence order:

    1. ``sources_json`` — an explicit list of source refs (strings) or fully-labelled
       source objects. A bare ref is resolved through the registered
       :class:`SourceLabelResolver`; a labelled object is taken as given.
    2. ``context_bundle_json`` — the SAME bundle the worthiness scorers already read.
       Its citations' ``source_refs`` are the sources that actually contributed, so the
       common path needs no extra argument at all: a caller that hands over its context
       bundle automatically hands over its provenance.

    Every ref is resolved; a ref whose labels cannot be read comes back **unlabelled**
    (and therefore denies, naming itself). This function never returns fewer entries
    than it was given refs.
    """
    raise not_served("kvcache.eligibility", "MEDULLA-DURABLE-KERNEL-R800")


def _waterfall_span_node(span: Any, trace: Any) -> dict[str, Any]:
    """One SpanNode flattened into the waterfall's nested-duration node shape."""
    return {
        "id": span.id,
        "parentId": span.parent_span_id or getattr(trace, "id", None),
        "kind": span.span_kind,
        "name": span.name,
        "latencyMs": span.latency_ms or 0,
        "error": span.error,
    }


def _waterfall_generation_node(gen: Any, trace: Any) -> dict[str, Any]:
    """One GenerationNode flattened into the waterfall's node shape (+ token/cost)."""
    return {
        "id": gen.id,
        "parentId": gen.parent_span_id or getattr(trace, "id", None),
        "kind": "generation",
        "name": gen.name,
        "latencyMs": gen.latency_ms or 0,
        "model": gen.model,
        "costUsd": gen.total_cost_usd,
        "inputTokens": gen.input_tokens,
        "outputTokens": gen.output_tokens,
        "error": gen.error,
    }


def _waterfall_trace_header(trace: Any, trace_id: str) -> dict[str, Any]:
    """The trace-level roll-up the waterfall renders above its nodes."""
    return {
        "id": getattr(trace, "id", trace_id),
        "name": getattr(trace, "name", ""),
        "status": getattr(trace, "status", "ok"),
        "latencyMs": getattr(trace, "latency_ms", None),
        "costUsd": getattr(trace, "total_cost_usd", None),
        "inputTokens": getattr(trace, "input_tokens", 0),
        "outputTokens": getattr(trace, "output_tokens", 0),
        "toolCalls": getattr(trace, "tool_calls", 0),
    }


def _waterfall_result(entry: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """Flatten one sink trace entry into ``{"trace": ..., "nodes": [...]}``."""
    trace = entry.get("trace")
    nodes: list[dict[str, Any]] = [
        _waterfall_span_node(span, trace) for span in entry.get("spans", []) or []
    ]
    nodes.extend(
        _waterfall_generation_node(gen, trace)
        for gen in entry.get("generations", []) or []
    )
    return {"trace": _waterfall_trace_header(trace, trace_id), "nodes": nodes}


def _trace_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw ``TraceBackend.get_traces``/``get_trace_summary`` dict
    (``id``/``duration_ms``) into ``graph_traces``' own ``trace_id``/``duration``
    row shape (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
    return {
        "trace_id": raw.get("id") or raw.get("trace_id"),
        "name": raw.get("name"),
        "status": raw.get("status"),
        "duration": raw.get("duration_ms", raw.get("duration")),
        "error": raw.get("error"),
    }


def _trace_search_needle(service: str, operation: str, query: str) -> str:
    """One case-folded filter string from the three client-side search filters."""
    return " ".join(v for v in (service, operation, query) if v).strip().lower()


def _trace_row_matches(row: dict[str, Any], needle: str) -> bool:
    """Whether a normalized trace row contains the case-folded needle anywhere."""
    return needle in " ".join(str(v) for v in row.values() if v).lower()


def _require_process_perspective(params: dict[str, Any]) -> Any:
    """Mint the explicit, versioned ``ProcessPerspective`` a flattening needs.

    CONCEPT:MERISTEM-KG.mining.governed-perspective-flattening — classical
    single-case flattening (one trace per object) is a real information loss
    over the object-centric source truth, so ``graph_mine(action="process")``
    refuses to derive traces from a bare ``object_type`` string: a caller must
    also disclose WHICH versioned analytical selection produced them via
    ``perspective_id``/``derivation_version``. Pops the three params it
    consumes; raises ``ValueError`` (caught by the caller's existing
    invalid-request handling) when any is missing.
    """
    raise not_served(
        "knowledge_graph.ingestion.semantic_event_model", "MEDULLA-REPO-INGEST-R002"
    )


# ── graph_viz helpers (GOC-88, D-VZ-1 lane V5) ──────────────────────────────


def _ocel_evidence(tenant: str, slice_: Any, envelope: Any) -> dict[str, Any]:
    """The tEKG evidence snapshot for one imported OCEL slice + its envelope."""
    return {
        "mode": "ocel_2.0",
        "tenant": tenant,
        "content_hash": slice_.canonical_digest(),
        "idempotency_key": envelope.idempotency_key,
        "mapping_version": slice_.mapping_version,
        "node_count": len(envelope.typed_payload["entities"]),
        "relationship_count": len(envelope.typed_payload["relationships"]),
    }


def _record_process_signal_evidence(
    evidence: dict[str, Any], engine: Any = None
) -> None:
    """Best-effort Unified-Evidence ``process_signal`` write.

    CONCEPT:MERISTEM-KG.evolution.unified-evidence-resource (D-71-1) — recorded at
    the one place the import's real outcome is computed, never re-derived by a
    second query. A pure observability side-channel: it must never gate the
    import, so every failure is swallowed at DEBUG. ``engine`` is resolved
    lazily *inside* the guard when the caller has none yet, so a failing
    engine resolution degrades exactly like a failing record.
    """
    exc = not_served(
        "knowledge_graph.research.evidence", "MEDULLA-DECISION-ENGINE-R652"
    )
    logger.debug(
        "OCEL process_signal evidence record failed for %s: %s",
        evidence.get("idempotency_key"),
        type(exc).__name__,
    )


def _ocel_replay_deriver(slice_: Any) -> Any:
    """Replay an imported OCEL slice through the incremental deriver.

    CONCEPT:MERISTEM-KG.mining.incremental-object-centric-derivation — read-only by
    construction; commits nothing.
    """
    raise not_served(
        "knowledge_graph.ingestion.object_centric_derivation",
        "MEDULLA-REPO-INGEST-R002",
    )


def _ocel_derive_response(
    action: str, slice_: Any, tenant: str, evidence: dict[str, Any]
) -> str:
    """``ocel_mode='derive'`` — the read-only incremental-derivation replay.

    The ONE surface exposure of ``ingestion/object_centric_derivation.py``
    (previously built-but-not-wired). Returns the derived aggregate
    directly-follows graph, per-object timelines, and derivation generation.
    It commits nothing — that stays exclusive to ``mine``.
    """
    deriver = _ocel_replay_deriver(slice_)
    object_ids = sorted(
        {
            participation.object_id
            for event in slice_.events
            for participation in event.objects
        }
    )
    return json.dumps(
        {
            "surface": "mining",
            "action": action,
            "ocel_mode": "derive",
            "tenant": tenant,
            "generation": deriver.generation,
            "directly_follows": [
                {"from": pair[0], "to": pair[1], "count": count}
                for pair, count in sorted(deriver.dfg_snapshot().items())
            ],
            "timelines": {
                object_id: [event.event_id for event in deriver.timeline(object_id)]
                for object_id in object_ids
            },
            "tekg": evidence,
        },
        default=_json_default,
    )


def _ocel_mine(
    action: str,
    params: dict,
    slice_: Any,
    tenant: str,
    provenance: Any,
    exported: Any,
) -> str | tuple[Any, dict[str, Any]]:
    """``ocel_mode='mine'`` — commit source truth + the disclosed perspective.

    Returns a ready-to-send JSON error string, or ``(projection, evidence)``
    for the caller to fold into the trace-mining response.

    ``mine`` mode always materializes source truth AND discloses any
    case-notion flattening it derives from that same truth
    (CONCEPT:MERISTEM-KG.mining.governed-perspective-flattening) — the perspective
    used for the trace projection is folded into the SAME committed slice as a
    real, versioned ``ProcessPerspective`` node, never a silent side channel.
    """
    del exported
    raise not_served(
        "knowledge_graph.ingestion.envelope_ingest", "MEDULLA-REPO-INGEST-R002"
    )


def _popped_str(params: dict, key: str, *, strip: bool = True) -> str:
    """``params.pop(key)`` coerced to a plain (optionally stripped) string.

    The ``str(params.pop(k, "") or "")`` idiom repeated across the governed
    mining entrypoints, in one place: a missing key, ``None``, ``0`` and ``""``
    all collapse to the empty string.
    """
    value = str(params.pop(key, "") or "")
    return value.strip() if strip else value


def _conformance_commit(
    inputs: dict[str, Any], run: Any, deviations: Any
) -> dict[str, Any]:
    """Commit one ``ConformanceRun`` (+ its Deviations) as a tenant-scoped slice."""
    del deviations
    raise not_served(
        "knowledge_graph.ingestion.envelope_ingest", "MEDULLA-REPO-INGEST-R002"
    )


def _mining_request_error(exc: Exception, action: str) -> str:
    """Map governed mining validation and commit failures onto public codes."""
    code = "commit_failed" if isinstance(exc, RuntimeError) else "invalid_request"
    return _surface_error(exc, surface="mining", action=action, code=code)


def _invoke_mining_projection(
    action: str, *, params: dict, graph: str, projection: Any
) -> dict[str, Any]:
    """Dispatch projected traces through the native mining surface."""
    params["traces"] = projection.engine_traces()
    response = json.loads(
        _invoke(
            surface="mining",
            action=action,
            graph=graph,
            candidates=(("mining", action),),
            params=params,
        )
    )
    response["projection"] = projection.public_metadata()
    return response


def register_engine_surface_tools(
    mcp, tools: ToolTable, *, include_unserved_mining: bool = False
) -> None:
    """Register the KG-2.310 engine-surface tools + their REST twins.

    Each tool is added to the host tool table and mapped to a ``/graph/<name>``
    route in ``ACTION_TOOL_ROUTES`` (auto-mounted by the generic REST factory), so
    MCP and REST stay in lockstep. CONCEPT:MERISTEM-KG.coordination.engine-message-broker.
    """

    register_kv_cache_tools(mcp, tools)
    register_trace_tools(mcp, tools)
    register_viz_tools(mcp, tools)
    register_deep_tools(mcp, tools)
    register_mining_tools(mcp, tools, include_unserved_mining=include_unserved_mining)
