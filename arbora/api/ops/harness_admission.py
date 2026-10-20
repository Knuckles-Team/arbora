"""Harness substrate admission: one closed admit/refuse boundary.

``ARBORA-OPS-R068``: admission, refusal, and provenance for the served
harness component and improvement library surface, in the A2A
tool-subset admission style (``arbora.a2a.admission``): a typed request,
a closed refusal-reason enum, and a provenanced decision. Every refusal
fails closed and is never advisory, never partial. The decision names
the missing scope or the uncomposed reader; it carries no harness
content, no ranking, and no engine data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import VerifiedCaller

__all__ = [
    "HARNESS_SURFACE_AUTHORITY",
    "HarnessAdmissionDecision",
    "HarnessAdmissionRequest",
    "HarnessSurfaceRefusalReason",
    "admit_harness_surface",
    "admission_request_for",
    "refusal_for",
]

#: Closed refusal reasons for the served harness surface.
HarnessSurfaceRefusalReason = Literal[
    "HARNESS_SURFACE_UNKNOWN",
    "HARNESS_SCOPE_REQUIRED",
    "HARNESS_READER_UNAVAILABLE",
]


class _HarnessAdmissionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HarnessAdmissionRequest(_HarnessAdmissionModel):
    """The verified authority for one harness surface request.

    Every field comes from the verified caller, never from request
    parameters; an admitted decision confers no authority of its own.
    """

    op_id: str = Field(min_length=1, max_length=128)
    tenant: str = Field(min_length=1, max_length=256)
    principal: str = Field(min_length=1, max_length=256)
    principal_kind: Literal["human", "service"]
    effective_scopes: frozenset[str]


class HarnessAdmissionDecision(_HarnessAdmissionModel):
    """The door's admit/refuse result. Never advisory; never partial."""

    admitted: bool
    refusal_reason: HarnessSurfaceRefusalReason | None = None
    missing_scopes: tuple[str, ...] = ()
    provenance: str = Field(min_length=1, max_length=256)


@dataclass(frozen=True)
class _HarnessSurface:
    """One served harness operation's authority requirements."""

    scopes: frozenset[str]
    service: str


#: The served harness surface: operation id to exact scopes and the
#: composed reader or runner service that must exist to serve it.
HARNESS_SURFACE_AUTHORITY: Mapping[str, _HarnessSurface] = {
    "harness.components.list": _HarnessSurface(
        frozenset({"kg:read"}), "harness_component_reader"
    ),
    "harness.components.get": _HarnessSurface(
        frozenset({"kg:read"}), "harness_component_reader"
    ),
    "harness.lessons.retrieve": _HarnessSurface(
        frozenset({"kg:read"}), "harness_lesson_retriever"
    ),
    "harness.evolution.report": _HarnessSurface(
        frozenset({"kg:read"}), "harness_evolution_report_reader"
    ),
    "harness.evolution.adopt": _HarnessSurface(
        frozenset({"decide:commit"}), "harness_adoption_runner"
    ),
}


def admit_harness_surface(
    request: HarnessAdmissionRequest, *, services: Mapping[str, Any]
) -> HarnessAdmissionDecision:
    """Admit the request, or fail closed with a typed, provenanced refusal."""
    surface = HARNESS_SURFACE_AUTHORITY.get(request.op_id)
    if surface is None:
        return HarnessAdmissionDecision(
            admitted=False,
            refusal_reason="HARNESS_SURFACE_UNKNOWN",
            provenance=f"harness.admission:{request.op_id}:unknown-surface",
        )
    missing = sorted(surface.scopes - request.effective_scopes)
    if missing:
        return HarnessAdmissionDecision(
            admitted=False,
            refusal_reason="HARNESS_SCOPE_REQUIRED",
            missing_scopes=tuple(missing),
            provenance=(
                f"harness.admission:{request.op_id}:missing-{','.join(missing)}"
            ),
        )
    if services.get(surface.service) is None:
        return HarnessAdmissionDecision(
            admitted=False,
            refusal_reason="HARNESS_READER_UNAVAILABLE",
            provenance=(
                f"harness.admission:{request.op_id}:{surface.service}-uncomposed"
            ),
        )
    return HarnessAdmissionDecision(
        admitted=True,
        provenance=f"harness.admission:{request.op_id}:admitted",
    )


def admission_request_for(
    caller: VerifiedCaller, op_id: str
) -> HarnessAdmissionRequest:
    """One verified caller's closed authority for one harness operation.

    The invoke pipeline's authenticate boundary guarantees the caller's
    principal kind is one of the closed vocabulary before any handler
    runs; a direct call without it refuses rather than widens the type.
    """
    kind = caller.principal_kind
    if kind not in ("human", "service"):
        raise OperationRefused(
            "UNAVAILABLE",
            {
                "reason": "HARNESS_SURFACE_UNKNOWN",
                "provenance": f"harness.admission:{op_id}:principal-kind-unverified",
            },
        )
    return HarnessAdmissionRequest(
        op_id=op_id,
        tenant=caller.tenant,
        principal=caller.principal,
        principal_kind=cast("Literal['human', 'service']", kind),
        effective_scopes=frozenset(caller.effective_scopes),
    )


def refusal_for(decision: HarnessAdmissionDecision) -> OperationRefused:
    """The closed invoke envelope for one refused harness admission."""
    if decision.refusal_reason == "HARNESS_SCOPE_REQUIRED":
        return OperationRefused(
            "SCOPE_REQUIRED",
            {
                "missing_scopes": list(decision.missing_scopes),
                "provenance": decision.provenance,
            },
        )
    return OperationRefused(
        "UNAVAILABLE",
        {
            "reason": decision.refusal_reason,
            "provenance": decision.provenance,
        },
    )
