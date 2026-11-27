"""Typed action-to-operation mapping for retired legacy MCP host actions.

RF-ADR-009 host-composition-boundary R005 requires that every MCP action
the agent runtime's legacy MCP host used to serve is, once that host is
retired, resolved against an *approved* mapping -- not an ad hoc or implicit
one. Every legacy action maps to exactly one registry operation id, or to a
``retired_by`` owner row id: no third outcome exists. This module gives the
mapping its typed shape, a validator that refuses an action with no approved
mapping and a table with the same action listed twice, and
:func:`resolve_legacy_action`, which the MCP dispatch calls
(ARBORA-HOST-R005.2.9).

An action that exists in more than one legacy tool (``status``, ``list``) is keyed
``<tool>.<action>``; an action unique to the tools of the first slices is keyed by
its bare name, which :func:`resolve_legacy_action` tries after the qualified key.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


class DuplicateActionMappingError(ValueError):
    """Raised when the same legacy MCP action appears more than once in a table."""


class UnmappedActionError(ValueError):
    """Raised when a served action has no approved operation mapping."""


@dataclass(frozen=True)
class ActionOperationMapping:
    """One legacy MCP action's approved mapping.

    Exactly one of ``operation`` (a registry operation id) and ``retired_by``
    (the owner row that will serve it) is set. ``params`` are the fixed
    parameters the mapped call carries; ``capability`` names the retired
    capability in the ``UNAVAILABLE`` refusal.
    """

    action: str
    operation: str | None = None
    retired_by: str | None = None
    params: tuple[tuple[str, str], ...] = ()
    capability: str = ""

    def __post_init__(self) -> None:
        if (self.operation is None) == (self.retired_by is None):
            raise ValueError(
                f"action {self.action!r} needs exactly one of operation and retired_by"
            )


@dataclass(frozen=True)
class ResolvedAction:
    """The outcome of resolving one legacy action."""

    action: str
    operation: str | None
    retired_by: str | None
    params: tuple[tuple[str, str], ...]
    capability: str


ActionTable = MappingProxyType[str, ActionOperationMapping]


def build_approved_action_mapping(
    entries: tuple[ActionOperationMapping, ...],
) -> ActionTable:
    """Build an action lookup table, refusing duplicate actions."""

    table: dict[str, ActionOperationMapping] = {}
    for entry in entries:
        if entry.action in table:
            raise DuplicateActionMappingError(
                f"action {entry.action!r} is mapped more than once "
                f"(already -> {_target(table[entry.action])!r}, "
                f"again -> {_target(entry)!r})"
            )
        table[entry.action] = entry
    return MappingProxyType(table)


def _target(entry: ActionOperationMapping) -> str:
    return entry.operation or f"retired_by {entry.retired_by}"


def validate_served_actions(
    served_actions: tuple[str, ...],
    approved: ActionTable,
) -> None:
    """Refuse any served action absent from the approved mapping table."""

    unmapped = sorted({action for action in served_actions if action not in approved})
    if unmapped:
        raise UnmappedActionError(
            f"actions have no approved action-to-operation mapping: {unmapped}"
        )


def _op(action: str, operation: str, **params: str) -> ActionOperationMapping:
    return ActionOperationMapping(
        action, operation, params=tuple(sorted(params.items()))
    )


def _retired(action: str, row: str, capability: str = "") -> ActionOperationMapping:
    return ActionOperationMapping(action, retired_by=row, capability=capability)


def _camel(action: str) -> str:
    return "".join(part.capitalize() for part in action.split("_"))


#: Owner rows named by the retired host's ``not_served`` calls.
_CHOICE = "MEDULLA-DECISION-ENGINE"
_MEMORY_ROW = "MEDULLA-DECISION-ENGINE-R651"
_PLACEMENT_ROW = "MEDULLA-PLACEMENT-R700"
_INGEST_WORKER = "MEDULLA-INGEST-WORKER"

#: Legacy ``graph_analyze`` actions answered by a deterministic graph query.
CODE_NAVIGATION_ACTIONS = ("code_context", "call_graph", "routes")
IMPACT_ACTIONS = ("blast_radius", "change_coupling")
SIMILARITY_ACTIONS = ("similar_code",)
#: The remaining analyze actions answered by the engine query path (R005.2.1.3).
ANALYZE_QUERY_ACTIONS = (
    "code_evolution",
    "adr",
    "cross_repo_usages",
    "code_metrics",
    "arch_report",
    "context",
    "enrichment_coverage",
)
INGEST_SUBMISSION_ACTIONS = (
    "ingest",
    "ingest_url",
    "ingest_knowledge_pack",
    "fact_extract",
    "distill",
)
#: Legacy ``graph_config`` / ``graph_configure`` reads served by ``ops.config``.
CONFIG_READ_ACTIONS = ("describe", "get", "get_config", "list_config")
CODE_GRAPH_QUERY_ACTIONS = CODE_NAVIGATION_ACTIONS + IMPACT_ACTIONS + SIMILARITY_ACTIONS


def _analyze_entries() -> tuple[ActionOperationMapping, ...]:
    """Analyze family (R005.2.1.x): graph questions, never bespoke read operations."""
    return (
        *(
            _op(a, "query.uql")
            for a in CODE_GRAPH_QUERY_ACTIONS + ANALYZE_QUERY_ACTIONS
        ),
        _op("explain", "query.nl"),
        _op("inspect", "ontology.inspect"),
        _op("security_scan", "security.posture.read"),
        _op("readiness", "ops.doctor"),
        _op("process_writeback", "eg.storage.WriteBack"),
        _retired("executable_rag", "MEDULLA-FEDERATED-QUERY-R008"),
        _retired("recommend", "MEDULLA-FEDERATED-QUERY-R652"),
        _retired("placement_plan", _PLACEMENT_ROW, "knowledge_graph.infra"),
        _retired("infra_sweep", _PLACEMENT_ROW, "knowledge_graph.infra"),
        _retired("distill_memory", _MEMORY_ROW, "knowledge_graph.memory"),
        _retired("world_model_rollout", _CHOICE, "knowledge_graph.core.world_model"),
        _retired(
            "research_ingest",
            "MEDULLA-DECISION-ENGINE-R652",
            "knowledge_graph.research.research_intelligence_engine",
        ),
        _retired("check_constraints", _CHOICE, "harness.constraint_engine"),
        *(
            _retired(a, _CHOICE)
            for a in (
                "specialize",
                "evolve_variants",
                "spawn_background",
                "track_citations",
                "guard_corpus",
                "evaluate_harness",
                "evolve_agent",
                "recursive_distill",
                "distill_search",
                "evolve_code",
                "assimilation_benchmark",
                "latent_efficiency_benchmark",
                "infer_links",
                "pick_skill",
                "quant_regime",
                "workforce_plan",
                "harness_gate",
                "harness_evolve",
                "harness_certify",
                "harness_benchmark",
                "synthesize",
                "deep_extract",
                "background_research",
                "relevance_sweep",
                "evaluate",
                "evolve_model",
                "forecast",
                "causal",
                "invariant",
                "extract_claims",
                "contradictions",
                "night_shift",
                "x_workflow",
                "cleanup_documents",
                "epistemic_sync",
                "quant_banking",
                "quant_arb",
                "quant_crypto",
                "quant_microstructure",
                "quant_insider",
                "close",
            )
        ),
    )


def _configure_entries() -> tuple[ActionOperationMapping, ...]:
    """Configure family (R005.2.2): config reads, doctor, vault and hook actions."""
    vault = "openbao-mcp"
    return (
        *(_op(a, "ops.config") for a in CONFIG_READ_ACTIONS),
        _op("generate_config", "ops.config"),
        _op("config_reference", "ops.config"),
        *(
            _op(a, "ops.doctor")
            for a in ("config_doctor", "health", "system_doctor", "preflight", "doctor")
        ),
        _op("set_secret", "fleet.call", server=vault, tool="set_secret"),
        _op("vault_sync", "fleet.call", server=vault, tool="vault_sync"),
        _op("install_hooks", "ops.hooks.install"),
        _op("uninstall_hooks", "ops.hooks.uninstall"),
    )


def _ingest_entries() -> tuple[ActionOperationMapping, ...]:
    """Ingest family (R005.2.3.x): submissions, jobs, sources and retired owners."""
    return (
        *(
            _op(a, "ingest.submissions.create", kind=a)
            for a in INGEST_SUBMISSION_ACTIONS
        ),
        _op("import_pack", "ingest.submissions.create", kind="ingest_knowledge_pack"),
        _op("corpus", "ingest.sources.list"),
        _retired("corpus_build", _INGEST_WORKER, "knowledge_graph.kb.corpus"),
        _op("jobs", "ingest.jobs.list"),
        _op("job_status", "ingest.jobs.status"),
        _op("status", "ingest.jobs.status"),
        _op("cancel", "ingest.jobs.cancel"),
        _op("prioritize", "ingest.jobs.prioritize"),
        _retired("clear", _INGEST_WORKER, "knowledge_graph.ingest.queue"),
        _op("rebuild_indexes", "ingest.repositories.index"),
        _op("observe", "telemetry.read"),
        _op("sync", "ingest.sources.sync"),
        _op("materialize", "ingest.sources.sync", mode="full"),
        _op("materialize_source", "ingest.sources.sync", mode="full"),
        _retired("reflect", _CHOICE, "knowledge_graph.core.reflection"),
        _op("agent_toolkit", "fleet.catalog.list"),
        _op("profile", "telemetry.read"),
        _op("curate_wiki", "knowledge.curation.propose"),
        _retired("sync_second_brain", _INGEST_WORKER),
        _retired("skill_workflows", "MEDULLA-DECISION-ENGINE-R166"),
        *(
            _retired(a, _INGEST_WORKER)
            for a in (
                "cohort_create",
                "cohort_status",
                "extract_submit",
                "extract_jobs",
                "extract_status",
                "extract_pause",
                "extract_resume",
                "extract_jsonl",
                "build_skill_graph",
                "skill_graph_status",
                "rebuild_skill_graph",
                "gitlab_sync",
                "gitlab_webhook",
                "fleet_relevance",
                "backfill_platform_history",
                "archivebox_sync",
                "cdc_catchup",
                "opensearch_reindex",
                "classify_topics",
                "enrich_pending_documents",
            )
        ),
    )


def _tool(tool: str, entries: tuple[ActionOperationMapping, ...]):
    """Qualify one legacy tool's entries as ``<tool>.<action>``."""
    return tuple(
        ActionOperationMapping(
            f"{tool}.{e.action}", e.operation, e.retired_by, e.params, e.capability
        )
        for e in entries
    )


