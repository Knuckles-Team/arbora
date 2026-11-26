"""Meeting summary orchestration with provider admission (ARBORA-OPS-R884)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import meetings
from arbora.api.registry import Effect
from tests.api._meeting_fakes import (
    PARAMS,
    Admission,
    Summarizer,
    install_engine,
    meeting_context,
)

ALLOWED = {"allowed": True, "policy_revision": "p1", "admission_id": "a1"}
_OP = meetings.operations()[0]
_FULL = {
    **PARAMS,
    "max_tokens": 10,
    "max_spans": 2,
    "max_segment_bytes": 100,
    "max_attempts": 2,
}


@pytest.mark.spec("ARBORA-OPS-R884")
async def test_summary_create_admits_context_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_engine(monkeypatch, meetings)
    summarizer = Summarizer(timeouts=1)
    context = meeting_context(Admission(ALLOWED), summarizer=summarizer)
    result = await meetings.handle_summary_create(context, _FULL, _OP)
    assert calls == ["MeetingSummaryContext", "MeetingSummaryCommit"]
    assert result["value"]["attempts"] == 2 and summarizer.calls == 2
    assert result["value"]["summary_id"].startswith("sum-")
    assert _OP.effect is Effect.WRITE
    assert meetings.NATIVE_IDEMPOTENCY["meetings.summary.create"].startswith("medulla:")

    # One invocation needs one receipt: exhausted attempts commit nothing.
    calls.clear()
    stuck = meeting_context(Admission(ALLOWED), summarizer=Summarizer(timeouts=9))
    with pytest.raises(OperationRefused) as exhausted:
        await meetings.handle_summary_create(stuck, _FULL, _OP)
    assert exhausted.value.code == "PROVIDER_UNAVAILABLE"
    assert calls == ["MeetingSummaryContext"]

    # A denied provider refuses before any medulla call.
    calls.clear()
    for code in ("CAPTURE_DENIED", "EGRESS_DENIED", "PROVIDER_UNAVAILABLE"):
        denied = meeting_context(Admission({"allowed": False, "code": code}))
        with pytest.raises(OperationRefused) as refused:
            await meetings.handle_summary_create(denied, _FULL, _OP)
        assert refused.value.code == code
    assert calls == []
