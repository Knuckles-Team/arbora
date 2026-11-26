"""The dispatch request carries the harness decision reference (ARBORA-A2A-R018.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.contracts import SignedAgentDispatchRequest
from tests.a2a._admission_support import DIGEST


def _request(**extra: object) -> SignedAgentDispatchRequest:
    return SignedAgentDispatchRequest(
        job_id="job", work_item_id="w", session_ref="s", agent_name="a", **extra
    )


@pytest.mark.spec("ARBORA-A2A-R018.1")
def test_enqueue_passes_harness_config_reference() -> None:
    bare = _request()
    assert (bare.allowed_tool_subset, bare.decision_record_ref) == ((), None)
    carried = _request(
        allowed_tool_subset=("kg.query",),
        decision_record_ref="record:harness:1",
        harness_config_digest=DIGEST,
    )
    assert carried.allowed_tool_subset == ("kg.query",)
    assert carried.model_dump()["harness_config_digest"] == DIGEST
    with pytest.raises(ValidationError):
        _request(harness_config_digest="NOT-HEX")
    with pytest.raises(ValidationError):
        _request(allowed_tool_subset=tuple(f"t{i}" for i in range(65)))
