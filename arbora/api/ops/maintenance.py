"""Paper-shortfall maintenance (ARBORA-OPS-R072).

``maintenance.paper_shortfall.run`` applies one engine maintenance order for a paper
shortfall under an owner authorization. It refuses before any effect: a missing
authorization is ``SCOPE_REQUIRED``, a cost above the ceiling is ``POLICY_DENIED``
with reason ``cost_over_ceiling``. The engine enforces what may be purged (nothing
leased, authoritative or the last source copy) and its refusals pass through.
PIN-LAG: the engine publishes a throughput measurement (``MeasureIngestThroughput``)
but no maintenance-apply method yet, so the operation is not registered until
``PaperMaintenanceApply`` exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

SCOPE = "agent:decision-write"
#: The maintenance method the engine must publish (requested under MEDULLA FOR ARBORA w15).
METHOD = "PaperMaintenanceApply"


class PaperShortfallRunParams(_common.Params):
    owner_authorization_ref: str | None = Field(default=None, max_length=256)
    expected_revision: int = Field(ge=0)
    #: Integer cents, 0 to 1,000,000.
    cost_ceiling: int = Field(ge=0, le=1_000_000)
    admission_verdict_ref: str = Field(min_length=1, max_length=256)
    #: The cost the admission verdict named, in the same cents.
    cost: int = Field(default=0, ge=0)


class PaperShortfallRunResult(_common.Params):
    value: dict[str, Any]


def method_published() -> bool:
    try:
        require_methods(METHOD)
    except PinLagUnavailable:
        return False
    return True


async def handle_paper_shortfall_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Run the pre-effect checks, then the one engine call; engine refusals pass through."""
    if not params.get("owner_authorization_ref"):
        raise OperationRefused("SCOPE_REQUIRED", {"missing": "owner_authorization_ref"})
    if params["cost"] > params["cost_ceiling"]:
        raise OperationRefused("POLICY_DENIED", {"reason": "cost_over_ceiling"})
    try:
        require_methods(METHOD)
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    receipt = await engine_send(
        context,
        METHOD,
        {
            "owner_authorization_ref": params["owner_authorization_ref"],
            "expected_revision": params["expected_revision"],
            "admission_verdict_ref": params["admission_verdict_ref"],
        },
        idempotent=True,
    )
    return {
        "value": dict(receipt) if isinstance(receipt, Mapping) else {"receipt": receipt}
    }


def operations() -> tuple[OpSpec, ...]:
    if not method_published():
        return ()
    return (
        OpSpec(
            id="maintenance.paper_shortfall.run",
            verb=Verb.ACT,
            summary="Run the paper-shortfall maintenance order under an owner authorization",
            examples=("run the paper shortfall maintenance",),
            params=PaperShortfallRunParams,
            result=PaperShortfallRunResult,
            binding=Composite(
                handler="arbora.api.ops.maintenance.handle_paper_shortfall_run"
            ),
            scopes=frozenset({SCOPE}),
            executor_scopes=frozenset({SCOPE}),
            effect=Effect.DESTRUCTIVE,
            executor=Executor.SERVICE,
            principals=PrincipalRule.SERVICE_ONLY,
            subject=SubjectRef(source=SubjectSource.CALLER_TENANT),
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.IDENTITY_CHAIN,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "METHOD",
    "PaperShortfallRunParams",
    "handle_paper_shortfall_run",
    "method_published",
    "operations",
    "specs",
]