def _query_memory_entries() -> tuple[ActionOperationMapping, ...]:
    """Query-memory family (R005.2.4): graph_query, graph_memory and graph_write."""
    reads = {
        "local": "query.uql",
        "uql": "query.uql",
        "sql": "query.sql",
        "sparql": "query.sparql",
        "federated": "query.cross_source_report",
    }
    memory = (
        _op("recall", "memory.read"),
        _op("store", "memory.write"),
        _op("link", "memory.write"),
        *(
            _retired(a, _MEMORY_ROW)
            for a in (
                "create_summary",
                "consolidate",
                "maintain",
                "add_scene_object",
                "world_transform",
                "start_trajectory",
                "append_step",
                "discounted_return",
            )
        ),
    )
    writes = (
        _op("add_node", "eg.graph.AddNode"),
        _op("add_edge", "eg.graph.AddEdge"),
        _op("delete_node", "eg.graph.RemoveNode"),
        _op("delete_edge", "eg.graph.RemoveEdge"),
        _op("bulk_ingest", "eg.graph.ApplyMutation"),
        _op("compare_and_set", "eg.graph.ApplyMutation"),
        _op("store_memory", "memory.write"),
        _op("register_external_graph", "federation.sources.register"),
        _op("log_chat", "chats.create"),
        _retired("recall_media", _MEMORY_ROW),
        _retired("register_execution", _CHOICE),
        _retired("check_loop", _CHOICE),
    )
    return (
        *_tool("graph_query", tuple(_op(k, v) for k, v in reads.items())),
        *_tool("graph_memory", memory),
        *_tool("graph_write", writes),
        _op("recall_memory", "memory.read"),
        _op("submit_sdd", "agents.plan_run"),
    )


