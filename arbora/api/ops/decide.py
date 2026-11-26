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
from arbora.api.invoke.plan import params_digest
from arbora.api.ops._engine import engine_send
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


def operations() -> tuple[OpSpec, ...]:
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

__all__ = [
    "DecideCommitParams",
    "DecideCommitResult",
    "handle_decide_commit",
    "operations",
    "specs",
]
