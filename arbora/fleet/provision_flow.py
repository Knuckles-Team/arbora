"""Integrated discovery, distribution and provisioning of one work item (ARBORA-FLEET-R027.2/.4/.5).

``provision_for_work_item`` discovers the authorized skill, reads the engine's
distribution decision, provisions the agent through ``provision_instances`` and
claims success only when the graph read and the agent observation name the same skill
identity. Arbora ranks and decides nothing: every answer comes from a port. Each
refusal stops before the affected dispatch and keeps any known allocation state; no
rollback is claimed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from arbora.a2a.work_items import WorkItemView
from arbora.api.contracts.provisioning_capacity import (
    ProvisionRequest,
    ProvisionResult,
)
from arbora.control_plane.policy.models import canonical_digest
from arbora.control_plane.runs.audit import AuditChain
from arbora.fleet.provision_receipt import (
    IntegratedProvisionReceipt,
    PhaseBinding,
)

__all__ = [
    "EVIDENCE_KIND",
    "REFUSALS",
    "Decision",
    "FlowPorts",
    "ProducerUnavailable",
    "ProvisionFlowRefused",
    "RunResult",
    "SkillRef",
    "provision_for_work_item",
]

EVIDENCE_KIND = "provision.flow.evidence"

REFUSALS: Mapping[str, str] = MappingProxyType(
    {
        "SKILL_NOT_FOUND": "no authorized skill answers the request",
        "PRODUCER_UNAVAILABLE": "a discovery or distribution producer is unavailable",
        "SCHEMA_INCOMPATIBLE": "the skill identity or schema disagrees across the sources",
        "CROSS_TENANT_RECEIPT": "a receipt belongs to another tenant",
        "TOOL_NOT_APPROVED": "the skill needs a tool the decision did not approve",
    }
)


class ProducerUnavailable(RuntimeError):
    """A port could not answer; the flow refuses ``PRODUCER_UNAVAILABLE``."""


class ProvisionFlowRefused(RuntimeError):
    """A closed refusal. ``details`` keeps known allocation state, never a protected identity."""

    def __init__(self, code: str, **details: Any) -> None:
        if code not in REFUSALS:
            raise ValueError(f"unknown flow refusal {code!r}")
        self.code = code
        self.details = details
        super().__init__(code)


@dataclass(frozen=True)
class SkillRef:
    skill_id: str
    version: str
    digest: str
    schema_digest: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    decision_id: str
    tenant: str
    skill_id: str
    schema_digest: str
    approved_tools: tuple[str, ...]


@dataclass(frozen=True)
class RunResult:
    model_trace_id: str
    result_receipt_id: str
    result_digest: str
    skill_digest: str


@dataclass(frozen=True)
class FlowPorts:
    """Every answer the flow consumes; none is computed in Arbora."""

    discover: Callable[[WorkItemView], tuple[str, tuple[SkillRef, ...]]]
    distribution: Callable[[WorkItemView, SkillRef], Decision]
    graph_read: Callable[[str], tuple[str, str]]
    """``skill_id`` -> ``(graph read receipt id, skill digest)``."""
    agent_observe: Callable[[str], str]
    """``instance_id`` -> the skill digest the agent runs with."""
    run: Callable[[str, SkillRef], RunResult]


def _discover(ports: FlowPorts, view: WorkItemView) -> tuple[str, SkillRef]:
    try:
        generation, skills = ports.discover(view)
    except ProducerUnavailable:
        raise ProvisionFlowRefused("PRODUCER_UNAVAILABLE") from None
    if not skills:
        raise ProvisionFlowRefused("SKILL_NOT_FOUND")
    return generation, skills[0]


def _decide(ports: FlowPorts, view: WorkItemView, skill: SkillRef) -> Decision:
    try:
        decision = ports.distribution(view, skill)
    except ProducerUnavailable:
        raise ProvisionFlowRefused("PRODUCER_UNAVAILABLE") from None
    if decision.tenant != view.tenant:
        raise ProvisionFlowRefused("CROSS_TENANT_RECEIPT")
    if (decision.skill_id, decision.schema_digest) != (
        skill.skill_id,
        skill.schema_digest,
    ):
        raise ProvisionFlowRefused("SCHEMA_INCOMPATIBLE")
    if not set(skill.tools) <= set(decision.approved_tools):
        raise ProvisionFlowRefused("TOOL_NOT_APPROVED")
    return decision


def _verify_identity(ports: FlowPorts, skill: SkillRef, result: ProvisionResult) -> str:
    instance_id = result.instance_ids[0] if result.instance_ids else ""
    graph_receipt, graph_digest = ports.graph_read(skill.skill_id)
    observed = ports.agent_observe(instance_id) if instance_id else ""
    if not instance_id or not (graph_digest == observed == skill.digest):
        raise ProvisionFlowRefused(
            "SCHEMA_INCOMPATIBLE",
            instance_ids=result.instance_ids,
            allocation_receipt_digest=result.allocation_receipt_digest,
        )
    return graph_receipt


def provision_for_work_item(
    view: WorkItemView,
    *,
    ports: FlowPorts,
    provision: Callable[[ProvisionRequest], ProvisionResult],
    request: ProvisionRequest,
    journal: AuditChain,
    clock: Callable[[], int],
) -> IntegratedProvisionReceipt:
    """Discover, read the distribution decision, provision, run and verify identity."""
    if request.tenant != view.tenant:
        raise ProvisionFlowRefused("CROSS_TENANT_RECEIPT")
    generation, skill = _discover(ports, view)
    decision = _decide(ports, view, skill)
    result = provision(request)
    ingested_receipt = _verify_identity(ports, skill, result)
    instance_id = result.instance_ids[0]
    run = ports.run(instance_id, skill)
    if run.skill_digest != skill.digest:
        raise ProvisionFlowRefused(
            "SCHEMA_INCOMPATIBLE", instance_ids=result.instance_ids
        )
    executed_receipt, executed_digest = ports.graph_read(skill.skill_id)
    if executed_digest != skill.digest:
        raise ProvisionFlowRefused(
            "SCHEMA_INCOMPATIBLE", instance_ids=result.instance_ids
        )
    run_id = view.work_item_id
    admission = (
        "admission:"
        + hashlib.sha256(result.allocation_receipt_digest.encode()).hexdigest()[:32]
    )
    receipt = IntegratedProvisionReceipt(
        tenant=view.tenant,
        run_id=run_id,
        skill_digest=skill.digest,
        skill_id=skill.skill_id,
        skill_version=skill.version,
        discovery_generation=generation,
        distribution_decision_id=decision.decision_id,
        admission_receipt_id=admission,
        instance_id=instance_id,
        result_digest=run.result_digest,
        phases=tuple(
            PhaseBinding(
                phase=name, tenant=view.tenant, run_id=run_id, skill_digest=skill.digest
            )
            for name in ("discovery", "distribution", "provision", "execution")
        ),
    )
    evidence = {
        "catalog_generation": generation,
        "graph_read_receipts": [ingested_receipt, executed_receipt],
        "distribution_decision": decision.decision_id,
        "instance": instance_id,
        "model_trace": run.model_trace_id,
        "result_receipt": run.result_receipt_id,
        "skill_digest": skill.digest,
    }
    journal.append(
        kind=EVIDENCE_KIND,
        subject_ref="flow:" + hashlib.sha256(run_id.encode()).hexdigest()[:32],
        payload_digest=canonical_digest(evidence),
        observed_at=clock(),
    )
    return receipt
