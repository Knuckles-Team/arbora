"""Summarize a finalized meeting (ARBORA-OPS-R895).

``meeting.summarize`` first requires the transcript's current finalization seal, read
through the ``meeting_final`` port (the audio-transcriber ``meeting_get_final`` tool), and
checks the caller's receipt against it with ``scion_sdk.meeting.require_finalized``. Only
then does it admit the provider and run the one shared summary pipeline of
:mod:`arbora.api.ops.meetings`. A local binding never executes hosted.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import bound_service, build_write_op
from arbora.api.ops.meetings import (
    MeetingSummaryResult,
    MeetingSummaryRunParams,
    admit_provider,
    run_summary,
)
from arbora.api.registry import OpSpec, Verb
from arbora.pin_lag.unavailable import PinLagUnavailable


class MeetingSummarizeParams(MeetingSummaryRunParams):
    #: The receipt the caller holds; it must be the engine's current seal.
    finalization_digest: str = Field(min_length=1, max_length=256)
    execution_class: str = Field(default="hosted", pattern=r"^(hosted|local)$")


def _require_finalized(held: str, current: str) -> None:
    """The SDK check that a summary names the engine's current seal (pin-lag guarded)."""
    try:
        from scion_sdk.meeting.client import require_finalized
    except ImportError as error:
        raise PinLagUnavailable("scion_sdk.meeting") from error
    try:
        require_finalized(
            SimpleNamespace(receipt=SimpleNamespace(finalization_digest=held)),
            SimpleNamespace(finalization_digest=current),
        )
    except Exception as error:
        code = getattr(error, "code", None) or "SUMMARY_BEFORE_FINALIZE"
        raise OperationRefused("POLICY_DENIED", {"reason": str(code)}) from error


async def handle_meeting_summarize(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Finalization first, then admission, then one provider run and one commit."""
    from arbora.pin_lag.medulla import refusal

    finals = bound_service(
        context, "meeting_final", reason="meeting finalization reader is not composed"
    )
    current = await finals.get_final(
        tenant=context.caller.tenant, meeting_id=params["meeting_id"]
    )
    try:
        _require_finalized(
            params["finalization_digest"], current["finalization_digest"]
        )
    except PinLagUnavailable as error:
        raise refusal(error) from error
    admission = await admit_provider(context, params)
    return await run_summary(context, params, admission)


NATIVE_IDEMPOTENCY: dict[str, str] = {
    "meeting.summarize": "medulla:MeetingSummaryCommit",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            verb=Verb.ACT,
            op_id="meeting.summarize",
            summary="Summarize a finalized meeting under an admitted provider",
            examples=("summarize the finalized meeting",),
            params=MeetingSummarizeParams,
            result=MeetingSummaryResult,
            handler="arbora.api.ops.meeting.handle_meeting_summarize",
            scope="kg:write",
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "MeetingSummarizeParams",
    "handle_meeting_summarize",
    "operations",
    "specs",
]
