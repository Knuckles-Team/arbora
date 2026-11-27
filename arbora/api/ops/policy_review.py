"""Review receipt for a reviewed repository policy (ARBORA-OPS-R071).

``policy.review.approve`` turns one human review of a repository policy file
into the receipt the engine checks before it activates a policy
(MEDULLA-ORCH-VOCAB-R012.8). Arbora binds the repository, path, commit, blob
digest, candidate-set digest, invariant generation, the revision the active
policy had, the author and the reviewing actor into a SHA-256 receipt digest,
and records it with the engine ``DecisionLog`` learn write. The engine checks
the binding against its indexed candidates and its invariants in force and
answers ``POLICY_REVIEW_MISMATCH`` when any value is stale; that is read here as
``PLAN_STALE`` and no receipt is returned. Arbora holds no policy, grammar or
activation authority, and a rejected review records nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_validator

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops import engine_calls as _engine
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import Confirm, OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

SCOPE = "agent:decision-write"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_STALE = frozenset({"POLICY_REVIEW_MISMATCH"})


def _digest(value: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise ValueError("a digest is 64 lowercase hex characters")
    return value


class PolicyReviewParams(_common.Params):
    repository: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=512)
    commit: str = Field(min_length=7, max_length=64)
    blob_digest: str
    candidate_set_digest: str
    invariant_generation: str = Field(min_length=1, max_length=128)
    expected_policy_revision: str | None = Field(default=None, max_length=128)
    author: str = Field(min_length=1, max_length=256)
    approved: bool = True

    _blob = field_validator("blob_digest")(_digest)
    _candidates = field_validator("candidate_set_digest")(_digest)


class PolicyReviewReceipt(_common.Params):
    repository: str
    path: str
    commit: str
    blob_digest: str
    candidate_set_digest: str
    invariant_generation: str
    expected_policy_revision: str | None
    reviewer: str
    receipt_digest: str


class PolicyReviewResult(_common.Params):
    receipt: PolicyReviewReceipt | None = None
    recorded: dict[str, Any] = Field(default_factory=dict)


def receipt_digest(binding: Mapping[str, Any], author: str, reviewer: str) -> str:
    """SHA-256 over the canonical JSON of the eight bound fields."""
    body = {**binding, "author": author, "reviewer": reviewer}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def handle_policy_review_approve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Record one approved review with the engine and return its receipt."""
    if not params.get("approved", True):
        # A rejected review is not recorded and yields no receipt.
        return {"receipt": None, "recorded": {}}
    try:
        require_models(("LearningWriteRecordPolicyReview",))
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    reviewer = str(context.caller.principal)
    if reviewer == params["author"]:
        raise OperationRefused("POLICY_DENIED", {"reason": "reviewer_is_author"})
    binding = {
        "repository": params["repository"],
        "path": params["path"],
        "commit": params["commit"],
        "blob_digest": params["blob_digest"],
        "candidate_set_digest": params["candidate_set_digest"],
        "invariant_generation": params["invariant_generation"],
        "expected_policy_revision": params.get("expected_policy_revision"),
    }
    graph = await _engine.caller_dispatch_graph(context)
    digest = receipt_digest(binding, str(params["author"]), reviewer)
    try:
        recorded = await engine_send(
            context,
            "DecisionLog",
            {
                "op": {
                    "op": "learn",
                    "tenant_id": str(context.caller.tenant),
                    "write": {
                        "write": "record_policy_review",
                        "graph": graph,
                        "review": {
                            "author": params["author"],
                            "reviewer": reviewer,
                            "binding": {**binding, "review_receipt_digest": digest},
                        },
                    },
                }
            },
            idempotent=True,
        )
    except EngineRefusal as refused:
        if refused.code in _STALE:
            raise OperationRefused(
                "PLAN_STALE", {"engine_code": refused.code}
            ) from refused
        raise
    receipt = PolicyReviewReceipt(**binding, reviewer=reviewer, receipt_digest=digest)
    return {
        "receipt": receipt.model_dump(mode="json"),
        "recorded": dict(recorded) if isinstance(recorded, Mapping) else {},
    }


NATIVE_IDEMPOTENCY: dict[str, str] = {"policy.review.approve": "medulla:DecisionLog"}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            op_id="policy.review.approve",
            summary="Approve a reviewed repository policy file and return its receipt",
            examples=("approve this policy review",),
            params=PolicyReviewParams,
            result=PolicyReviewResult,
            handler="arbora.api.ops.policy_review.handle_policy_review_approve",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "PolicyReviewParams",
    "PolicyReviewReceipt",
    "PolicyReviewResult",
    "handle_policy_review_approve",
    "operations",
    "receipt_digest",
    "specs",
]
