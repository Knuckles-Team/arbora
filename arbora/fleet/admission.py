"""Admission of skills and workflows before they run (ARBORA-FLEET-R034).

The engine selects and scores: ``DecisionEval`` stores the attestation of a
local-model certification. Arbora validates that the attestation digests equal
the binding and holds no scoring. A skill or workflow that does not admit
causes no effect: every refusal is raised before the first tool call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arbora.fleet.admission_models import (
    AdmissionRefused,
    CertificationBinding,
    SkillAuditReport,
    WorkflowAdmissionRecord,
    WorkflowBinding,
    canonical_digest,
    parse_certification_binding,
    parse_workflow_binding,
)
from arbora.fleet.admission_policy import (
    SKILL_POLICIES,
    WORKFLOW_POLICIES,
    AdmissionConfig,
)

__all__ = [
    "ADMITTED_OPERATIONS",
    "AdmissionDecision",
    "AdmissionGate",
    "REFUSALS",
    "WorkflowAdmission",
    "admit_workflow",
    "certification_order",
    "validate_certification",
]

#: Operations that load a skill or start a workflow.
ADMITTED_OPERATIONS = frozenset({"agents.run"})

REFUSALS: dict[str, str] = {
    "SKILL_AUDIT_FAILED": "the skill audit exceeded the high-finding limit",
    "WORKFLOW_NOT_ADMITTED": "the workflow declaration, tool or dependency is not admitted",
    "ADMISSION_FIELD_MISSING": "a required admission field is missing",
    "CERTIFICATION_REQUIRED": "the skill has no certification binding",
    "CERTIFICATION_STALE": "the attestation digests differ from the binding",
    "PRODUCER_UNAVAILABLE": "the certification attestation could not be read",
}


def validate_certification(
    binding: CertificationBinding, attestation: Mapping[str, Any] | None
) -> str:
    """Return the attestation digest when every digest equals the binding."""
    if attestation is None:
        raise AdmissionRefused("PRODUCER_UNAVAILABLE", {"skill": binding.skill_id})
    stale = [
        name
        for name in (
            "skill_id",
            "skill_version",
            "model_endpoint_revision",
            "system_prompt_digest",
            "attestation_digest",
        )
        if attestation.get(name) != getattr(binding, name)
    ]
    if tuple(attestation.get("dependency_digests", ())) != binding.dependency_digests:
        stale.append("dependency_digests")
    if stale:
        raise AdmissionRefused(
            "CERTIFICATION_STALE", {"skill": binding.skill_id, "fields": sorted(stale)}
        )
    return binding.attestation_digest


def certification_order(dependencies: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """Atomic skills first, composed skills after their dependencies; cycles refuse."""
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str, path: tuple[str, ...]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise AdmissionRefused(
                "WORKFLOW_NOT_ADMITTED", {"cycle": list((*path, node))}
            )
        state[node] = 1
        for child in dependencies.get(node, ()):
            visit(child, (*path, node))
        state[node] = 2
        order.append(node)

    for node in sorted(dependencies):
        visit(node, ())
    return tuple(order)


@dataclass(frozen=True)
class WorkflowAdmission:
    """The receipt of one admitted workflow: ordered ids, no prompt bodies."""

    certification_order: tuple[str, ...]
    tools: tuple[str, ...]
    workflow_digest: str
    grant_revision: str


def admit_workflow(
    binding: WorkflowBinding,
    grants: Mapping[str, Any],
    declarations: Mapping[str, str],
) -> WorkflowAdmission:
    """Admit every bound tool against its trusted declaration and the caller grant.

    ``grants`` carries ``revision`` and ``tools``; ``declarations`` maps a tool
    to its trusted schema digest. One stale dependency or one denied tool
    refuses the whole workflow before any call.
    """
    order = certification_order(binding.dependencies)
    if grants.get("revision") != binding.grant_revision:
        raise AdmissionRefused(
            "WORKFLOW_NOT_ADMITTED", {"reason": "grant revision changed"}
        )
    allowed = set(grants.get("tools", ()))
    for tool, digest in sorted(binding.tool_schema_digests.items()):
        if tool not in declarations:
            raise AdmissionRefused(
                "WORKFLOW_NOT_ADMITTED", {"tool": tool, "reason": "unknown"}
            )
        if declarations[tool] != digest:
            raise AdmissionRefused("CERTIFICATION_STALE", {"tool": tool})
        if tool not in allowed:
            raise AdmissionRefused(
                "WORKFLOW_NOT_ADMITTED", {"tool": tool, "reason": "denied"}
            )
    return WorkflowAdmission(
        order,
        tuple(sorted(binding.tool_schema_digests)),
        binding.workflow_digest,
        binding.grant_revision,
    )


@dataclass(frozen=True)
class AdmissionDecision:
    """What the gate observed; only digests, policy names and field names."""

    evidence: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmissionGate:
    """The one admission step of ``invoke``, run before the confirmation gate."""

    config: AdmissionConfig = field(default_factory=AdmissionConfig)

    def applies(self, op_id: str) -> bool:
        return op_id in ADMITTED_OPERATIONS

    def evaluate(self, op_id: str, arguments: Mapping[str, Any]) -> AdmissionDecision:
        """Admit or refuse. Raises :class:`AdmissionRefused`; runs no effect."""
        if not self.applies(op_id):
            return AdmissionDecision()
        metadata = (arguments.get("message") or {}).get("metadata") or {}
        evidence: dict[str, str] = {}
        skill = metadata.get("skill")
        if skill is not None:
            evidence.update(self._skill(skill))
        workflow = metadata.get("workflow")
        if workflow is not None:
            evidence.update(self._workflow(workflow))
        return AdmissionDecision(evidence)

    def _skill(self, skill: Mapping[str, Any]) -> dict[str, str]:
        policy = SKILL_POLICIES[self.config.skill_policy]
        report: SkillAuditReport = policy(
            str(skill.get("id") or "skill"),
            str(skill.get("text", "")),
            tuple(str(t) for t in skill.get("tools", ())),
            self.config.skill_max_high_findings,
        )
        evidence = {
            "admission.skill_report_digest": report.digest(),
            "admission.policy": report.policy,
        }
        if not report.passed:
            raise AdmissionRefused(
                "SKILL_AUDIT_FAILED",
                {**evidence, "limit": report.limit, "high": report.high},
            )
        return evidence

    def _workflow(self, workflow: Mapping[str, Any]) -> dict[str, str]:
        name = self.config.workflow_policy
        try:
            record: WorkflowAdmissionRecord = WORKFLOW_POLICIES[name](
                workflow.get("declaration") or {}
            )
        except AdmissionRefused as refusal:
            raise AdmissionRefused(
                "WORKFLOW_NOT_ADMITTED",
                {**refusal.detail, "policy": name, "cause": refusal.code},
            ) from None
        evidence = {
            "admission.workflow_digest": canonical_digest(
                record.model_dump(mode="json")
            ),
            "admission.policy": name,
        }
        return evidence


__all__ += ["parse_certification_binding", "parse_workflow_binding"]
