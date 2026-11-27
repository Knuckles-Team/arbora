"""Decide operations: service-executed commit (ARBORA-OPS-R021.1).

The commit path runs only under the service executor so no caller can
materialize an agent decision using only a human-scoped token. The handler
sends the engine ``DecisionCommit`` method through the composition root's
client; there is no runner seam. When the pinned policy requires approval the
request carries the recommendation approval and the engine checks it, so the
engine's ``APPROVAL_MISSING`` and ``APPROVAL_STALE`` pass through unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import bound_service
from arbora.api.ops._decide import committed_record, record_digest
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)
from arbora.core.claims import params_digest


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecideCommitParams(_Params):
    request: dict[str, Any] = Field(min_length=0)


class DecideCommitResult(_Params):
    value: dict[str, Any]
    provenance: dict[str, str]


async def handle_decide_commit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Commit one decision record through the engine as the service executor."""
    if getattr(context, "client", None) is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine client is not bound"})
    request = dict(params["request"])
    answer = await engine_send(
        context, "DecisionCommit", {"request": request}, idempotent=True
    )
    value = dict(answer) if isinstance(answer, Mapping) else {"result": answer}
    value["record_digest"] = params_digest(request.get("record", request))
    return {
        "value": value,
        "provenance": {
            "op": op.id,
            "tenant": str(context.caller.tenant),
            "principal": str(getattr(context.caller, "principal", "")),
        },
    }


class RecommendationApproveParams(_Params):
    record_id: str = Field(min_length=1, max_length=256)
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecommendationApproveResult(_Params):
    value: dict[str, Any]


APPROVE_SCOPE = "approvals:decide"


def _approval_published() -> bool:
    """Whether the pinned engine contract declares the approval receipt (R019.100.2)."""
    from medulla.generated import models

    return hasattr(models, "RecommendationApproval")


async def handle_recommendation_approve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Issue the approval receipt of one committed record, bound to its digest.

    The scope is checked first and a refusal makes no engine call; the reviewer is the
    verified caller; the receipt is committed by the service-executed ``decide.commit``.
    """
    if APPROVE_SCOPE not in (getattr(context.caller, "effective_scopes", None) or ()):
        raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": [APPROVE_SCOPE]})
    record = await committed_record(context, params["record_id"])
    if record_digest(record) != params["record_digest"]:
        raise OperationRefused("PLAN_MISMATCH")
    clock = context.services.get("now_ms")
    approval = {
        "record_id": params["record_id"],
        "record_digest": params["record_digest"],
        "reviewer_principal": str(context.caller.principal),
        "approved_at_ms": int(clock()) if clock else 0,
        "approval_revision": int(record.get("approval_revision", 0)) + 1,
    }
    runner = bound_service(
        context, "decide_runner", reason="decide runner is not composed"
    )
    committed = await runner.commit(
        {"record_id": params["record_id"], "approval": approval},
        tenant=context.caller.tenant,
    )
    return {"value": {"approval": approval, "commit": dict(committed)}}


def operations() -> tuple[OpSpec, ...]:
    approve = (
        (
            OpSpec(
                id="decide.recommendation.approve",
                verb=Verb.ACT,
                summary="Approve one committed recommendation by its record digest",
                examples=("approve this recommendation",),
                params=RecommendationApproveParams,
                result=RecommendationApproveResult,
                binding=Composite(
                    handler="arbora.api.ops.decide.handle_recommendation_approve"
                ),
                scopes=frozenset({APPROVE_SCOPE}),
                effect=Effect.WRITE,
                idempotency=Idempotency.KEY_REQUIRED,
                audit=AuditClass.IDENTITY_CHAIN,
                principals=PrincipalRule.HUMAN,
            ),
        )
        if _approval_published()
        else ()
    )
    return (*approve, *_commit_operations())


def _commit_operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="decide.commit",
            verb=Verb.ACT,
            summary="Commit an agent decision through the service executor",
            examples=("commit this routing decision",),
            params=DecideCommitParams,
            result=DecideCommitResult,
            binding=Composite(handler="arbora.api.ops.decide.handle_decide_commit"),
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

#: The approval commits through the decision log, which replays by the receipt (R863).
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "decide.recommendation.approve": "medulla:DecisionLog",
}

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "RecommendationApproveParams",
    "handle_recommendation_approve",
    "DecideCommitParams",
    "DecideCommitResult",
    "handle_decide_commit",
    "operations",
    "specs",
]
