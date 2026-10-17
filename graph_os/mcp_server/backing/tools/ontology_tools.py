"""Auto-extracted graph-os MCP tools: ontology_tools (register_ontology_tools).

Split out of runtime._build_server to deepen the MCP surface into focused
modules without changing tool behavior or names.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from graph_os.api.errors import not_served
from graph_os.mcp_server import runtime
from graph_os.mcp_server.backing import engine_routing
from graph_os.mcp_server.backing.ontology_types import (
    OntologyCatalogRequest,
)
from graph_os.security.error_surface import (
    public_error_json,
)

logger = logging.getLogger(__name__)

# BUG-1 (CRITICAL, kg-exhaustive-smoke.md): a hard ceiling on the ``object_set``
# tool's ``limit`` field so a caller can't defeat the guard by simply passing an
# absurdly large explicit limit — ``of_type``/``from_ids``/``union``/
# ``intersect``/``subtract`` used to call ``ObjectSet.ids()`` with NO bound at
# all, materializing the entire matching id set (13,793 ``Concept`` nodes out of
# 139,655 total in the deployment that OOM-killed the live graph-os pod).
_OBJECT_SET_HARD_CAP = 10_000


_ONTOLOGY_CATALOG_ACTIONS = frozenset({"load", "list", "get", "update", "delete"})


def _graph_ontology_load(
    lc: Any,
    *,
    source: str,
    source_type: str,
    iri: str,
    version: str,
) -> str:
    if not source:
        return json.dumps({"error": "load requires `source`"})
    return json.dumps(
        lc.load(
            source,
            source_type=source_type,
            version=version or None,
            iri=iri or None,
        ),
        default=str,
    )


def _graph_ontology_list(
    lc: Any,
) -> str:
    return json.dumps(lc.list_ontologies(), default=str)


def _graph_ontology_get(lc: Any, *, iri: str, version: str) -> str:
    if not iri:
        return json.dumps({"error": "get requires `iri`"})
    return json.dumps(lc.get(iri, version=version or None), default=str)


def _graph_ontology_update(
    lc: Any, *, source: str, iri: str, version: str, source_type: str
) -> str:
    if not (source and iri and version):
        return json.dumps({"error": "update requires `source`, `iri`, and `version`"})
    return json.dumps(
        lc.update(source, iri=iri, version=version, source_type=source_type),
        default=str,
    )


def _graph_ontology_delete(lc: Any, *, iri: str, version: str) -> str:
    if not iri:
        return json.dumps({"error": "delete requires `iri`"})
    return json.dumps(
        lc.delete(iri, version=version or None),
        default=str,
    )


def _graph_ontology_catalog(
    action: str, lc: Any, request: OntologyCatalogRequest
) -> str:
    if action == "load":
        return _graph_ontology_load(
            lc,
            source=request.source,
            source_type=request.source_type,
            iri=request.iri,
            version=request.version,
        )
    if action == "list":
        return _graph_ontology_list(
            lc,
        )
    if action == "get":
        return _graph_ontology_get(lc, iri=request.iri, version=request.version)
    if action == "update":
        return _graph_ontology_update(
            lc,
            source=request.source,
            iri=request.iri,
            version=request.version,
            source_type=request.source_type,
        )
    if action == "delete":
        return _graph_ontology_delete(
            lc,
            iri=request.iri,
            version=request.version,
        )
    raise ValueError(f"unknown GraphSchema lifecycle action: {action}")


def _ontology_interface_list(reg: Any, registry: str) -> str:
    return json.dumps(
        {"registry": registry, "interfaces": [i.name for i in reg.list_interfaces()]}
    )


def _ontology_interface_implementers(reg: Any, name: str) -> str:
    raise not_served("knowledge_graph.ontology.interfaces", "EG-TYPED-PACKS-R650")


def _ontology_interface_conforms(reg: Any, name: str, object_json: str) -> str:
    obj = json.loads(object_json) if object_json else {}
    return json.dumps({"interface": name, "conforms": reg.conforms(obj, name)})


def _ontology_interface_owl(reg: Any) -> str:
    return json.dumps({"owl": reg.to_owl()})


def _ontology_interface_schema(reg: Any, registry: str, action: str) -> str:
    raise not_served("knowledge_graph.ontology.links", "EG-TYPED-PACKS-R650")


def _ontology_interface_lint(reg: Any, registry: str) -> str:
    raise not_served("knowledge_graph.ontology.style_lint", "EG-TYPED-PACKS-R651")


def _ontology_interface_explain_routing() -> str:
    raise not_served(
        "graph.routing.enrichers.capability_routing", "EG-DECISION-ENGINE-R035.750"
    )


def _ontology_sampling_profile_describe(registry: Any, task_class: str) -> str:
    return json.dumps(
        registry.pick_profile_for_task(task_class).model_dump(), default=str
    )


@dataclass
class _WritebackCtx:
    """Bundled writeback destination + engine handles shared across
    `graph_writeback`'s action/flag branches."""

    target: str
    backend: Any
    engine: Any
    dry_run: bool


def _graph_writeback_proposals() -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _graph_writeback_approve(proposal_id: str, ctx: _WritebackCtx) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _graph_writeback_asset_mirror(ctx: _WritebackCtx) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _graph_writeback_inventory(ctx: _WritebackCtx) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _graph_writeback_findings(ctx: _WritebackCtx, creations_json: str) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _graph_writeback_default(
    ctx: _WritebackCtx,
    inferences_json: str,
    enrichments_json: str,
    creations_json: str,
    retirements_json: str,
    process_ids_json: str,
) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


@dataclass
class _SpecTicketCtx:
    """Bundled writeback destination + engine handles shared across
    `spec_ticket`'s link/pull branches."""

    target: str
    backend: Any
    dry_run: bool


def _spec_ticket_pull(target: str, user: str, project_id: str) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _spec_ticket_link(
    ctx: _SpecTicketCtx,
    spec_json: str,
    issue_id: str,
    project_id: str,
    assignee: str,
    agent: str,
    comment: str,
) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SDK-SOURCE-INGEST-R600",
        owner="agent-connector-sdk",
    )


def _source_sync_resolve_engine(connection: str, graph: str) -> tuple[Any, str | None]:
    """Resolve the target engine for `source_sync` per the graph/connection
    selection rules (U-37/GOC-67): an explicit graph never defaults, never fans
    out, and an unknown/unauthorized graph fails closed with no partial sync
    started. Returns ``(engine, error_json)`` — ``error_json`` is ``None`` on
    success."""
    if graph or connection:
        try:
            entries, errors, fanout = engine_routing._resolve_target_engines(connection)
            entries = engine_routing.resolve_explicit_graph(
                entries, graph, fanout=fanout
            )
        except engine_routing.GraphNotFoundError as e:
            return None, public_error_json(e, code="graph_not_found")
        except engine_routing.GraphSelectionConflictError as e:
            return None, public_error_json(e, code="graph_selection_conflict")
        except Exception as e:  # noqa: BLE001
            return None, public_error_json(e)
        if fanout or len(entries) != 1:
            return None, public_error_json(
                engine_routing.GraphSelectionConflictError(
                    "source_sync targets exactly one backend; "
                    "'connection=all'/a list is not supported "
                    "(use source='all' to fan out across connectors "
                    "instead)"
                ),
                code="graph_selection_conflict",
            )
        _sync_conn_name, engine = entries[0]
        return engine, None
    try:
        engine = runtime._get_engine()
    except Exception:  # noqa: BLE001
        engine = None
    return engine, None


def _source_sync_run(
    engine: Any, source: str, mode: str, ids: list[Any], graph: str, connection: str
) -> dict[str, Any]:
    raise not_served("knowledge_graph.core.source_sync", "EG-INGEST-WORKER-R605")


def _graph_etl_list() -> str:
    raise not_served("knowledge_graph.enrichment.registry", "EG-INGEST-WORKER-R606")


def _graph_etl_lineage(engine: Any, source: str, sink: str, limit: int) -> str:
    raise not_served("knowledge_graph.etl", "EG-UNIFIED-DATA-PLANE-R702")


