"""graph_claims MCP tool — the X-3 epistemic mining flywheel's governed claim
lifecycle (CONCEPT:AU-KG.evolution.mining-flywheel), exposed as a standalone,
directly-callable surface.

Until now the five-state ``proposed -> validated -> accepted -> deprecated ->
retracted`` lifecycle (``knowledge_graph/research/claim_flywheel.py::
ClaimFlywheel``) only ever fired as a side effect of a mining pass
(``loop_controller._run_insight_validation``/``_run_trace_mining``) — a caller
could not directly propose/validate/accept/deprecate/retract one claim through
either the MCP or REST surface. This tool closes that gap.

**Thin dispatch only** — this module never reimplements the lifecycle's
transition rules (the ``ClaimLifecycleState`` machine, terminal/sticky
retraction) or its governance semantics. Every action is either a straight
read off :class:`~agent_utilities.knowledge_graph.research.claim_flywheel.
ClaimFlywheel` (``get``/``list``), or — for the five state-changing actions —
FIRST a fail-closed :class:`~agent_utilities.orchestration.action_policy.
ActionPolicy` decision (mirroring ``graph_secret``'s ``_gate`` pattern in
``secret_tools.py``) and only THEN, if allowed, the matching
``ClaimFlywheel`` method call. A denied/queued decision returns
``{"error": "policy_denied", ...}`` without ever calling the flywheel.

Provenance is inherited, never re-written here: ``ActionPolicy.decide()``
persists its own ``ActionDecision`` audit node, and every real
``ClaimFlywheel`` transition persists its own append-only
``ClaimLifecycleEvent`` node — both writes already exist upstream of this
tool.

**The universal-ingestion program's governed promotion** (CONCEPT:
AU-KG.ingest.governed-claim-promotion, ``knowledge_graph/ingestion/
promotion.py``) hooks into this SAME dispatch rather than a separate tool:
an ``evaluate`` action runs ``promotion.evaluate_and_advance`` (propose a
candidate claim carrying a proposed ``ChangeEnvelope`` + domain/statement/
confidence, then run every governance gate and advance/hold/reject it —
deliberately ungated, mirroring ``graph_ingest``'s ``sync_second_brain``,
which already proposes claims into this SAME flywheel with no
``action_policy`` check); ``accept`` calls
``promotion.materialize_on_claim_accepted`` (the ONLY place an ingestion
candidate claim's proposed fact gets written — so the fail-closed
``ActionPolicy`` approval-queue hold on ``claim.accept`` IS that program's
steward-review hold, not a second gate); and ``retract`` calls
``promotion.retract_and_supersede`` (tombstones an already-materialized fact
rather than deleting it). The ``accept``/``retract`` hooks are no-ops for any
claim without a ``proposed_envelope`` (every non-ingestion claim family).
"""

from __future__ import annotations

from pydantic import Field

from graph_os.api.errors import not_served
from graph_os.mcp_server import runtime
from graph_os.mcp_server.backing.context_helpers import action_policy_gate


def _gate(kind: str, target: str, reason: str) -> tuple[bool, dict]:
    """Run the fail-closed ActionPolicy gate for one claim-lifecycle mutation.

    Mirrors ``secret_tools._gate`` exactly — a thin call into the shared
    ``get_action_policy(engine).decide(...)`` decision point, never a
    reimplementation of its rules.
    """
    return action_policy_gate(kind, target, reason)


def register_claim_tools(mcp):
    """Register the ``graph_claims`` tool onto the MCP server."""

    @mcp.tool(
        name="graph_claims",
        description=(
            "Drive the X-3 epistemic mining flywheel's governed claim lifecycle "
            "(CONCEPT:AU-KG.evolution.mining-flywheel) directly: proposed -> "
            "validated -> accepted -> deprecated -> retracted (any pre-terminal "
            "state may also be retracted directly; RETRACTED is TERMINAL and "
            "STICKY — 'propose' refuses to reopen a retracted claim). Every "
            "state-changing action ('propose', 'validate', 'accept', 'deprecate', "
            "'retract') FIRST passes the fail-closed ActionPolicy gate "
            "(orchestration/action_policy.py, kind='claim.<action>') — a deny/"
            "queue verdict blocks the mutation and is returned under `policy` — "
            "and only then is routed through the SAME ClaimFlywheel the mining "
            "pipeline uses; this tool never reimplements the transition rules or "
            "recomputes governance validity itself. For an ingestion candidate "
            "claim (knowledge_graph/ingestion/promotion.py), 'evaluate' "
            "(domain, statement, confidence, envelope_json) is the governed-"
            "promotion entrypoint — proposes the claim and runs every "
            "governance gate (classification/policy, PII, SHACL, dedup, "
            "contradiction, per-pack confidence) in one call, UNGATED (no fact "
            "is written here); 'accept' is the only action that materializes "
            "the claim's proposed fact (returned under `materialization`, "
            "fail-closed ActionPolicy-gated — the real steward-review hold), "
            "and 'retract' supersedes an already-materialized fact rather than "
            "deleting it (returned under `superseded_fact`) — all three are "
            "no-ops for every other (non-ingestion) claim family. 'get' "
            "returns one claim's current state + full transition history; "
            "'list' enumerates claims by their current state."
        ),
        tags=["graph-os", "epistemic", "claims", "governance"],
    )
    async def graph_claims(
        action: str = Field(
            default="get",
            description="propose|validate|accept|deprecate|retract|evaluate|get|list",
        ),
        claim_id: str = Field(
            default="", description="Target claim id (every action but 'list')."
        ),
        reason: str = Field(
            default="",
            description="Why this transition is happening — the audit trail.",
        ),
        valid: bool = Field(
            default=True,
            description=(
                "'validate' only: the caller's ALREADY-COMPUTED governance verdict "
                "(e.g. from PromotionGovernanceValidator) — this tool never "
                "recomputes validity itself, it only records it. False HOLDS the "
                "claim at its current state (not a retraction — validate may be "
                "called again later once the hold clears)."
            ),
        ),
        state: str = Field(
            default="",
            description=(
                "'list' only: filter to claims whose CURRENT state equals this "
                "(proposed|validated|accepted|deprecated|retracted)."
            ),
        ),
        limit: int = Field(default=50, description="'list' only: max rows."),
        domain: str = Field(
            default="",
            description=(
                "'evaluate' only: the domain/pack name governing this candidate "
                "claim's per-pack confidence threshold."
            ),
        ),
        statement: str = Field(
            default="", description="'evaluate' only: the candidate claim's text."
        ),
        confidence: float = Field(
            default=1.0,
            description="'evaluate' only: the candidate claim's confidence.",
        ),
        envelope_json: str = Field(
            default="",
            description=(
                "'evaluate' only: JSON ChangeEnvelope.as_dict()-shaped payload — "
                "the write this claim proposes if later accepted."
            ),
        ),
    ) -> str:
        """Propose / validate / accept / deprecate / retract / evaluate / get / list
        claims through the governed ClaimFlywheel lifecycle, ActionPolicy-gated."""

        raise not_served(
            "knowledge_graph.research.claim_flywheel", "EG-DECISION-ENGINE-R652"
        )

    runtime.REGISTERED_TOOLS["graph_claims"] = graph_claims
