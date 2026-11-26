"""Policy review returns a bound receipt and refuses self approval (ARBORA-OPS-R885)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import policy
from tests.api._ops_support import service_context

_OP = next(o for o in policy.operations() if o.id == "policy.review")
_PARAMS = {
    "repository": "r",
    "candidate_set_digest": "a" * 64,
    "source_blob_digest": "b" * 64,
    "invariant_generation": 4,
    "author": "principal:author",
}


@pytest.mark.spec("ARBORA-OPS-R885")
async def test_policy_review_returns_bound_receipt_and_refuses_self_approval() -> None:
    reviewer = service_context(principal="principal:reviewer")
    result = await policy.handle_policy_review(reviewer, _PARAMS, _OP)
    assert result["value"] == {
        "repository": "r",
        "candidate_set_digest": "a" * 64,
        "source_blob_digest": "b" * 64,
        "invariant_generation": 4,
        "reviewer_principal": "principal:reviewer",
    }
    author = service_context(principal="principal:author")
    with pytest.raises(OperationRefused) as refused:
        await policy.handle_policy_review(author, _PARAMS, _OP)
    assert refused.value.code == "POLICY_DENIED"
    assert "policy.review" in policy.NATIVE_IDEMPOTENCY
