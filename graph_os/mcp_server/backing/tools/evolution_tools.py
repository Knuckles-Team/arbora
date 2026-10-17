"""Focused graph-os evolution and enterprise-knowledge improvement operations."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from graph_os.core.handoff import raise_moved
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
        raise_moved("graph_evolution", "harness.audit_gap_detector", "epistemic-graph")

    runtime.REGISTERED_TOOLS["graph_evolution"] = graph_evolution
    runtime.ACTION_TOOL_ROUTES["graph_evolution"] = "/graph/evolution"