def _graph_etl_resolve_sink_backend(sink: str) -> Any:
    # Write-back sinks + the native SQL-table sink need none — run_etl routes
    # sink='table' itself (KG-2.266).
    raise not_served(
        "knowledge_graph.enrichment.writeback.core", "SDK", owner="agent-connector-sdk"
    )


@dataclass
class _EtlRunArgs:
    """Bundled `graph_etl` action='run' parameters (kept under the 7-parameter
    cap as a single dataclass rather than 8 positional args)."""

    engine: Any
    source: str
    sink: str
    mode: str
    sources_json: str
    ids_json: str
    ops_json: str
    dry_run: bool


def _graph_etl_run(args: _EtlRunArgs) -> str:
    raise not_served("knowledge_graph.etl", "EG-UNIFIED-DATA-PLANE-R702")


def _classification_claim_from_json(raw: str) -> Any:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claim_dict(claim: Any) -> dict[str, Any]:
    import dataclasses

    row = dataclasses.asdict(claim)
    row["evidence_refs"] = list(row["evidence_refs"])
    return row


@dataclass
class _ClassificationClaimsCtx:
    """Bundled `ontology_classification_claims` parameters, passed as one
    object to every action handler so each stays at a single parameter
    regardless of how many of the tool's 17 fields it actually needs."""

    engine: Any
    subject_id: str
    category: str
    status: str
    claim_json: str
    new_claim_json: str
    source_snapshot: str
    policy_approved: bool
    reviewer: str
    reason: str
    viewer_clearance: str
    claim_id: str
    artifact_a_id: str
    artifact_b_id: str
    evidence_refs_json: str
    extractor_ref: str
    confidence: float
    tenant: str