def _orchestrate_entries() -> tuple[ActionOperationMapping, ...]:
    """Orchestrate family (R005.2.5): runs map to agents.*; scheduling is a choice."""
    return (
        *_tool(
            "graph_orchestrate",
            (
                _op("execute_agent", "agents.run"),
                _op("execute_workflow", "workflows.run"),
            ),
        ),
        *_tool(
            "graph_agents",
            (
                _op("run_org", "agents.run"),
                *(
                    _retired(a, _CHOICE)
                    for a in ("swarm", "computer_use", "synthesize_org", "reason")
                ),
            ),
        ),
        *_tool(
            "graph_loops",
            (
                _op("run", "loops.run"),
                _op("state", "loops.status"),
                *(
                    _retired(a, _CHOICE)
                    for a in (
                        "submit",
                        "list",
                        "drive",
                        "cancel",
                        "prioritize",
                        "specs",
                        "review",
                        "gaps",
                        "submit_gap",
                        "gap",
                    )
                ),
            ),
        ),
        *_tool(
            "graph_goals",
            (
                _op("list", "agents.goals.list"),
                _op("iterations", "agents.goals.iterations"),
                _op("cancel", "agents.goals.cancel"),
                _retired("create", _CHOICE),
            ),
        ),
        *_tool(
            "graph_evolution",
            (
                _op("publish_proposal", "evolution.proposals.list"),
                *(
                    _retired(a, _CHOICE)
                    for a in (
                        "assimilate",
                        "audit_scan",
                        "distill_skills",
                        "standardize",
                        "failure_ingest",
                        "optimize_component",
                        "evidence_lineage",
                    )
                ),
            ),
        ),
    )


