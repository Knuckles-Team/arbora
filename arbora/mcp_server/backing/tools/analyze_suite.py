"""arbora MCP analyze SUITE (register_analyze_suite_tools).

``graph_analyze`` had grown to ~30 actions spanning six unrelated domains in one wall of
description — hard for an agent to select against. This splits that surface into a few
COHESIVE, intent-scoped tools, each with a short scannable description:

* ``graph_code``     — code intelligence (call graph, similar code, routes, impact, …)
* ``graph_research`` — assimilation / research pipeline
* ``graph_evaluate`` — eval, harness gates, world-model, forecasting
* ``graph_explain``  — the universal context plane (one cited answer per question)
* ``graph_observe``  — KG-native observability/eval analytics (CONCEPT:AU-KG.ingest.observability-queries-opik-cannot)

The code/research/evaluate/explain tools are thin facades: they carry only a focused
description and delegate to the same action core without duplicating logic.
``graph_observe`` routes to trace analytics. ``graph_analyze`` is strictly the six-action
operations/structural surface; it is not a compatibility catch-all.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from arbora.api.evidence import EvidenceBundle
from arbora.gateway.schemas.graph_core import (
    GraphCodeAction,
    GraphEvaluateAction,
    GraphExplainAction,
    GraphResearchAction,
)
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.tools._surface_selection import (
    conditional_tool_decorator,
    register_conditional_tools,
)
from arbora.mcp_server.backing.tools.analysis_tools import execute_focused_analysis

_OptionalNodeId = Annotated[str, Field(description="Optional node id.")]
_OptionalDepth = Annotated[int, Field(description="Optional depth.")]
_OptionalTarget = Annotated[str, Field(description="Optional target.")]


def register_analyze_suite_tools(
    mcp: Any, *, include_unserved_research: bool = False
) -> None:
    """Register the focused analyze-suite tools on the FastMCP server."""

    async def _delegate(
        action: str, query: str, top_k: int, node_id: str, depth: int, target: str
    ) -> Any:
        return await execute_focused_analysis(
            action=action,
            query=query,
            top_k=top_k,
            node_id=node_id,
            depth=depth,
            target=target,
        )

    @mcp.tool(
        name="graph_code",
        description=(
            "Understand a CODEBASE via the ingested code graph — query this before grep. "
            "Actions: 'code_context' (cited how/usage/impact answer for a symbol/area; "
            "target=how|usage|impact, node_id=optional :Code anchor — KG-2.134), "
            "'cross_repo_usages' (every usage of a symbol across the fleet — KG-2.135), "
            "'call_graph' (callers/callees/inherits for node_id; target=callees|callers|"
            "inherits — KG-2.100), 'similar_code' (near-clones of node_id — KG-2.101), "
            "'routes' (HTTP route→handler→service graph — KG-2.102), 'change_coupling' "
            "(files that change together; target=repo path — KG-2.104), 'code_evolution' "
            "(query the ingested commit-history graph; target=file|owners|hotspots|coupled, "
            "query=file/subsystem path — file timelines, ownership, churn hotspots, co-change "
            "— KG-2.283), 'blast_radius' "
            "(transitive impact of node_id; depth — what breaks if I change this), "
            "'code_metrics' (god nodes / communities / bridges — KG-2.210), 'arch_report' "
            "(regenerable architecture report — KG-2.213), 'adr' (architecture decision records)."
        ),
        tags=["arbora", "code"],
    )
    async def graph_code(
        action: GraphCodeAction = Field(
            default="code_context",
            description="code_context | cross_repo_usages | call_graph | similar_code | routes | change_coupling | code_evolution | blast_radius | code_metrics | arch_report | adr",
        ),
        query: str = Field(
            default="", description="Question / area / symbol name / repo path."
        ),
        top_k: int = Field(default=10, description="Result count."),
        node_id: str = Field(
            default="",
            description="Symbol/:Code node id (call_graph, similar_code, blast_radius).",
        ),
        depth: int = Field(default=2, description="Traversal depth (blast_radius)."),
        target: str = Field(
            default="",
            description="how|usage|impact (code_context) or callees|callers|inherits (call_graph).",
        ),
    ) -> EvidenceBundle:
        """Code intelligence over the ingested, resolved code graph."""
        return await _delegate(action, query, top_k, node_id, depth, target)

    @conditional_tool_decorator(
        mcp,
        name="graph_research",
        excluded_names=frozenset({"graph_research"}),
        include_excluded=include_unserved_research,
        description=(
            "Run the research/assimilation pipeline. Actions: 'synthesize' (synthesize "
            "knowledge from a source), 'deep_extract' (deep entity/relation extraction), "
            "'background_research' (spawn background research), 'relevance_sweep' (sweep "
            "ingested content for relevance), 'research_ingest' (ingest a research artifact), "
            "'evolve_variants' (evolve solution variants), 'track_citations' (citation graph), "
            "'spawn_background' (spawn a background analysis job), 'night_shift' (run one "
            "autonomous overnight second-brain research cycle over a local markdown vault — "
            "scout/catalog/cartograph/critique/edit; target=vault root, propose-only), "
            "'contradictions' (the night-shift Critic — flag existing nodes that OPPOSE a "
            "new claim in `query`; propose-only, KG-2.83). "
            "query=source/topic; jobs return a job_id to poll with graph_ingest(action='status')."
        ),
        tags=["arbora", "research"],
    )
    async def graph_research(
        action: GraphResearchAction = Field(
            default="synthesize",
            description="synthesize | deep_extract | background_research | relevance_sweep | research_ingest | evolve_variants | track_citations | spawn_background | night_shift | contradictions",
        ),
        query: str = Field(default="", description="Source / topic / artifact."),
        top_k: int = Field(default=10, description="Complexity budget / result count."),
        node_id: _OptionalNodeId = "",
        depth: _OptionalDepth = 2,
        target: _OptionalTarget = "",
    ) -> EvidenceBundle:
        """Research assimilation + knowledge-synthesis pipeline."""
        return await _delegate(action, query, top_k, node_id, depth, target)

    @mcp.tool(
        name="graph_evaluate",
        description=(
            "Evaluate agents/harnesses and reason over learned world models. Actions: "
            "'evaluate' (score outputs), 'evaluate_harness' (run the "
            "harness eval), 'guard_corpus' (reliability/eval corpus gate), 'harness_gate' "
            "(formal concentration/no-regression SHACL gate — AHE-3.53), 'check_constraints', "
            "'specialize' (one SAI specialization cycle + superhuman cert — AHE-3.29), "
            "'world_model_rollout' (forward-simulate the world model — KG-2.73b), "
            "'latent_efficiency_benchmark' (AHE-3.48), 'assimilation_benchmark' "
            "(measured empirical-parity evidence for assimilated paper mechanisms "
            "vs baseline — AHE-3.39), 'evolve_model', 'evolve_code' "
            "(Monte-Carlo GRAPH search code evolution driven by an LLM coder, falling "
            "back deterministically offline — KG-2.92/MLEvolve; query=task description), "
            "'forecast', 'causal', "
            "'invariant', plus the finance evaluation actions 'quant_crypto', "
            "'quant_microstructure', 'quant_regime', and 'quant_insider'."
        ),
        tags=["arbora", "evaluate"],
    )
    async def graph_evaluate(
        action: GraphEvaluateAction = Field(
            default="evaluate",
            description="evaluate | evaluate_harness | guard_corpus | harness_gate | check_constraints | specialize | world_model_rollout | latent_efficiency_benchmark | assimilation_benchmark | evolve_model | evolve_code | forecast | causal | invariant | quant_crypto | quant_microstructure | quant_regime | quant_insider",
        ),
        query: str = Field(
            default="",
            description="Subject of the evaluation (JSON / id / start state).",
        ),
        top_k: int = Field(default=10, description="Steps / result count."),
        node_id: _OptionalNodeId = "",
        depth: _OptionalDepth = 2,
        target: _OptionalTarget = "",
    ) -> EvidenceBundle:
        """Evaluation, harness gates, world-model rollouts, forecasting/causal analysis."""
        return await _delegate(action, query, top_k, node_id, depth, target)

    @mcp.tool(
        name="graph_explain",
        description=(
            "The UNIVERSAL context plane (CONCEPT:AU-KG.retrieval.route-question-its-domain): route a question to its domain "
            "provider and return ONE grounded, cited answer. action='explain' with "
            "target='domain:intent' (e.g. 'ops:why', 'code:usage', 'deploy:status', "
            "'entity:health') — or a bare intent with the domain inferred, or target='domains' "
            "to list providers. action='context' returns a synthesized context bundle; "
            "action='executable_rag' runs the grounded multi-hop retriever. "
            "Domains: code, ops (live task-queue), deploy (is my change live — KG-2.138), "
            "entity/tickets/deploys/process (KG-2.139), capability (Capability Power "
            "Descriptor for a arbora tool by id, e.g. 'capability:graph_query', or "
            "'capability:list' for the browsable index — Seam 8 Phase 1, "
            "CONCEPT:AU-KG.retrieval.capability-power-descriptor). "
            "action='recommend' shares this tool's hybrid-retrieval substrate but returns "
            "RANKED items (not a single cited answer): a PauseRec implicit-reasoning "
            "generative recommender over query=intent/history — KG-2.93."
        ),
        tags=["arbora", "explain"],
    )
    async def graph_explain(
        action: GraphExplainAction = Field(
            default="explain",
            description="explain | context | executable_rag | recommend",
        ),
        query: str = Field(default="", description="The question."),
        top_k: int = Field(default=10, description="Result count."),
        node_id: str = Field(default="", description="Optional anchor node id."),
        depth: _OptionalDepth = 2,
        target: str = Field(
            default="", description="'domain:intent' | bare intent | 'domains'."
        ),
    ) -> EvidenceBundle:
        """One grounded, cited answer per question, routed to the right domain provider."""
        return await _delegate(action, query, top_k, node_id, depth, target)

    register_conditional_tools(
        runtime.REGISTERED_TOOLS,
        [
            ("graph_code", graph_code, None),
            ("graph_research", graph_research, None),
            ("graph_evaluate", graph_evaluate, None),
            ("graph_explain", graph_explain, None),
        ],
        excluded_names=frozenset({"graph_research"}),
        include_excluded=include_unserved_research,
    )