def _classification_claims_record(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_query(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_categories(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_history(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_resolve_evidence(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_review(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_promote(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_reject(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_supersede(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_lifecycle_history(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


def _classification_claims_propose_identity(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "EG-TYPED-PACKS-R652"
    )


_CLASSIFICATION_CLAIMS_ACTIONS: dict[str, Callable[[_ClassificationClaimsCtx], str]] = {
    "record": _classification_claims_record,
    "query": _classification_claims_query,
    "categories": _classification_claims_categories,
    "history": _classification_claims_history,
    "resolve_evidence": _classification_claims_resolve_evidence,
    "review": _classification_claims_review,
    "promote": _classification_claims_promote,
    "reject": _classification_claims_reject,
    "supersede": _classification_claims_supersede,
    "lifecycle_history": _classification_claims_lifecycle_history,
    "propose_identity": _classification_claims_propose_identity,
}


def _repo_provenance_snapshot(
    repo_id: str, commit_sha: str, ref: str
) -> tuple[Any, str, str | None]:
    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "EG-TYPED-PACKS-R651"
    )


def _repo_provenance_branch(
    repo_id: str, name: str, commit_sha: str
) -> tuple[Any, str, str | None]:
    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "EG-TYPED-PACKS-R651"
    )


def _repo_provenance_tag(
    repo_id: str, name: str, commit_sha: str, annotation: str
) -> tuple[Any, str, str | None]:
    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "EG-TYPED-PACKS-R651"
    )


def _repo_provenance_change_event(
    repo_id: str, commit_sha: str, kind: str, occurred_at: str, subject_id: str
) -> tuple[Any, str, str | None]:

    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "EG-TYPED-PACKS-R651"
    )


def _repo_provenance_ingest(engine: Any, obj: Any, node_id: str, repo_id: str) -> str:
    raise not_served("knowledge_graph.ingestion.envelope_ingest", "EG-REPO-INGEST-R002")


def _ontology_derive_list() -> str:
    raise not_served(
        "knowledge_graph.ontology.derived_properties", "EG-TYPED-PACKS-R650"
    )


def _ontology_derive_compute(object_json: str, name: str, object_type: str) -> str:
    ont = engine_routing._ontology_system()
    obj = json.loads(object_json) if object_json else {}
    res = ont.derive(obj, name, object_type=object_type or None)
    return json.dumps(res.model_dump(), default=str)


def _ontology_derive_compute_all(object_json: str, object_type: str) -> str:
    ont = engine_routing._ontology_system()
    obj = json.loads(object_json) if object_json else {}
    return json.dumps(ont.derive_all(obj, object_type=object_type or None), default=str)


@dataclass
class _ObjectEditsRecordArgs:
    """Bundled `object_edits` action='record' parameters (kept under the
    7-parameter cap as a dataclass rather than positional args)."""

    edit_type: str
    object_id: str
    properties_json: str
    link_target: str
    link_label: str
    actor: str
    expect: dict


def _object_edits_compare_and_set() -> str:
    # CONCEPT:AU-KG.ontology.optimistic-concurrency-object-property — atomic
    # optimistic-concurrency property set. The object id IS the node id (the
    # ledger persists the edit's target as MERGE (t {id: object_id})), so we
    # condition on the SAME node the edit targets. Apply the set ONLY if the
    # node still matches ``expect`` (missing field ≡ null), under the engine
    # write lock. If we lose the race we record NOTHING and surface
    # applied=false — never a misleading audit edit.
    raise not_served("knowledge_graph.ontology.edits", "EG-TYPED-PACKS-R651")


def _object_edits_record(ledger: Any, args: _ObjectEditsRecordArgs) -> str:
    raise not_served("knowledge_graph.ontology.edits", "EG-TYPED-PACKS-R651")


def _object_edits_revert(ledger: Any, edit_id: str, actor: str) -> str:
    raise not_served("knowledge_graph.ontology.edits", "EG-TYPED-PACKS-R651")


def _object_edits_history(ledger: Any, object_id: str) -> str:
    return json.dumps(
        {
            "object_id": object_id,
            "history": [e.model_dump() for e in ledger.history(object_id)],
        },
        default=str,
    )


def _object_edits_as_of(ledger: Any, object_id: str, ts: float) -> str:
    return json.dumps(
        {"object_id": object_id, "snapshot": ledger.as_of(object_id, ts)}, default=str
    )


def _graph_share_hierarchy(action: str, tenant_id: str, parent_tenant_id: str) -> str:

    raise not_served("knowledge_graph.core.tenant_registry", "EG-IDENTITY-R610")


def _graph_share_org(node_id: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "EG-IDENTITY-R610")


def _graph_share_commons(node_id: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "EG-IDENTITY-R610")


def _graph_share_mark(node_id: str, marking: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "EG-IDENTITY-R610")


def _graph_share_private(node_id: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "EG-IDENTITY-R610")


def _object_set_path(source_id: str, target_id: str) -> str:
    raise not_served("knowledge_graph.ontology.object_path", "EG-TYPED-PACKS-R650")


def _object_set_base(
    ont: Any, action: str, type_or_interface: str, ids_json: str
) -> Any:
    if action == "from_ids" or action in ("union", "intersect", "subtract"):
        return ont.object_set(json.loads(ids_json) if ids_json else [])
    return ont.object_set_of_type(type_or_interface)


def _object_set_effective_limit(limit: int) -> int:
    # BUG-1: clamp to a safe, non-zero, hard-capped limit for the actions
    # that used to materialize an UNBOUNDED id list (of_type/from_ids/union/
    # intersect/subtract) — a DYNAMIC ``ObjectSet`` (of_type) scans every
    # node in the graph and would otherwise return every match with no cap,
    # which OOM-crashed the live pod. ``search`` already had its own bound;
    # leave it as-is.
    return max(1, min(int(limit) if limit else 50, _OBJECT_SET_HARD_CAP))


def _object_set_ids_result(base: Any, effective_limit: int) -> str:
    ids = base.ids(limit=effective_limit)
    return json.dumps(
        {"ids": ids, "count": len(ids), "limited": len(ids) >= effective_limit}
    )


def _object_set_search(base: Any, query: str, limit: int) -> str:
    res = base.search(query, limit=limit)
    return json.dumps({"ids": res.ids(), "count": res.count()})


def _object_set_search_around(
    base: Any, link_type: str, hops: int, direction: str
) -> str:
    res = base.search_around(link_type or None, hops=hops, direction=direction)
    return json.dumps({"ids": res.ids(), "count": res.count()})


def _object_set_pivot(base: Any, link_type: str, group_by: str, direction: str) -> str:
    piv = base.pivot(link_type or None, group_by, direction=direction)
    return json.dumps(
        {"link_type": piv.link_type, "group_by": piv.group_by, "groups": piv.groups},
        default=str,
    )


def _object_set_aggregate(base: Any, metric: str, field: str, group_by: str) -> str:
    agg = base.aggregate(metric, field=field or None, group_by=group_by or None)
    return json.dumps(
        {
            "metric": agg.metric,
            "field": agg.field,
            "group_by": agg.group_by,
            "groups": {str(k): v for k, v in agg.groups.items()},
            "total_objects": agg.total_objects,
        },
        default=str,
    )


def _object_set_algebra(
    ont: Any, base: Any, action: str, type_or_interface: str, effective_limit: int
) -> str:
    other = (
        ont.object_set_of_type(type_or_interface)
        if type_or_interface
        else ont.object_set([])
    )
    # BUG-1: bound both operands AND the combined result — ``other`` can
    # itself be an unbounded of_type() DYNAMIC set.
    combined = getattr(base, action)(other, limit=effective_limit)
    ids = combined.ids(limit=effective_limit)
    return json.dumps(
        {"ids": ids, "count": len(ids), "limited": len(ids) >= effective_limit}
    )


def register_ontology_tools(mcp):
    """Register the ontology_tools group on the given FastMCP server."""

    @mcp.tool(
        name="ontology_property_types",
        description="List the ontology property-type registry and resolve/validate a Palantir-style type ref (CONCEPT:AU-KG.ontology.ontology-property-types).",
        tags=["graph-os", "ontology"],
    )
    def ontology_property_types(
        action: str = Field(
            default="list",
            description="'list' all type names, 'describe' a type, 'column_type' a type's column DDL string, or 'validate' a value.",
        ),
        type_ref: str = Field(
            default="", description="A type ref, e.g. 'array<string>' or 'vector<768>'."
        ),
        value: str = Field(
            default="", description="JSON-encoded value for action='validate'."
        ),
    ) -> str:
        """List/describe ontology property types and resolve/validate a type reference."""
        raise not_served(
            "knowledge_graph.ontology.property_types", "EG-TYPED-PACKS-R650"
        )

    runtime.REGISTERED_TOOLS["ontology_property_types"] = ontology_property_types

    @mcp.tool(
        name="ontology_value_types",
        description="List/describe constrained ontology value types and validate or coerce a value (CONCEPT:AU-KG.ontology.value-type-shacl-load).",
        tags=["graph-os", "ontology"],
    )
    def ontology_value_types(
        action: str = Field(
            default="list", description="'list' | 'describe' | 'validate' | 'coerce'."
        ),
        name: str = Field(
            default="", description="The value-type name, e.g. 'EmailAddress'."
        ),
        value: str = Field(
            default="", description="JSON-encoded value for validate/coerce."
        ),
    ) -> str:
        """List/describe value types and validate or coerce a value through one."""
        raise not_served("knowledge_graph.ontology.value_types", "EG-TYPED-PACKS-R650")

    runtime.REGISTERED_TOOLS["ontology_value_types"] = ontology_value_types

    @mcp.tool(
        name="ontology_interface",
        description=(
            "Ontology interfaces: resolve implementers (targeting), check conformance, or emit "
            "OWL (CONCEPT:AU-KG.ontology.conformance-check). Set registry='enterprise' to "
            "operate on the enterprise-standard contracts "
            "(CONCEPT:AU-KG.ontology.populated-at-import-real-3). "
            "'explain_routing_eligibility' (CONCEPT:AU-P1-3, X-4) answers WHY a "
            "candidate tool/agent (entity_id) is — or would be — eligible for a required "
            "ontology capability type: ontology SUBSUMPTION path, tenant/policy match, and the "
            "calibrated bandit reward, computed engine-native-first over "
            "`graph.routing.enrichers.capability_routing.explain_routing_eligibility`. "
            "'graph' (CONCEPT:AU-KG.ontology.schema-graph-visualization) renders the interface + "
            "link-type registries as a Cytoscape-style node/edge schema diagram; 'summary' "
            "renders the same schema as a Markdown document; 'lint' "
            "(CONCEPT:AU-KG.ontology.style-lint) flags interface/property naming-convention "
            "violations and common typos."
        ),
        tags=["graph-os", "ontology"],
    )
    def ontology_interface(
        action: str = Field(
            default="list",
            description=(
                "'list' interfaces, 'implementers' (resolve an interface/type to concrete "
                "types), 'conforms' (check an object), 'owl', 'graph' (schema as a node/edge "
                "diagram), 'summary' (schema as Markdown), 'lint' (naming-style + typo check), "
                "or 'explain_routing_eligibility' (X-4 WHY-eligible routing explanation)."
            ),
        ),
        name: str = Field(default="", description="Interface or concrete type name."),
        object_json: str = Field(
            default="{}", description="JSON object dict for action='conforms'."
        ),
        registry: str = Field(
            default="structural",
            description="Which interface registry: 'structural' (built-in shapes) or 'enterprise' (enterprise-standard contracts, CONCEPT:AU-KG.ontology.populated-at-import-real-3).",
        ),
        entity_id: str = Field(
            default="",
            description="Candidate tool/agent node id (action='explain_routing_eligibility').",
        ),
        tenant: str = Field(
            default="",
            description="Required tenant scope, if any (action='explain_routing_eligibility').",
        ),
    ) -> str:
        """Resolve interface targeting, check conformance, emit interface OWL/SHACL, or explain
        WHY a candidate is routing-eligible (X-4)."""
        raise not_served("knowledge_graph.ontology.interfaces", "EG-TYPED-PACKS-R650")

    runtime.REGISTERED_TOOLS["ontology_interface"] = ontology_interface

    @mcp.tool(
        name="ontology_leanix_sync",
        description="Discover the live LeanIX metamodel and mirror it natively as OWL/RDF: regenerates ontology_leanix.ttl (every fact sheet type, relation, field) and registers the types for OWL promotion (CONCEPT:AU-KG.ingest.enterprise-source-extractor). dry_run=true previews without writing.",
        tags=["graph-os", "ontology"],
    )
    def ontology_leanix_sync(
        dry_run: bool = Field(
            default=True,
            description="Preview the generated ontology without writing (default). Set false to apply.",
        ),
    ) -> str:
        """Compile the live LeanIX data model into OWL and apply it to both reasoning layers."""
        raise not_served(
            "knowledge_graph.ontology.leanix_metamodel",
            "SDK",
            owner="agent-connector-sdk",
        )

    runtime.REGISTERED_TOOLS["ontology_leanix_sync"] = ontology_leanix_sync

    @mcp.tool(
        name="graph_ontology",
        description=(
            "GraphSchema lifecycle control plane. load/update attach an explicit "
            "iri+version Turtle document through EG; list/get return generated "
            "GraphSchema metadata; delete detaches the exact source. EG alone "
            "validates, composes, persists, reasons over, and reports schema state."
        ),
        tags=["graph-os", "ontology", "lifecycle"],
    )
    def graph_ontology(
        action: str = Field(
            default="list",
            description=("load | list | get | update | delete"),
        ),
        source: str = Field(
            default="",
            description="For load/update: a UTF-8 Turtle file path or raw Turtle text.",
        ),
        source_type: str = Field(
            default="auto",
            description="How to read `source`: 'file' | 'text' | 'auto' (sniff).",
        ),
        iri: str = Field(
            default="",
            description="Required stable ontology IRI for load/get/update/delete.",
        ),
        version: str = Field(
            default="",
            description="Required stable version for load/get/update/delete.",
        ),
    ) -> str:
        """Attach, inspect, replace, or detach EG GraphSchema sources."""
        raise not_served("knowledge_graph.ontology.lifecycle", "EG-TYPED-PACKS-R651")

    runtime.REGISTERED_TOOLS["graph_ontology"] = graph_ontology

    @mcp.tool(
        name="graph_writeback",
        description="Backfeed KG-derived knowledge into an external system-of-record (CONCEPT:EG-KG.storage.nonblocking-checkpoint/2.9). target='leanix'|'servicenow'|'erpnext'|'process'|'capability'. ops: inferences_json [{source,rel_type,target}] (relationships), enrichments_json [{node,patches,tag}], creations_json [{type,name}] (inventory CIs/items/fact sheets), retirements_json [{node}]. Fail-closed: live writes need the target's enable flag (e.g. LEANIX_ENABLE_WRITE / SERVICENOW_ENABLE_WRITE / ERPNEXT_ENABLE_WRITE / KG_PROCESS_WRITEBACK); dry_run=true (default) previews the exact proposed writes.",
        tags=["graph-os", "writeback"],
    )
    def graph_writeback(
        target: str = Field(
            default="leanix",
            description="Write-back target: leanix | servicenow | erpnext | process | capability | (any registered sink).",
        ),
        action: str = Field(
            default="write",
            description="'write' (default), 'proposals' (list queued high-stakes proposals), or 'approve' (apply proposal_id).",
        ),
        proposal_id: str = Field(
            default="",
            description="For action='approve': the queued high-stakes proposal id to apply.",
        ),
        inferences_json: str = Field(
            default="[]",
            description="JSON list of inferred edges [{source,rel_type,target}] to write as upstream relations.",
        ),
        enrichments_json: str = Field(
            default="[]",
            description="JSON list of enrichments [{node, patches, tag}] onto existing records.",
        ),
        creations_json: str = Field(
            default="[]",
            description="JSON list of new records [{type,name,...}] to create upstream (inventory CIs/items).",
        ),
        retirements_json: str = Field(
            default="[]",
            description="JSON list [{node}] to retire/decommission upstream (highest risk).",
        ),
        process_ids_json: str = Field(
            default="[]",
            description="For target=process: JSON list of process ids to narrow to.",
        ),
        inventory: bool = Field(
            default=False,
            description="If true, collect the KG's reconciled inventory (infra/topology + LeanIX + TRM, deduped via ALIGNED_WITH) and create the items missing from the target CMDB/ERP.",
        ),
        asset_mirror: bool = Field(
            default=False,
            description="If true, fan ONE inventory pass out to EVERY enabled CMDB sink (ASSET_MIRROR_TARGETS ⊆ servicenow/erpnext/egeria/twenty) — the multi-SoR asset mirror. Each sink stays fail-closed (<SINK>_ENABLE_WRITE) + dry-run-first. Ignores `target`.",
        ),
        findings: bool = Field(
            default=False,
            description="If true, file the KG's risk findings (TRM TechnologyRisk: EOL/vuln) as issues in the target tracker (gitlab/github/plane). Pass project context via creations_json[0] or the route.",
        ),
        dry_run: bool = Field(
            default=True,
            description="Preview proposed writes without mutating the system-of-record (default). Set false to apply.",
        ),
    ) -> str:
        """Unified fail-closed write-back to any target system (dry-run-first)."""
        try:
            try:
                engine = runtime._get_engine()
            except Exception:  # noqa: BLE001 - offline → no backend resolver
                engine = None
            backend = getattr(engine, "backend", None) if engine is not None else None
            ctx = _WritebackCtx(
                target=str(target),
                backend=backend,
                engine=engine,
                dry_run=bool(dry_run),
            )
            if str(action) == "proposals":
                return _graph_writeback_proposals()
            if str(action) == "approve":
                return _graph_writeback_approve(proposal_id, ctx)
            if bool(asset_mirror):
                return _graph_writeback_asset_mirror(ctx)
            if bool(inventory):
                return _graph_writeback_inventory(ctx)
            if bool(findings):
                return _graph_writeback_findings(ctx, creations_json)
            return _graph_writeback_default(
                ctx,
                inferences_json,
                enrichments_json,
                creations_json,
                retirements_json,
                process_ids_json,
            )
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_writeback"] = graph_writeback

    @mcp.tool(
        name="spec_ticket",
        description="Link a KG SDD spec/feature to a Plane/Jira work item and make agents assignable (CONCEPT:AU-KG.ingest.enterprise-source-extractor). action='link' (push spec content onto the item, link it, assign to user/agent/act-as-user, comment) or 'pull' (read items assigned to a user — 'what do I own?'). Fail-closed (PLANE_ENABLE_WRITE/JIRA_ENABLE_WRITE), dry_run=true previews.",
        tags=["graph-os", "writeback", "sdd"],
    )
    def spec_ticket(
        action: str = Field(default="link", description="'link' or 'pull'."),
        target: str = Field(default="plane", description="'plane' or 'jira'."),
        spec_json: str = Field(
            default="{}",
            description="For action='link': the spec dict {feature_id,title,user_stories,...}.",
        ),
        issue_id: str = Field(
            default="", description="The Plane work-item id / Jira issue key."
        ),
        project_id: str = Field(
            default="", description="Plane project id (for plane)."
        ),
        assignee: str = Field(default="", description="Explicit user id to assign."),
        agent: str = Field(
            default="", description="Agent id to assign (maps via AGENT_USER_MAP)."
        ),
        comment: str = Field(default="", description="Optional comment to post."),
        user: str = Field(
            default="", description="For action='pull': user whose items to read."
        ),
        dry_run: bool = Field(
            default=True, description="Preview without writing (default)."
        ),
    ) -> str:
        """Spec↔ticket↔agent linking + assignment + assigned-items read."""
        try:
            try:
                engine = runtime._get_engine()
            except Exception:  # noqa: BLE001
                engine = None
            backend = getattr(engine, "backend", None) if engine is not None else None
            if str(action) == "pull":
                return _spec_ticket_pull(target, user, project_id)
            ctx = _SpecTicketCtx(
                target=str(target), backend=backend, dry_run=bool(dry_run)
            )
            return _spec_ticket_link(
                ctx, spec_json, issue_id, project_id, assignee, agent, comment
            )
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["spec_ticket"] = spec_ticket

    @mcp.tool(
        name="source_sync",
        description="THE canonical connector→KG ingestion tool (CONCEPT:AU-KG.ingest.enterprise-source-extractor) — one entrypoint for every external source. source='leanix'|'camunda'|'servicenow'|'gitlab'|… (any registered hydration/materialize source), OR source='all' to sweep EVERY configured connector in one pass (the fleet-wide background-ingest sweep). mode='delta' (only changes since the watermark, default), 'full' (re-mirror all), or 'reconcile' (tombstone records deleted upstream). Delta-capable sources do incremental sync; all others fall back to a full hydrate, and a generic write-layer content-hash delta means unchanged entities are skipped (no re-write, no re-reason) for ALL sources even on a full fetch. ids=[...] narrows to specific records (webhook-driven). graph_ingest covers path/URL/document content.",
        tags=["graph-os", "ingestion"],
    )
    def source_sync(
        source: str = Field(
            default="leanix",
            description="Registered source to sync (e.g. 'leanix', 'camunda', 'servicenow').",
        ),
        mode: str = Field(
            default="delta",
            description="'delta' (watermark poll), 'full' (re-mirror all), or 'reconcile' (tombstone deletions).",
        ),
        ids_json: str = Field(
            default="[]",
            description="JSON list of record ids to narrow the sync (webhook delta).",
        ),
        connection: str = Field(
            default="",
            description=(
                "CONCEPT:AU-KG.backend.multi-connection-registry — named BACKEND connection to sync into "
                "(default = primary). Selects WHICH BACKEND, never which physical "
                "graph — see `graph`. Fan-out ('all'/a list) is rejected: a source "
                "sync always targets exactly one backend, never a union — distinct "
                "from `source='all'`, which fans out across CONNECTORS, not "
                "backends/graphs."
            ),
        ),
        graph: str = Field(
            default="",
            description=(
                "CONCEPT:AU-KG.backend.explicit-graph-selection — explicit physical engine graph to sync THIS "
                "source into (one of the names `engine_tenants(action='list')` "
                "returns), independent of `connection`. U-37: gives each source "
                "its OWN isolated physical graph and watermark/checkpoint "
                "(the native ChangeEnvelope cursor is scoped to whatever graph "
                "this call narrows onto), so unbounded multi-source imports "
                "never share one bulk watermark across sources with different "
                "trust/change-detection boundaries. Empty = the caller's own "
                "bound graph (unchanged default behavior). An unknown graph, or "
                "a `connection` with no physical-graph concept, is a typed "
                "error — never a silent fallback or fan-out."
            ),
        ),
    ) -> str:
        """Run a delta/full/reconcile sync for any registered source against the live engine."""
        # A direct call bypassing FastMCP's own Field-default resolution binds
        # an omitted `graph`/`connection` to a raw `FieldInfo` rather than
        # `""` — normalize once here, mirroring `query_tools._run_graph_query`'s
        # identical guard.
        graph = graph if isinstance(graph, str) else ""
        connection = connection if isinstance(connection, str) else ""

        try:
            ids = json.loads(ids_json) if ids_json else []

            # U-37/GOC-67: resolve `connection`/`graph` BEFORE touching the
            # engine or reading any watermark — exactly like `graph_query`/
            # `graph_write`/`graph_ingest`.
            engine, resolve_error = _source_sync_resolve_engine(connection, graph)
            if resolve_error is not None:
                return resolve_error
            if engine is None:
                return json.dumps(
                    {"status": "error", "error": "active engine required"}
                )
            result = _source_sync_run(engine, source, mode, ids, graph, connection)
            return json.dumps(result)
        except PermissionError as e:
            return public_error_json(
                e, code="permission_denied" if graph else "operation_failed"
            )
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["source_sync"] = source_sync

    @mcp.tool(
        name="source_drain",
        description="Watch a chunked async drain started by source_sync(mode='full') on a LARGE corpus (CONCEPT:AU-KG.ontology.single-source-full-drain). action='status' + drain_id returns cumulative progress (pages_done / items_seen / items_ingested) plus a live per-status breakdown of the drain's connector_drain WorkItems. action='list' lists registered chunked-drain sources. A single source_sync(source=X, mode='full') returns a drain_id immediately and drains the whole backlog across capacity-guarded background page WorkItems; poll this to watch it finish.",
        tags=["graph-os", "ingestion"],
    )
    def source_drain(
        action: str = Field(
            default="status",
            description="'status' (progress for a drain_id) or 'list' (registered chunked sources).",
        ),
    ) -> str:
        """Report progress of a chunked drain or list chunked-drain-capable sources."""
        raise not_served("knowledge_graph.core.chunked_drain", "EG-INGEST-WORKER-R605")

    runtime.REGISTERED_TOOLS["source_drain"] = source_drain

    @mcp.tool(
        name="graph_etl",
        description=(
            "Unified ETL pipeline between systems over the canonical KG hub "
            "(CONCEPT:AU-KG.ontology.one-source). One source→(ontological transform)→sink interface that "
            "composes the existing ingestion, write-back, and graph-store machinery. "
            "action='run' (default): pull `source` into the KG (any registered "
            "ingestion source: leanix/servicenow/egeria/camunda/aris/gitlab/…; mode "
            "delta|full|reconcile) and/or load `sink` from the KG — `sink` is either a "
            "write-back system-of-record (leanix/servicenow/egeria/… → dry_run-first + "
            "approval gate; pass ops_json with inferences/enrichments/creations/"
            "retirements) OR a graph store (stardog/neo4j/age/jena_fuseki or a "
            "registered connection name → full data load; SPARQL stores partition into "
            "urn:source:<system> named graphs; sources_json filters the subset) OR the "
            "native engine SQL-table sink (sink='table' → mirror the inbound `source` "
            "connector's data into an engine SQL table via CREATE TABLE + bulk INSERT; "
            "pass ops_json {table,config,limit,replace} — CONCEPT:AU-KG.ingest.mirror-inbound). Omit "
            "either side for a one-directional run. action='list': available sources, "
            "write-back sinks, and graph-store backends. action='lineage': recorded "
            "ETL runs (impact analysis), filterable by source/sink (CONCEPT:AU-KG.ontology.kg-3)."
        ),
        tags=["graph-os", "ingestion", "etl"],
    )
    def graph_etl(
        action: str = Field(default="run", description="'run' | 'list' | 'lineage'."),
        source: str = Field(
            default="", description="Ingestion source to pull into the KG (inbound)."
        ),
        sink: str = Field(
            default="",
            description="Write-back domain or graph-store/connection name (outbound).",
        ),
        mode: str = Field(
            default="delta", description="Inbound sync mode: delta|full|reconcile."
        ),
        sources_json: str = Field(
            default="[]",
            description="JSON list of source systems to filter a graph-store push.",
        ),
        ids_json: str = Field(
            default="[]",
            description="JSON list of record ids to narrow the inbound sync.",
        ),
        ops_json: str = Field(
            default="{}",
            description="JSON write-back payload (inferences/enrichments/creations/…).",
        ),
        dry_run: bool = Field(
            default=True, description="Write-back dry-run (fail-closed default)."
        ),
        limit: int = Field(default=200, description="Max rows for action='lineage'."),
    ) -> str:
        """Run / inspect a unified ETL flow over the canonical KG hub."""
        try:
            engine = runtime._get_engine()
        except Exception:  # noqa: BLE001
            engine = None

        try:
            if action == "list":
                return _graph_etl_list()

            if engine is None:
                return json.dumps(
                    {"status": "error", "error": "active engine required"}
                )

            if action == "lineage":
                return _graph_etl_lineage(engine, source, sink, limit)

            # action == "run"
            args = _EtlRunArgs(
                engine=engine,
                source=source,
                sink=sink,
                mode=mode,
                sources_json=sources_json,
                ids_json=ids_json,
                ops_json=ops_json,
                dry_run=dry_run,
            )
            return _graph_etl_run(args)
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_etl"] = graph_etl

    @mcp.tool(
        name="ontology_classification_claims",
        description=(
            "Typed, provenance-bearing multi-category classification claims (U-47, "
            "CONCEPT:AU-KG.ontology.classification-claim-multi-category / "
            "classification-claim-promotion-lifecycle / cross-source-identity-proposal). "
            "One subject can carry several simultaneous, independently true-or-false "
            "classifications (e.g. 'this file is code AND security-critical'), each with "
            "its own method (declared/observed/derived/generated) and promotion state "
            "(candidate -> reviewed -> promoted/rejected/superseded). "
            "action='record' (build + persist one claim from `claim_json`, a raw record "
            "with subject_id/category/value/evidence_refs/method/source_snapshot — "
            "idempotent on the claim's own content-addressed id), "
            "'query' (list claims for `subject_id`, optionally filtered by `status`/"
            "`category`), 'categories' (the set of currently-promoted categories for "
            "`subject_id`), 'history' (every claim ever made for one `subject_id` + "
            "`category` pair, across every snapshot), 'resolve_evidence' (a claim's "
            "evidence Fragments, clearance-redacted per `viewer_clearance`), "
            "'review'/'promote'/'reject' (governed promotion-lifecycle transitions on "
            "the claim in `claim_json`, via ClassificationPromotionLedger), 'supersede' "
            "(retire the claim in `claim_json` in favor of `new_claim_json`, both "
            "retained), 'lifecycle_history' (a `claim_id`'s full transition history), "
            "'propose_identity' (a cross-source-identity claim between `artifact_a_id` "
            "and `artifact_b_id`, recorded via the same evidence-required machinery)."
        ),
        tags=["graph-os", "ontology", "classification"],
    )
    def ontology_classification_claims(
        action: str = Field(
            default="query",
            description=(
                "record | query | categories | history | resolve_evidence | review | "
                "promote | reject | supersede | lifecycle_history | propose_identity."
            ),
        ),
        subject_id: str = Field(
            default="", description="For query/categories/history."
        ),
        category: str = Field(default="", description="For query/history."),
        status: str = Field(default="", description="For query: filter by status."),
        claim_json: str = Field(
            default="",
            description=(
                "For record: a raw claim record (subject_id/category/value/"
                "evidence_refs/method/source_snapshot/...) as JSON. For review/"
                "promote/reject/supersede/resolve_evidence: the FULL claim object "
                "(as returned by a prior record/query call) as JSON."
            ),
        ),
        new_claim_json: str = Field(
            default="",
            description="For supersede: the successor claim, same shape as claim_json.",
        ),
        source_snapshot: str = Field(
            default="", description="For record/propose_identity."
        ),
        policy_approved: bool = Field(
            default=False,
            description="For record: pre-cleared a candidate/generated claim through action_policy already.",
        ),
        reviewer: str = Field(default="", description="For promote/reject."),
        reason: str = Field(
            default="", description="For review/promote/reject/supersede."
        ),
        viewer_clearance: str = Field(
            default="internal", description="For resolve_evidence."
        ),
        claim_id: str = Field(default="", description="For lifecycle_history."),
        artifact_a_id: str = Field(default="", description="For propose_identity."),
        artifact_b_id: str = Field(default="", description="For propose_identity."),
        evidence_refs_json: str = Field(
            default="[]",
            description="For propose_identity: JSON array of Fragment ids.",
        ),
        extractor_ref: str = Field(
            default="", description="For record (via claim_json)/propose_identity."
        ),
        confidence: float = Field(
            default=-1.0,
            description="For propose_identity: 0.0-1.0, or omit (-1.0 sentinel) for an explicit abstain.",
        ),
        tenant: str = Field(default="", description="For record/propose_identity."),
    ) -> str:
        """Record / query / promote classification claims through the governed lifecycle."""
        try:
            engine = runtime._get_engine()
        except Exception:  # noqa: BLE001
            engine = None
        if engine is None:
            return json.dumps({"status": "error", "error": "active engine required"})

        try:
            handler = _CLASSIFICATION_CLAIMS_ACTIONS.get(action)
            if handler is None:
                return json.dumps(
                    {"status": "error", "error": f"unknown action {action!r}"}
                )
            ctx = _ClassificationClaimsCtx(
                engine=engine,
                subject_id=subject_id,
                category=category,
                status=status,
                claim_json=claim_json,
                new_claim_json=new_claim_json,
                source_snapshot=source_snapshot,
                policy_approved=policy_approved,
                reviewer=reviewer,
                reason=reason,
                viewer_clearance=viewer_clearance,
                claim_id=claim_id,
                artifact_a_id=artifact_a_id,
                artifact_b_id=artifact_b_id,
                evidence_refs_json=evidence_refs_json,
                extractor_ref=extractor_ref,
                confidence=confidence,
                tenant=tenant,
            )
            return handler(ctx)
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["ontology_classification_claims"] = (
        ontology_classification_claims
    )

    @mcp.tool(
        name="ontology_repository_provenance",
        description=(
            "Git identity and history anchors (U-47, CONCEPT:AU-KG.ontology."
            "repository-provenance-snapshot), additive to the existing :Commit/"
            ":Author/:File model git_history already ingests. Each action writes ONE "
            "node type onto the SAME 'commit:<sha>' id convention through the shared "
            "envelope-ingest write path, so a ClassificationClaim's `source_snapshot` "
            "has a real, queryable node to point at instead of an opaque string. "
            "action='snapshot' (RepositorySnapshot — the state of `repo_id` at "
            "`commit_sha`, optionally naming the `ref` it was captured at), "
            "'branch' (Branch — a named, moving pointer: `name` -> `commit_sha`), "
            "'tag' (Tag — a named, fixed pointer: `name` -> `commit_sha`, optional "
            "`annotation`), 'change_event' (ChangeEvent — one repo-scoped historical "
            "event of `kind` at `commit_sha`, optionally naming the `subject_id` — "
            "file/entity — it concerns). All four are read-only projections of git "
            "metadata already present in the repository; none modifies source truth."
        ),
        tags=["graph-os", "ontology", "provenance"],
    )
    def ontology_repository_provenance(
        action: str = Field(
            default="snapshot", description="snapshot | branch | tag | change_event."
        ),
        repo_id: str = Field(default="", description="Required for every action."),
        commit_sha: str = Field(
            default="", description="Required for snapshot/branch/tag/change_event."
        ),
        ref: str = Field(
            default="", description="For snapshot: the ref name captured."
        ),
        name: str = Field(
            default="", description="For branch/tag: the pointer's name."
        ),
        annotation: str = Field(
            default="", description="For tag: optional annotation."
        ),
        kind: str = Field(
            default="commit",
            description="For change_event: commit | branch_move | tag_cut | ...",
        ),
        occurred_at: str = Field(
            default="",
            description="For change_event: ISO-8601 timestamp; defaults to now.",
        ),
        subject_id: str = Field(
            default="",
            description="For change_event: the file/entity this event concerns.",
        ),
    ) -> str:
        """Record repository provenance (RepositorySnapshot/Branch/Tag/ChangeEvent) into the KG."""
        try:
            engine = runtime._get_engine()
        except Exception:  # noqa: BLE001
            engine = None
        if engine is None:
            return json.dumps({"status": "error", "error": "active engine required"})
        if not repo_id:
            return json.dumps({"status": "error", "error": "repo_id is required"})

        try:
            if action == "snapshot":
                obj, node_id, err = _repo_provenance_snapshot(repo_id, commit_sha, ref)
            elif action == "branch":
                obj, node_id, err = _repo_provenance_branch(repo_id, name, commit_sha)
            elif action == "tag":
                obj, node_id, err = _repo_provenance_tag(
                    repo_id, name, commit_sha, annotation
                )
            elif action == "change_event":
                obj, node_id, err = _repo_provenance_change_event(
                    repo_id, commit_sha, kind, occurred_at, subject_id
                )
            else:
                return json.dumps(
                    {"status": "error", "error": f"unknown action {action!r}"}
                )
            if err is not None:
                return err
            return _repo_provenance_ingest(engine, obj, node_id, repo_id)
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["ontology_repository_provenance"] = (
        ontology_repository_provenance
    )

    @mcp.tool(
        name="ontology_function",
        description="Typed, versioned ontology functions: list or invoke through the governed runtime (CONCEPT:AU-KG.ontology.default-runtime-bound-import).",
        tags=["graph-os", "ontology"],
    )
    def ontology_function(
        action: str = Field(
            default="list", description="'list' registered functions or 'invoke' one."
        ),
        name: str = Field(default="", description="Function name for action='invoke'."),
        params: str = Field(
            default="{}", description="JSON-encoded typed input params."
        ),
        version: str = Field(default="", description="Optional pinned semver version."),
        actor: str = Field(
            default="mcp:caller",
            description="Invoking actor id (recorded in the audit entry).",
        ),
    ) -> str:
        """List registered ontology functions or invoke one with typed params."""
        raise not_served("knowledge_graph.ontology.functions", "EG-TYPED-PACKS-R650")

    runtime.REGISTERED_TOOLS["ontology_function"] = ontology_function

    @mcp.tool(
        name="ontology_derive",
        description="Compute derived (function/cypher/sparql/embedding-backed) properties live at read time (CONCEPT:AU-KG.ontology.derived-property-registry).",
        tags=["graph-os", "ontology"],
    )
    def ontology_derive(
        action: str = Field(
            default="compute",
            description="'list' declarations, 'compute' one property, or "
            "'compute_all'. (Schema-discovery/standalone-generation proposals "
            "moved behind EG pack compilation, AU-SEMANTIC-R021.2 — no longer "
            "served from this tool.)",
        ),
        object_json: str = Field(
            default="{}", description="JSON object dict the property is computed for."
        ),
        name: str = Field(
            default="", description="Derived-property name for action='compute'."
        ),
        object_type: str = Field(
            default="",
            description="Optional object type for declaration resolution.",
        ),
    ) -> str:
        """Compute derived properties (declared function/cypher/sparql/embedding-backed)."""
        try:
            if action == "list":
                return _ontology_derive_list()
            if action == "compute":
                return _ontology_derive_compute(object_json, name, object_type)
            if action == "compute_all":
                return _ontology_derive_compute_all(object_json, object_type)
            return json.dumps({"error": f"unknown action: {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["ontology_derive"] = ontology_derive

    @mcp.tool(
        name="ontology_link_materialize",
        description="Reify a many-to-many ontology link as a (junction_node, edge_a, edge_b) triple and write it (CONCEPT:AU-KG.domains.trade-journal-bias-auditor).",
        tags=["graph-os", "ontology"],
    )
    async def ontology_link_materialize(
        action: str = Field(
            default="materialize",
            description="'types' to list link types, or 'materialize' a junction.",
        ),
        source_id: str = Field(default="", description="Source endpoint node id."),
        target_id: str = Field(default="", description="Target endpoint node id."),
        properties: str = Field(
            default="{}", description="JSON-encoded junction (link) properties."
        ),
    ) -> str:
        """List link types or reify + persist a M:N link via the graph_write path."""
        raise not_served("knowledge_graph.ontology.links", "EG-TYPED-PACKS-R650")

    runtime.REGISTERED_TOOLS["ontology_link_materialize"] = ontology_link_materialize

    @mcp.tool(
        name="object_edits",
        description=(
            "Durable object-edit ledger (CONCEPT:AU-KG.ontology.edit-ledger-writeback): record a structured edit "
            "(property_set/link_add/link_remove/object_create/object_delete), revert "
            "an edit, or read per-object history / as_of snapshot. For a property_set, "
            "pass 'expect' to record the edit ONLY if the object still matches those "
            "values (CONCEPT:AU-KG.ontology.optimistic-concurrency-object-property) — optimistic concurrency for concurrent agent "
            "object-shaping so two agents editing the same object never lose a write."
        ),
        tags=["graph-os", "ontology"],
    )
    def object_edits(
        action: str = Field(
            default="history",
            description="'record' an edit | 'revert' an edit by id | 'history' per object | 'as_of' snapshot.",
        ),
        object_id: str = Field(
            default="", description="Target object id (record/history/as_of)."
        ),
        edit_type: str = Field(
            default="property_set",
            description="property_set|link_add|link_remove|object_create|object_delete (for action='record').",
        ),
        properties_json: str = Field(
            default="{}",
            description="JSON property map (record property_set/object_create).",
        ),
        link_target: str = Field(
            default="", description="Link target id (record link_add/link_remove)."
        ),
        link_label: str = Field(
            default="related", description="Link label (record link_add/link_remove)."
        ),
        edit_id: str = Field(default="", description="Edit id (action='revert')."),
        ts: float = Field(default=0.0, description="Unix timestamp (action='as_of')."),
        actor: str = Field(
            default="system", description="Acting principal recorded on the edit."
        ),
        expect: dict = Field(
            default_factory=dict,
            description=(
                "For action='record' edit_type='property_set': field→expected current "
                "value the object must still match for the set to apply (missing field "
                "reads as null). When non-empty the set goes through an atomic "
                "compare-and-set (CONCEPT:AU-KG.ontology.optimistic-concurrency-object-property): the edit is recorded ONLY if it "
                "wins; an unapplied set returns {'applied': false} and records nothing. "
                "Empty (default) = unconditional set, identical to prior behavior. "
                "e.g. {'status': 'pending'}."
            ),
        ),
    ) -> str:
        """Record / revert object edits and read per-object edit history or an as_of snapshot.

        CONCEPT:AU-KG.ontology.optimistic-concurrency-object-property optimistic-concurrency for object property edits.
        ``action='record'`` with a non-empty ``expect`` is the object-layer
        optimistic-concurrency primitive: the property_set is
        applied through the engine's atomic ``compare_and_set_node_fields`` and the
        ledger edit is recorded **only if the precondition still holds** — use it
        when concurrent agents shape the same object so one never clobbers another.
        """
        try:
            ont = engine_routing._ontology_system()
            ledger = ont.edits
            if action == "record":
                args = _ObjectEditsRecordArgs(
                    edit_type=edit_type,
                    object_id=object_id,
                    properties_json=properties_json,
                    link_target=link_target,
                    link_label=link_label,
                    actor=actor,
                    expect=expect,
                )
                return _object_edits_record(ledger, args)
            if action == "revert":
                return _object_edits_revert(ledger, edit_id, actor)
            if action == "history":
                return _object_edits_history(ledger, object_id)
            if action == "as_of":
                return _object_edits_as_of(ledger, object_id, ts)
            return json.dumps({"error": f"unknown action: {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["object_edits"] = object_edits

    @mcp.tool(
        name="object_index",
        description="Object Index Lifecycle / Object Data Funnel (CONCEPT:AU-KG.ontology.batch-incremental-sync-live): batch/incremental sync of the live search index from source nodes, report staleness, or reindex stale objects.",
        tags=["graph-os", "ontology"],
    )
    def object_index(
        action: str = Field(
            default="status",
            description="'sync' (batch rebuild) | 'reindex' (reconcile stale) | 'status' (live/tombstone counts).",
        ),
        nodes_json: str = Field(
            default="[]",
            description="JSON list of source node mappings (sync/reindex).",
        ),
    ) -> str:
        """Sync / reindex the live object search index and report staleness."""
        try:
            ont = engine_routing._ontology_system()
            funnel = ont.index_funnel
            if action == "sync":
                nodes = json.loads(nodes_json) if nodes_json else []
                return json.dumps(funnel.batch_sync(nodes).as_dict())
            if action == "reindex":
                nodes = json.loads(nodes_json) if nodes_json else []
                return json.dumps(funnel.reconcile(nodes).as_dict())
            if action == "status":
                return json.dumps(
                    {
                        "live_size": len(funnel),
                        "tombstones": funnel.tombstone_count,
                        "indexed_ids": sorted(funnel.live_ids()),
                    }
                )
            return json.dumps({"error": f"unknown action: {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["object_index"] = object_index

    @mcp.tool(
        name="object_permissioning",
        description="Fine-grained object permissioning (CONCEPT:AU-KG.ontology.redact-object-materialize-restricted): redact an object, materialize a restricted view, or attach a mandatory marking. Actor is resolved from the ambient context — never from caller-supplied clearance.",
        tags=["graph-os", "ontology"],
    )
    def object_permissioning(
        action: str = Field(
            default="restricted_view",
            description="'redact' one object | 'restricted_view' an object set | 'mark' attach a marking.",
        ),
        object_json: str = Field(
            default="{}", description="JSON object dict (redact)."
        ),
        node_id: str = Field(default="", description="Node id (action='mark')."),
        marking: str = Field(default="", description="Marking name (action='mark')."),
    ) -> str:
        """Redact / restrict / mark objects for the AMBIENT actor (no spoofable clearance)."""
        raise not_served(
            "knowledge_graph.ontology.permissioning", "EG-TYPED-PACKS-R652"
        )

    runtime.REGISTERED_TOOLS["object_permissioning"] = object_permissioning

    @mcp.tool(
        name="graph_share",
        description="Share a private node (CONCEPT:AU-KG.compute.data-is-private-its). Data is private-to-its-owner by default; this is the explicit promotion path. action='org' shares with the owner's org (in-place), 'commons' promotes a copy into the shared cross-org commons graph (share by WHERE placed), 'mark' attaches a mandatory marking (share by HOW placed), 'private' restricts it back. Tenant HIERARCHY (share by WHO you inherit from): action='set_parent' durably records tenant_id's parent so `accessible_graphs()` returns a real multi-level overlay chain (most-specific tenant first, commons always last), 'clear_parent' detaches it, 'hierarchy' reads the registered chain. set_parent/clear_parent require the explicit 'kg:admin' capability. Actor/owner is the ambient identity — never caller-supplied.",
        tags=["graph-os", "tenancy"],
    )
    def graph_share(
        action: str = Field(
            default="org",
            description="'org' share with my org | 'commons' promote to the shared commons graph | 'mark' attach a marking | 'private' restrict back to me | 'set_parent'/'clear_parent'/'hierarchy' manage the durable tenant hierarchy (kg:admin for the two writes).",
        ),
        node_id: str = Field(default="", description="Id of the node to share."),
        marking: str = Field(default="", description="Marking name (action='mark')."),
        tenant_id: str = Field(
            default="",
            description="Tenant whose parent is being set/cleared/read (action='set_parent'|'clear_parent'|'hierarchy'). Defaults to the ambient actor's tenant for 'hierarchy'.",
        ),
        parent_tenant_id: str = Field(
            default="",
            description="Parent tenant to record (action='set_parent'). Refused on a cycle or past MAX_TENANT_DEPTH.",
        ),
    ) -> str:
        """Explicit, private-by-default sharing for the AMBIENT actor (KG-2.60).

        The tenant-hierarchy actions are the production write path into
        :mod:`agent_utilities.knowledge_graph.core.tenant_registry` — the durable
        ``__control__`` registry that
        :func:`~agent_utilities.knowledge_graph.core.tenant_sharing.accessible_graphs`
        reads on every request to build its precedence chain.
        """
        if action in ("set_parent", "clear_parent", "hierarchy"):
            return _graph_share_hierarchy(action, tenant_id, parent_tenant_id)

        if not node_id:
            return json.dumps({"error": "node_id is required"})
        try:
            if action == "org":
                return _graph_share_org(node_id)
            if action == "commons":
                return _graph_share_commons(node_id)
            if action == "mark":
                return _graph_share_mark(node_id, marking)
            if action == "private":
                return _graph_share_private(node_id)
            return json.dumps({"error": f"unknown action: {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_share"] = graph_share

    @mcp.tool(
        name="object_set",
        description="Object Set Service (CONCEPT:AU-KG.ontology.link-type-pivot/2.38): search/filter/search_around/pivot/aggregate and union/intersect/subtract over Foundry-style object sets. action='path' (CONCEPT:AU-KG.ontology.object-path-finder) finds the shortest path between two specific object ids and returns the hop-by-hop relationship chain — the object-agnostic generalization of the existing `graph_analyze` code-symbol path finder.",
        tags=["graph-os", "ontology"],
    )
    def object_set(
        action: str = Field(
            default="of_type",
            description="of_type|from_ids|search|filter|search_around|pivot|aggregate|union|intersect|subtract|path.",
        ),
        type_or_interface: str = Field(
            default="", description="Object type / interface (of_type)."
        ),
        ids_json: str = Field(
            default="[]",
            description="JSON list of ids (from_ids / set algebra 'other').",
        ),
        query: str = Field(default="", description="Search query (search)."),
        link_type: str = Field(
            default="", description="Link type (search_around/pivot); empty = any."
        ),
        hops: int = Field(default=1, description="Hop count (search_around)."),
        direction: str = Field(
            default="out", description="out|in|both (search_around/pivot)."
        ),
        group_by: str = Field(
            default="", description="Group-by property (pivot/aggregate)."
        ),
        metric: str = Field(
            default="count", description="count|sum|avg|min|max (aggregate)."
        ),
        field: str = Field(
            default="", description="Numeric field (aggregate sum/avg/min/max)."
        ),
        limit: int = Field(default=50, description="Result limit (search)."),
        source_id: str = Field(
            default="", description="Source object id (action='path')."
        ),
        target_id: str = Field(
            default="", description="Target object id (action='path')."
        ),
    ) -> str:
        """Compute over a Foundry-style object set: search/filter/traverse/pivot/aggregate/algebra/path."""
        try:
            if action == "path":
                return _object_set_path(source_id, target_id)
            ont = engine_routing._ontology_system()
            base = _object_set_base(ont, action, type_or_interface, ids_json)
            effective_limit = _object_set_effective_limit(limit)

            if action in ("of_type", "from_ids"):
                return _object_set_ids_result(base, effective_limit)
            if action == "search":
                return _object_set_search(base, query, limit)
            if action == "search_around":
                return _object_set_search_around(base, link_type, hops, direction)
            if action == "pivot":
                return _object_set_pivot(base, link_type, group_by, direction)
            if action == "aggregate":
                return _object_set_aggregate(base, metric, field, group_by)
            if action in ("union", "intersect", "subtract"):
                return _object_set_algebra(
                    ont, base, action, type_or_interface, effective_limit
                )
            return json.dumps({"error": f"unknown action: {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["object_set"] = object_set

    @mcp.tool(
        name="document_process",
        description="Document → ontology processing (CONCEPT:AU-KG.ingest.chunk-overlap-stage): extract → chunk(overlap) → embed → materialize a Document + linked Chunk objects through the live graph write path.",
        tags=["graph-os", "ontology"],
    )
    def document_process(
        document: str = Field(
            description="A file path or raw text content to process."
        ),
        text: str = Field(
            default="", description="Optional pre-extracted text (OCR/external)."
        ),
        source: str = Field(default="", description="Provenance label (path/URL)."),
        overlap: int = Field(
            default=120, description="Overlap characters between chunks."
        ),
        contextual: bool = Field(
            default=False,
            description="Enable contextual-retrieval enrichment (CONCEPT:AU-KG.enrichment.contextual-retrieval-enrichment): situate each chunk within the document and embed context+chunk for better recall.",
        ),
    ) -> str:
        """Process a document into Document + Chunk ontology objects through the live graph."""
        raise not_served("knowledge_graph.facade", "EG-CONTRACT-R005")

    runtime.REGISTERED_TOOLS["document_process"] = document_process

    # Quant trading system tool (CONCEPT:AU-ECO.messaging.native-backend-abstraction): debate/regime/data/execute/
    # portfolio over the finance engines. Registered onto the MCP server AND the
    # shared runtime.REGISTERED_TOOLS map so the gateway REST twin (/quant) reaches it.
    # The finance domain needs the optional `[finance]` extra (scipy/pandas/statsmodels);
    # the lean serving image omits it, so guard the registration so runtime still boots
    # — the quant tool is simply absent there. See AGENTS.md "Dependency discipline".
    try:
        raise not_served(
            "domains.finance.quant_mcp_tools",
            "GRAPHOS (served MCP tools)",
            owner="graph-os",
        )
    except ImportError:
        logger.info(
            "quant tools skipped (finance extra not installed) — "
            "install agent-utilities[finance] to enable the `quant` MCP tool"
        )

    @mcp.tool(
        name="source_connector",
        description="Document-source connectors (CONCEPT:AU-ECO.connector.document-source-framework–4.29, KG-2.59): list registered connectors, or run one (filesystem/web/rest/database/graphql_document/mcp:<package>/mcp_tool — GraphQL transports resolve endpoint, auth, TLS, schema mappings, and governance from runtime secret profiles; mcp_tool drives a fleet MCP listing tool as a paginated source) to ingest governed documents and ChangeEnvelopes into the KG with contextual enrichment (KG-2.50) and external permission sync (ECO-4.28).",
        tags=["graph-os", "ecosystem", "connectors"],
    )
    async def source_connector(
        action: str = Field(
            default="list",
            description="One of: 'list' (registered connector types), 'run' (build + ingest a connector).",
        ),
        source_type: str = Field(
            default="",
            description="Connector type for 'run' (filesystem/web/rest/database/graphql_document/mcp:<package>/mcp_tool).",
        ),
        config: dict = Field(
            default_factory=dict,
            description="Connector configuration dict for 'run' (e.g. {'root': '/docs'} or {'base_url': 'https://…'}).",
        ),
        connector_id: str = Field(
            default="",
            description="Stable id for incremental checkpoint storage (optional).",
        ),
        contextual: bool = Field(
            default=True,
            description="Enable contextual-retrieval enrichment (CONCEPT:AU-KG.enrichment.contextual-retrieval-enrichment).",
        ),
    ) -> str:
        """List or run a document-source connector (CONCEPT:AU-ECO.connector.document-source-framework–4.29)."""
        raise not_served("knowledge_graph.facade", "EG-CONTRACT-R005")

    runtime.REGISTERED_TOOLS["source_connector"] = source_connector