MINE_ACTIONS = (
    "associate",
    "cluster",
    "anomaly",
    "classify_fit",
    "classify_predict",
    "reduce",
    "sequence",
    "forecast",
    "text",
    "subgraph",
    "entity_resolve",
    "causal_impact",
    "process",
    "root_cause",
    "risk_propagation",
    "ontology_gap",
    "retrieval_quality",
    "community",
)
DEEP_MINE_ACTIONS = (
    "deep_forecast",
    "deep_classify",
    "autoencoder_anomaly",
    "xgboost",
    "embed",
)
EVALUATE_ACTIONS = (
    "evaluate",
    "evaluate_harness",
    "guard_corpus",
    "harness_gate",
    "latent_efficiency_benchmark",
    "assimilation_benchmark",
    "evolve_model",
    "evolve_code",
    "specialize",
    "forecast",
    "causal",
    "invariant",
    "quant_crypto",
    "quant_microstructure",
    "quant_regime",
    "quant_insider",
)


def _mine_learn_entries() -> tuple[ActionOperationMapping, ...]:
    """Mine-learn family (R005.2.6): engine computations; verdicts are choices."""
    return (
        *_tool(
            "graph_mine",
            tuple(_op(a, f"eg.compute.Mine{_camel(a)}") for a in MINE_ACTIONS),
        ),
        *_tool(
            "graph_mine_deep",
            tuple(
                _op(a, "fleet.call", server="data-science-mcp", tool=a)
                for a in DEEP_MINE_ACTIONS
            ),
        ),
        *_tool(
            "graph_learn",
            (
                _op("fit", "eg.compute.GraphLearnFit"),
                _op("predict", "eg.compute.GraphLearnPredict"),
            ),
        ),
        *_tool("graph_evaluate", tuple(_retired(a, _CHOICE) for a in EVALUATE_ACTIONS)),
    )


