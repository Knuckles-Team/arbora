"""Admission of skills and workflows before they run (ARBORA-FLEET-R034).

The engine selects and scores: ``DecisionEval`` stores the attestation of a
local-model certification. Arbora validates that the attestation digests equal
the binding and holds no scoring. A skill or workflow that does not admit
causes no effect: every refusal is raised before the first tool call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arbora.api.domain.fleet.admission_models import (
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

logger = logging.getLogger(__name__)

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
    "POLICY_UNAVAILABLE": "the narrowing policy could not decide for a workflow tool",
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
    #: The engine port (``EngineAdmission``): the stored attestation by digest and
    #: the ``SkillUpgrade`` decision. ``None`` fails a bound request closed.
    port: Any = None
    #: ``async grants(caller) -> {"revision", "tools"}`` and ``async
    #: declarations(caller) -> {tool: trusted schema digest}`` (``CatalogAdmissionSources``
    #: in production); ``None`` fails a workflow binding closed.
    grants: Callable[[Any], Awaitable[Mapping[str, Any]]] | None = None
    declarations: Callable[[Any], Awaitable[Mapping[str, str]]] | None = None

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

    async def evaluate_bound(
        self,
        op_id: str,
        arguments: Mapping[str, Any],
        caller: Any,
        policy_gate: Any,
    ) -> AdmissionDecision:
        """Validate certification, workflow binding and skill upgrade; no effect runs.

        Runs after :meth:`evaluate`. A skill ``certification``, a workflow
        ``binding`` with its dependency ``certifications`` and a skill ``upgrade``
        are each checked only when presented; every refusal precedes the first
        tool call, and the narrowing runs through the one ``PolicyGate``.
        """
        if not self.applies(op_id):
            return AdmissionDecision()
        metadata = (arguments.get("message") or {}).get("metadata") or {}
        tenant = str(getattr(caller, "tenant", ""))
        evidence: dict[str, str] = {}
        skill = metadata.get("skill") or {}
        workflow = metadata.get("workflow") or {}
        if "certification" in skill:
            binding = parse_certification_binding(skill["certification"])
            evidence["admission.certification_receipts"] = await self._certify(
                tenant, binding
            )
        if "upgrade" in skill:
            evidence.update(await self._upgrade(tenant, skill["upgrade"]))
        if "binding" in workflow:
            evidence.update(
                await self._bound_workflow(tenant, workflow, caller, policy_gate)
            )
        return AdmissionDecision(evidence)

    async def _certify(self, tenant: str, binding: CertificationBinding) -> str:
        if self.port is None:
            raise AdmissionRefused("PRODUCER_UNAVAILABLE", {"skill": binding.skill_id})
        stored = await self.port.attestation(tenant, binding.attestation_digest)
        return validate_certification(binding, stored)

    async def _upgrade(self, tenant: str, upgrade: Mapping[str, Any]) -> dict[str, str]:
        from arbora.fleet.definitions import (
            DefinitionRefused,
            admit_definition,
            parse_definition,
        )
        from arbora.fleet.engine_admission import SkillUpgradeQuestion

        if self.port is None:
            raise AdmissionRefused("PRODUCER_UNAVAILABLE", {"reason": "no engine port"})
        try:
            prior = parse_definition(upgrade["prior"])
            candidate = parse_definition(upgrade["candidate"])
            question = SkillUpgradeQuestion(
                subject_id=candidate.producer_id,
                previous_fields=tuple(upgrade.get("previous_fields", ())),
                new_fields=tuple(upgrade.get("new_fields", ())),
                declared_migration_id=upgrade.get("migration_id"),
                claimed_previous_digest=prior.schema_digest,
                claimed_new_digest=candidate.schema_digest,
            )
            decision = await self.port.skill_upgrade(tenant, question)
            admit_definition(prior, candidate, decision, tenant=tenant)
        except DefinitionRefused as refusal:
            raise AdmissionRefused(refusal.code, refusal.detail) from None
        except KeyError as missing:
            raise AdmissionRefused(
                "ADMISSION_FIELD_MISSING", {"fields": [str(missing.args[0])]}
            ) from None
        return {
            "admission.decision_ids": decision.decision_id,
            "admission.migration_id": decision.migration_id or "",
        }

    async def _bound_workflow(
        self,
        tenant: str,
        workflow: Mapping[str, Any],
        caller: Any,
        policy_gate: Any,
    ) -> dict[str, str]:
        # The tenant and caller are the verified ones, never request fields.
        binding = parse_workflow_binding(
            {
                **workflow["binding"],
                "tenant": tenant,
                "caller": str(getattr(caller, "principal", "")),
            }
        )
        if self.grants is None or self.declarations is None:
            raise AdmissionRefused(
                "WORKFLOW_NOT_ADMITTED", {"reason": "no grant source"}
            )
        by_skill: dict[str, str] = {}
        for item in workflow.get("certifications", ()):
            cert = parse_certification_binding(item)
            by_skill[cert.skill_id] = await self._certify(tenant, cert)
        order = certification_order(binding.dependencies)
        missing = [skill for skill in order if skill not in by_skill]
        if missing:
            raise AdmissionRefused("CERTIFICATION_REQUIRED", {"skills": missing})
        await self._narrow(binding, caller, policy_gate)
        admitted = admit_workflow(
            binding, await self.grants(caller), await self.declarations(caller)
        )
        return {
            "admission.certification_receipts": ",".join(by_skill[k] for k in order),
            "admission.certification_order": ",".join(order),
            "admission.workflow_snapshot_digest": admitted.workflow_digest,
            "admission.grant_revision": admitted.grant_revision,
        }

    async def _narrow(
        self, binding: WorkflowBinding, caller: Any, policy_gate: Any
    ) -> None:
        """Every bound tool passes the one narrowing boundary (ARBORA-FLEET-R019.1)."""
        from arbora.api.policy.eunomia import fleet_resource

        names = sorted(binding.tool_schema_digests)
        items = [fleet_resource("tool", n, required_scopes=()) for n in names]
        try:
            allowed = await policy_gate.loadable(items, caller)
        except Exception:
            logger.warning("_narrow failed", exc_info=True)
            raise AdmissionRefused("POLICY_UNAVAILABLE") from None
        for name, ok in zip(names, allowed, strict=True):
            if ok is not True:
                raise AdmissionRefused(
                    "WORKFLOW_NOT_ADMITTED", {"tool": name, "reason": "policy denied"}
                )

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
