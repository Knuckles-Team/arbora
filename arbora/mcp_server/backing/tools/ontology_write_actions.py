"""Write-side ontology action handlers: writeback, spec tickets, source sync, ETL, classification claims, provenance, derivation, object edits and sharing (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from arbora.api.domain.mcp_server.backing import engine_routing
from arbora.api.errors import not_served
from arbora.security.error_surface import (
    public_error_json,
)

logger = logging.getLogger(__name__)


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
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
    )


def _graph_writeback_approve(proposal_id: str, ctx: _WritebackCtx) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
    )


def _graph_writeback_asset_mirror(ctx: _WritebackCtx) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
    )


def _graph_writeback_inventory(ctx: _WritebackCtx) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
    )


def _graph_writeback_findings(ctx: _WritebackCtx, creations_json: str) -> str:
    raise not_served(
        "knowledge_graph.enrichment.writeback",
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
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
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
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
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
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
        "SCION-SOURCE-INGEST-R600",
        owner="scion-sdk",
    )


def _source_sync_resolve_engine(
    connection: str, graph: str, engine_provider: Callable[[], Any]
) -> tuple[Any, str | None]:
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
        except Exception as e:
            logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
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
        engine = engine_provider()
    except Exception:
        logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
        engine = None
    return engine, None


def _source_sync_run(
    engine: Any, source: str, mode: str, ids: list[Any], graph: str, connection: str
) -> dict[str, Any]:
    raise not_served("knowledge_graph.core.source_sync", "MEDULLA-INGEST-WORKER-R605")


def _graph_etl_list() -> str:
    raise not_served(
        "knowledge_graph.enrichment.registry", "MEDULLA-INGEST-WORKER-R606"
    )


def _graph_etl_lineage(engine: Any, source: str, sink: str, limit: int) -> str:
    raise not_served("knowledge_graph.etl", "MEDULLA-UNIFIED-DATA-PLANE-R702")


def _graph_etl_resolve_sink_backend(sink: str) -> Any:
    # Write-back sinks + the native SQL-table sink need none — run_etl routes
    # sink='table' itself (KG-2.266).
    raise not_served(
        "knowledge_graph.enrichment.writeback.core", "SDK", owner="scion-sdk"
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
    raise not_served("knowledge_graph.etl", "MEDULLA-UNIFIED-DATA-PLANE-R702")


def _classification_claim_from_json(raw: str) -> Any:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
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
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_query(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_categories(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_history(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_resolve_evidence(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_review(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_promote(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_reject(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_supersede(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_lifecycle_history(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


def _classification_claims_propose_identity(ctx: _ClassificationClaimsCtx) -> str:
    raise not_served(
        "knowledge_graph.ontology.classification_claims", "MEDULLA-TYPED-PACKS-R652"
    )


_CLASSIFICATION_CLAIMS_ACTIONS: Mapping[
    str, Callable[[_ClassificationClaimsCtx], str]
] = MappingProxyType(
    {
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
)


def _repo_provenance_snapshot(
    repo_id: str, commit_sha: str, ref: str
) -> tuple[Any, str, str | None]:
    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "MEDULLA-TYPED-PACKS-R651"
    )


def _repo_provenance_branch(
    repo_id: str, name: str, commit_sha: str
) -> tuple[Any, str, str | None]:
    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "MEDULLA-TYPED-PACKS-R651"
    )


def _repo_provenance_tag(
    repo_id: str, name: str, commit_sha: str, annotation: str
) -> tuple[Any, str, str | None]:
    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "MEDULLA-TYPED-PACKS-R651"
    )


def _repo_provenance_change_event(
    repo_id: str, commit_sha: str, kind: str, occurred_at: str, subject_id: str
) -> tuple[Any, str, str | None]:

    raise not_served(
        "knowledge_graph.ontology.repository_provenance", "MEDULLA-TYPED-PACKS-R651"
    )


def _repo_provenance_ingest(engine: Any, obj: Any, node_id: str, repo_id: str) -> str:
    raise not_served(
        "knowledge_graph.ingestion.envelope_ingest", "MEDULLA-REPO-INGEST-R002"
    )


def _ontology_derive_list() -> str:
    raise not_served(
        "knowledge_graph.ontology.derived_properties", "MEDULLA-TYPED-PACKS-R650"
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
    # CONCEPT:MERISTEM-KG.ontology.optimistic-concurrency-object-property — atomic
    # optimistic-concurrency property set. The object id IS the node id (the
    # ledger persists the edit's target as MERGE (t {id: object_id})), so we
    # condition on the SAME node the edit targets. Apply the set ONLY if the
    # node still matches ``expect`` (missing field ≡ null), under the engine
    # write lock. If we lose the race we record NOTHING and surface
    # applied=false — never a misleading audit edit.
    raise not_served("knowledge_graph.ontology.edits", "MEDULLA-TYPED-PACKS-R651")


def _object_edits_record(ledger: Any, args: _ObjectEditsRecordArgs) -> str:
    raise not_served("knowledge_graph.ontology.edits", "MEDULLA-TYPED-PACKS-R651")


def _object_edits_revert(ledger: Any, edit_id: str, actor: str) -> str:
    raise not_served("knowledge_graph.ontology.edits", "MEDULLA-TYPED-PACKS-R651")


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

    raise not_served("knowledge_graph.core.tenant_registry", "MEDULLA-IDENTITY-R610")


def _graph_share_org(node_id: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "MEDULLA-IDENTITY-R610")


def _graph_share_commons(node_id: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "MEDULLA-IDENTITY-R610")


def _graph_share_mark(node_id: str, marking: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "MEDULLA-IDENTITY-R610")


def _graph_share_private(node_id: str) -> str:
    raise not_served("knowledge_graph.core.tenant_sharing", "MEDULLA-IDENTITY-R610")
