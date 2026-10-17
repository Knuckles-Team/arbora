"""Focused graph-os evolution and enterprise-knowledge improvement operations."""

from __future__ import annotations

import json
from typing import Any

from agent_utilities.security.error_surface import public_error_text
from pydantic import Field

from graph_os.api.errors import not_served
from graph_os.mcp_server import runtime


def register_evolution_tools(mcp: Any) -> None:
    """Register on-demand evolution operations that share the daemon's native cores."""

    @mcp.tool(
        name="graph_evolution",
        description=(
            "Improve the graph and its executable knowledge. Actions: 'assimilate', "
            "'audit_scan' (Macroscope-class code-correctness/security findings over the "
            "ingested code KG, each filed as a canonical :Gap via "
            "harness.audit_gap_detector), 'distill_skills', 'standardize', "
            "'failure_ingest', 'optimize_component', 'publish_proposal', and "
            "'evidence_lineage' (walk one :EvolutionEvidence node forward through "
            "claim -> proposal -> capability-ratchet verdict, read-only). All "
            "proposal-producing actions remain review-gated."
        ),
        tags=["graph-os", "evolution", "optimization", "skills"],
    )
    def graph_evolution(
        action: str = Field(
            default="assimilate",
            description=(
                "assimilate | audit_scan | distill_skills | standardize | "
                "failure_ingest | optimize_component | publish_proposal | "
                "evidence_lineage"
            ),
        ),
        target: str = Field(
            default="",
            description=(
                "Proposal id, optimization component, distillation proposal id, or "
                "(for evidence_lineage) the :EvolutionEvidence node id. "
                "Use all/sweep for a full optimization sweep."
            ),
        ),
        data_json: str = Field(
            default="{}", description="JSON input data for component optimization."
        ),
        limit: int = Field(
            default=20, description="Bounded result/materialization limit."
        ),
        synthesize: bool = Field(
            default=False,
            description="Synthesize synergy proposals during assimilation.",
        ),
        force: bool = Field(
            default=False,
            description="Re-run assimilation even when its watermark is unchanged.",
        ),
        draft: bool = Field(
            default=False,
            description="Render reviewable skill drafts while distilling.",
        ),
    ) -> str:
        engine = runtime._get_engine()
        if engine is None:
            return "Error: IntelligenceGraphEngine not active."
        try:
            if action == "assimilate":
                raise not_served(
                    "knowledge_graph.research.loop_controller",
                    "EG-DECISION-ENGINE-R653",
                )

            if action == "distill_skills":
                raise not_served(
                    "knowledge_graph.distillation.skill_synthesizer",
                    "EG-TYPED-PACKS-R700",
                )

            if action == "standardize":
                raise not_served(
                    "knowledge_graph.standardization", "EG-UNIFIED-DATA-PLANE-R701"
                )

            if action == "failure_ingest":
                raise not_served(
                    "knowledge_graph.adaptation.failure_analyzer",
                    "EG-DECISION-ENGINE-R701",
                )

            if action == "audit_scan":
                # Code-correctness/security-audit gap track (CONCEPT:AU-AHE.harness.audit-gap-detector):
                # file a canonical :Gap per Macroscope-class finding over the ingested
                # code KG (opt-in KG_LOOP_AUDIT — self-gated).
                from agent_utilities.harness.audit_gap_detector import (
                    run_audit_gap_scan,
                )

                return json.dumps(
                    run_audit_gap_scan(engine, limit=max(1, int(limit))),
                    indent=2,
                    default=str,
                )

            if action == "optimize_component":
                from agent_utilities.harness.program_optimization import (
                    run_component_optimization,
                    run_optimization_sweep,
                )

                component = target.strip().lower()
                if component in {"", "all", "sweep"}:
                    report = run_optimization_sweep(engine)
                else:
                    data = json.loads(data_json) if data_json else {}
                    if not isinstance(data, dict):
                        raise ValueError("data_json must decode to an object")
                    report = run_component_optimization(component, data, engine=engine)
                return json.dumps(report, indent=2, default=str)

            if action == "evidence_lineage":
                # (7.6, D-71-4) expose evidence.evidence_lineage() — previously
                # import-only — on the MCP/REST two-surface contract, dispatching
                # into the SAME core the direct-import callers already use.
                if not target:
                    raise ValueError("target evidence id is required")
                raise not_served(
                    "knowledge_graph.research.evidence", "EG-DECISION-ENGINE-R652"
                )

            if action == "publish_proposal":
                if not target:
                    raise ValueError("target proposal id is required")
                raise not_served(
                    "knowledge_graph.research.change_publisher",
                    "AU-harness (deleted from AU)",
                    owner="agent-utilities",
                )

            return f"Error: Unknown graph_evolution action '{action}'"
        except PermissionError:
            raise
        except Exception as exc:
            return public_error_text(exc)

    runtime.REGISTERED_TOOLS["graph_evolution"] = graph_evolution
    runtime.ACTION_TOOL_ROUTES["graph_evolution"] = "/graph/evolution"
