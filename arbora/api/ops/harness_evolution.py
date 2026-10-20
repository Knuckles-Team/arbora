"""Served harness evolution report and adoption (ARBORA-OPS-R068.3, .4).

``harness.evolution.report`` reads one task's contrast and the engine
contrast-decision record (EG-DECISION-ENGINE-R803) through the
caller-bound engine path under ``kg:read``; a caller denied by policy
never reaches the reader. ``harness.evolution.adopt`` commits the
adopted harness beside the decide-commit pattern of ARBORA-OPS-R021.1:
service executor only, the exact service scope, the caller read check on
the subject, and the decide commit's idempotency and audit class.
Arbora stores no harness content and computes no ranking.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops.harness_admission import (
    admission_request_for,
    admit_harness_surface,
    refusal_for,
)
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Executor,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)

__all__ = [
    "AbstainReason",
    "AdoptionReceipt",
    "AdoptionRunner",
    "EvolutionReport",
    "EvolutionReportReader",
    "EvolutionReportRead",
    "HarnessContrastDelta",
    "HarnessEvolutionAdoptParams",
    "HarnessEvolutionAdoptResult",
    "HarnessEvolutionReportParams",
    "UNAVAILABLE_EVOLUTION_REPORT",
    "handle_harness_evolution_adopt",
    "handle_harness_evolution_report",
    "operations",
    "project_evolution_report",
    "specs",
]

#: The closed engine abstention reasons (EG-DECISION-ENGINE-R803).
AbstainReason = Literal["no_improvement", "trace_mismatch", "insufficient_evidence"]


class HarnessContrastDelta(BaseModel):
    """One candidate's measured behavioral contrast, engine-owned.

    Mirrors the engine vocabulary contrast record (EG-ORCH-VOCAB-R802.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_version_ref: str = Field(min_length=1, max_length=512)
    retry_delta: float
    tool_selection_delta: float
    eval_score_delta: float
    cost_delta: float


class EvolutionReport(BaseModel):
    """One task's contrast and contrast-decision record, engine-owned.

    Carries the decision id, the contrast digest and the adopted version
    reference; every other field is engine evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1, max_length=512)
    task_ref: str = Field(min_length=1, max_length=512)
    task_class: str = Field(min_length=1, max_length=256)
    conditions_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contrast_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_trace_ids: tuple[str, ...] = Field(default=(), max_length=8)
    deltas: tuple[HarnessContrastDelta, ...] = Field(default=(), max_length=8)
    adopted_version_ref: str | None = Field(default=None, max_length=512)
    abstain_reason: AbstainReason | None = None


class EvolutionReportRead(Params):
    """One task's evolution report, or nothing.

    A denied or unknown task projects to the same unavailable report;
    the caller receives neither the contrast nor the record.
    """

    availability: Literal["available", "unavailable"]
    report: EvolutionReport | None = None


UNAVAILABLE_EVOLUTION_REPORT = EvolutionReportRead(availability="unavailable")


class EvolutionReportReader(Protocol):
    """The host-composed read port for one task's evolution report."""

    async def report(self, *, context: Any, task_ref: str) -> EvolutionReportRead: ...


class HarnessEvolutionReportParams(Params):
    """``harness.evolution.report``: one task's evolution report."""

    task_ref: str = Field(min_length=1, max_length=512)


class EvolutionReportResult(Params):
    value: dict[str, Any]


class HarnessEvolutionAdoptParams(Params):
    """``harness.evolution.adopt``: one engine contrast-decision adoption.

    ``decision_ref`` names the engine decision record; the adopted
    harness it composes (EG-ORCH-VOCAB-R802) carries the agent, task
    class, component version references and contrast reference.
    """

    decision_ref: str = Field(min_length=1, max_length=512)
    adopted_version_ref: str = Field(min_length=1, max_length=512)
    agent_ref: str = Field(min_length=1, max_length=512)
    task_class: str = Field(min_length=1, max_length=256)
    component_version_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    contrast_ref: str = Field(min_length=1, max_length=512)


class AdoptionReceipt(BaseModel):
    """The one stored adoption, exactly as committed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_ref: str = Field(min_length=1, max_length=512)
    adopted_version_ref: str = Field(min_length=1, max_length=512)
    agent_ref: str = Field(min_length=1, max_length=512)
    task_class: str = Field(min_length=1, max_length=256)
    component_version_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    contrast_ref: str = Field(min_length=1, max_length=512)


class HarnessEvolutionAdoptResult(Params):
    value: dict[str, Any]


@runtime_checkable
class AdoptionRunner(Protocol):
    """The service-bound seam that commits one adopted harness."""

    async def adopt(
        self, request: Mapping[str, Any], *, tenant: str
    ) -> Mapping[str, Any]: ...


def project_evolution_report(raw: object) -> EvolutionReport | None:
    """Validate one reader answer; anything not a valid report is nothing."""
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, Mapping):
        return None
    try:
        return EvolutionReport.model_validate(raw)
    except ValidationError:
        return None


async def handle_harness_evolution_report(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one task's report, or an unavailable read with no record."""
    decision = admit_harness_surface(
        admission_request_for(context.caller, op.id), services=context.services
    )
    if not decision.admitted:
        raise refusal_for(decision)
    request = HarnessEvolutionReportParams.model_validate(dict(params))
    reader: EvolutionReportReader = bound_service(
        context,
        "harness_evolution_report_reader",
        reason="harness evolution report reader is not composed",
    )
    read = await reader.report(context=context, task_ref=request.task_ref)
    report = project_evolution_report(read.report)
    if read.availability != "available" or report is None:
        return {"value": UNAVAILABLE_EVOLUTION_REPORT.model_dump(mode="json")}
    return {
        "value": EvolutionReportRead(
            availability="available", report=report
        ).model_dump(mode="json")
    }


async def handle_harness_evolution_adopt(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Commit one adoption through the service-bound runner only."""
    decision = admit_harness_surface(
        admission_request_for(context.caller, op.id), services=context.services
    )
    if not decision.admitted:
        raise refusal_for(decision)
    request = HarnessEvolutionAdoptParams.model_validate(dict(params))
    runner: AdoptionRunner = bound_service(
        context,
        "harness_adoption_runner",
        reason="harness adoption runner is not composed",
    )
    result = await runner.adopt(
        request.model_dump(mode="json"), tenant=context.caller.tenant
    )
    try:
        receipt = AdoptionReceipt.model_validate(result)
    except ValidationError:
        raise OperationRefused(
            "UNAVAILABLE",
            {
                "reason": "adoption receipt malformed",
                "provenance": "harness.evolution.adopt:receipt-unprojectable",
            },
        ) from None
    return {"value": receipt.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.ASK,
            op_id="harness.evolution.report",
            summary="Read one task's contrast and adoption decision record",
            examples=("what did the harness evolution decide for this task",),
            params=HarnessEvolutionReportParams,
            result=EvolutionReportResult,
            handler="arbora.api.ops.harness_evolution.handle_harness_evolution_report",
            scope="kg:read",
        ),
        OpSpec(
            id="harness.evolution.adopt",
            verb=Verb.ACT,
            summary="Adopt a harness version from an engine contrast decision",
            examples=("adopt this harness version for the task",),
            params=HarnessEvolutionAdoptParams,
            result=HarnessEvolutionAdoptResult,
            binding=Composite(
                handler="arbora.api.ops.harness_evolution.handle_harness_evolution_adopt"
            ),
            scopes=frozenset({"decide:commit"}),
            executor_scopes=frozenset({"decide:commit"}),
            effect=Effect.WRITE,
            executor=Executor.SERVICE,
            principals=PrincipalRule.SERVICE_ONLY,
            subject=SubjectRef(source=SubjectSource.CALLER_TENANT),
            audit=AuditClass.EVENT,
        ),
    )


specs = operations