def _governance_entries() -> tuple[ActionOperationMapping, ...]:
    """Governance-ops family (R005.2.7): access, policy, ontology and audit."""
    return (
        *_tool(
            "graph_audit",
            (
                _op("verify", "eg.security.AuditVerify"),
                _op("for_target", "identity.audit.list"),
                _op("lineage", "lifecycle.trace"),
            ),
        ),
        *_tool(
            "graph_governance",
            (
                _op("grant_approval", "approvals.decide"),
                _op("verify_action", "access.check"),
                _op("ownership_report", "access.ownership.plan"),
                _op("ownership_apply", "access.ownership.apply"),
                _op("claim_ownership", "access.ownership.apply"),
                _op("policy_status", "policy.read"),
                _retired("submit_risk_veto", _CHOICE),
            ),
        ),
        *_tool(
            "graph_compliance",
            (
                _op("posture", "security.posture.read"),
                _op("export", "identity.audit.export"),
            ),
        ),
        *_tool(
            "graph_bus",
            (
                *(
                    _op(a, f"bus.{a}")
                    for a in (
                        "register",
                        "heartbeat",
                        "roster",
                        "send",
                        "receive",
                        "dispatch",
                    )
                ),
                *(
                    _retired(a, _CHOICE)
                    for a in ("subscribe", "unsubscribe", "leave", "status")
                ),
            ),
        ),
        *_tool(
            "graph_ontology",
            (
                _op("load", "deploy.ontology.import"),
                _op("list", "ontology.schema.list"),
                _op("get", "ontology.schema.classes"),
                _retired("update", "MEDULLA-TYPED-PACKS-R105"),
                _retired("delete", "MEDULLA-TYPED-PACKS-R105"),
            ),
        ),
    )


#: The approved mapping of every legacy MCP host action, each traced to the
#: operation or owner row that replaces it. The legacy list is frozen in
#: ``tests/mcp_server/fixtures/legacy_mcp_actions.json``.
APPROVED_ACTION_MAPPING = build_approved_action_mapping(
    (
        *_analyze_entries(),
        *_configure_entries(),
        *_ingest_entries(),
        *_query_memory_entries(),
        *_orchestrate_entries(),
        *_mine_learn_entries(),
        *_governance_entries(),
    )
)


def resolve_legacy_action(action: str, tool: str | None = None) -> ResolvedAction:
    """Resolve one retired MCP action to its operation or its owner row.

    A tool-qualified key is tried first, then the bare action name. An action
    with no approved mapping raises :class:`UnmappedActionError`; the dispatch
    surfaces that as ``INVALID_REQUEST``.
    """

    for key in (f"{tool}.{action}" if tool else "", action):
        entry = APPROVED_ACTION_MAPPING.get(key) if key else None
        if entry is not None:
            return ResolvedAction(
                action=action,
                operation=entry.operation,
                retired_by=entry.retired_by,
                params=entry.params,
                capability=entry.capability or f"legacy_mcp.{action}",
            )
    raise UnmappedActionError(f"action {action!r} has no approved mapping")


def require_served_action(action: str, tool: str | None = None) -> ResolvedAction:
    """Resolve at the MCP dispatch: refuse a retired or unknown legacy action.

    A ``retired_by`` action answers ``UNAVAILABLE`` naming its owner row; an
    unknown action answers ``INVALID_ARGUMENT``. A mapped action is returned for
    the caller to serve (ARBORA-HOST-R005.2.9).
    """
    from arbora.api.errors import ArboraErrorCode, ArboraRefusal, not_served

    try:
        resolved = resolve_legacy_action(action, tool)
    except UnmappedActionError as exc:
        raise ArboraRefusal(
            code=ArboraErrorCode.INVALID_ARGUMENT,
            message=f"Unknown legacy action {action!r}",
            details={"action": action},
        ) from exc
    if resolved.retired_by is not None:
        raise not_served(resolved.capability, resolved.retired_by)
    return resolved
