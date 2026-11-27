"""Order-proposal confirmation receipt (ARBORA-OPS-R070).

``order.proposal.confirm`` reads the engine's order admission receipt, compares the
admission digest the caller confirmed and the checked actor, and issues a
confirmation receipt binding them. It routes no order and calls no provider; order
routing stays in the emerald-exchange tools. The receipt read is the engine's
``FinanceOrderReceiptGet {request: {proposal_id}}``, answering
``{receipt_id, receipt_digest, receipt}``; ``receipt_digest`` is the admission digest.
An absent, unpromoted or foreign proposal all refuse ``ORDER_RECEIPT_NOT_FOUND``, so
no caller can tell them apart. PIN-LAG: the pinned release lacks the method, so the
operation is not registered (``operations()`` is empty) until the re-pin carries it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops.engine_calls import engine_send
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
#: What the engine publishes on ``main`` (MEDULLA w16, MEDULLA-FINANCE-PRIMITIVES-R015.1.4).
RECEIPT_METHODS = ("FinanceOrderReceiptGet",)
_DIGEST = r"^[0-9a-f]{64}$"


class OrderProposalConfirmParams(_common.Params):
    proposal_id: str = Field(min_length=1, max_length=256)
    admission_digest: str = Field(pattern=_DIGEST)
    approver_set_digest: str = Field(pattern=_DIGEST)


class OrderProposalConfirmResult(_common.Params):
    value: dict[str, Any]


def receipt_published() -> bool:
    """Whether the pinned engine publishes the admission receipt read."""
    try:
        require_methods(*RECEIPT_METHODS)
    except PinLagUnavailable:
        return False
    return True


def confirmation_digest(binding: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(binding), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def handle_order_proposal_confirm(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Issue the confirmation receipt when the engine's admission digest still matches."""
    try:
        require_methods(*RECEIPT_METHODS)
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    admission = await engine_send(
        context, RECEIPT_METHODS[0], {"request": {"proposal_id": params["proposal_id"]}}
    )
    if not isinstance(admission, Mapping) or not admission.get("receipt_id"):
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    if admission.get("receipt_digest") != params["admission_digest"]:
        raise OperationRefused("PLAN_STALE", {"reason": "admission_digest_mismatch"})
    binding = {
        "proposal_id": params["proposal_id"],
        "receipt_id": str(admission["receipt_id"]),
        "admission_digest": params["admission_digest"],
        "approver_set_digest": params["approver_set_digest"],
        "confirmed_by": str(context.caller.principal),
    }
    return {"value": {**binding, "receipt_digest": confirmation_digest(binding)}}


def operations() -> tuple[OpSpec, ...]:
    if not receipt_published():
        return ()
    return (
        OpSpec(
            id="order.proposal.confirm",
            verb=Verb.ACT,
            summary="Confirm an admitted order proposal and return its receipt",
            examples=("confirm this admitted order proposal",),
            params=OrderProposalConfirmParams,
            result=OrderProposalConfirmResult,
            binding=Composite(
                handler="arbora.api.ops.finance_orders.handle_order_proposal_confirm"
            ),
            scopes=frozenset({SCOPE}),
            executor_scopes=frozenset({SCOPE}),
            effect=Effect.WRITE,
            executor=Executor.SERVICE,
            principals=PrincipalRule.SERVICE_ONLY,
            subject=SubjectRef(source=SubjectSource.CALLER_TENANT),
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.IDENTITY_CHAIN,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

#: The receipt is a pure function of the engine's immutable admission receipt, so a
#: replay of the same key reads the same receipt and issues the same confirmation.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "order.proposal.confirm": "medulla:FinanceOrderReceiptGet",
}

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "OrderProposalConfirmParams",
    "confirmation_digest",
    "handle_order_proposal_confirm",
    "operations",
    "receipt_published",
    "specs",
]
