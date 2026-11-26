"""A summary needs the final receipt and an admitted provider (ARBORA-OPS-R895)."""

from __future__ import annotations

import sys
import types

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import meeting, meetings
from tests.api._meeting_fakes import (
    PARAMS,
    Admission,
    Final,
    Summarizer,
    install_engine,
    meeting_context,
)

ALLOWED = {"allowed": True, "policy_revision": "p1", "admission_id": "a1"}
_OP = meeting.operations()[0]
_PARAMS = {
    **PARAMS,
    "finalization_digest": "f1",
    "max_tokens": 10,
    "max_spans": 2,
    "max_segment_bytes": 100,
    "max_attempts": 1,
    "execution_class": "hosted",
}


def _install_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    client = types.ModuleType("scion_sdk.meeting.client")

    def require_finalized(request: object, current: object) -> None:
        if request.receipt.finalization_digest != current.finalization_digest:  # type: ignore[attr-defined]
            error = RuntimeError("SUMMARY_BEFORE_FINALIZE")
            error.code = "SUMMARY_BEFORE_FINALIZE"  # type: ignore[attr-defined]
            raise error

    client.require_finalized = require_finalized  # type: ignore[attr-defined]
    package = types.ModuleType("scion_sdk.meeting")
    monkeypatch.setitem(sys.modules, "scion_sdk.meeting", package)
    monkeypatch.setitem(sys.modules, "scion_sdk.meeting.client", client)


@pytest.mark.spec("ARBORA-OPS-R895")
async def test_summary_needs_final_receipt_and_admitted_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk(monkeypatch)
    calls = install_engine(monkeypatch, meetings)
    summarizer = Summarizer()

    stale = meeting_context(
        Admission(ALLOWED), summarizer=summarizer, meeting_final=Final("other")
    )
    with pytest.raises(OperationRefused) as early:
        await meeting.handle_meeting_summarize(stale, _PARAMS, _OP)
    assert early.value.code == "POLICY_DENIED"
    assert early.value.details["reason"] == "SUMMARY_BEFORE_FINALIZE"

    for answer, code in (
        ({"allowed": False, "code": "CAPTURE_DENIED"}, "CAPTURE_DENIED"),
        ({"allowed": False, "code": "EGRESS_DENIED"}, "EGRESS_DENIED"),
        ({"allowed": False}, "PROVIDER_UNAVAILABLE"),
    ):
        context = meeting_context(
            Admission(answer), summarizer=summarizer, meeting_final=Final("f1")
        )
        with pytest.raises(OperationRefused) as refused:
            await meeting.handle_meeting_summarize(context, _PARAMS, _OP)
        assert refused.value.code == code
    tenant = meeting_context(
        Admission({"allowed": False, "code": "TENANT_DENIED"}),
        summarizer=summarizer,
        meeting_final=Final("f1"),
    )
    with pytest.raises(EngineRefusal) as foreign:
        await meeting.handle_meeting_summarize(tenant, _PARAMS, _OP)
    assert foreign.value.code == "TENANT_DENIED"
    assert summarizer.calls == 0 and calls == []

    ok = meeting_context(
        Admission(ALLOWED), summarizer=summarizer, meeting_final=Final("f1")
    )
    result = await meeting.handle_meeting_summarize(ok, _PARAMS, _OP)
    assert summarizer.calls == 1 and result["value"]["attempts"] == 1
    assert calls == ["MeetingSummaryContext", "MeetingSummaryCommit"]
