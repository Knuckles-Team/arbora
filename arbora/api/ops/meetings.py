"""Meeting summary orchestration with provider admission (ARBORA-OPS-R065, R884).

``meetings.summary.create`` admits the provider and model for the caller, then asks the
engine for the bounded summary context (``MeetingSummaryContext``), hands that context to
the harness through the ``meeting_summarizer`` port and commits the run's receipt with
``MeetingSummaryCommit``. A denied or disabled provider refuses before any engine call.
Arbora keeps the attempt count of one invocation and nothing else: the engine holds the
summary, the harness holds the run, and one invocation commits one receipt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_write_op
from arbora.api.ops._engine import engine_send
from arbora.api.registry import OpSpec, Verb

#: The refusals of a provider admission, in the order they are checked.
ADMISSION_CODES = frozenset(
    {"CAPTURE_DENIED", "EGRESS_DENIED", "TENANT_DENIED", "PROVIDER_UNAVAILABLE"}
)
MAX_ATTEMPTS = 3


class MeetingSummaryCreateParams(Params):
    meeting_id: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    max_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    max_spans: int = Field(default=64, ge=1, le=10_000)
    max_segment_bytes: int = Field(default=4096, ge=1, le=1_000_000)
    max_attempts: int = Field(default=2, ge=1, le=MAX_ATTEMPTS)


class MeetingSummaryResult(Params):
    value: dict[str, Any]


def _digest(value: Mapping[str, Any]) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def refuse_admission(admission: Mapping[str, Any]) -> None:
    """Refuse a denied or disabled provider with its own code; the engine is untouched."""
    if admission.get("allowed") is True:
        return
    code = str(admission.get("code") or "PROVIDER_UNAVAILABLE")
    if code not in ADMISSION_CODES:
        code = "PROVIDER_UNAVAILABLE"
    if code == "TENANT_DENIED":
        raise EngineRefusal(code)
    raise OperationRefused(code)


async def admit_provider(context: Any, params: Mapping[str, Any]) -> dict[str, str]:
    """The admission ref ``{provider, model, policy_revision, admission_id, admission_digest}``."""
    port = bound_service(
        context,
        "meeting_provider_admission",
        reason="provider admission is not composed",
    )
    admission = await port.admit(
        tenant=context.caller.tenant,
        principal=context.caller.principal,
        provider=params["provider"],
        model=params["model"],
    )
    refuse_admission(admission)
    ref = {
        "provider": params["provider"],
        "model": params["model"],
        "policy_revision": str(admission["policy_revision"]),
        "admission_id": str(admission["admission_id"]),
    }
    return {**ref, "admission_digest": _digest(ref)}


async def run_summary(
    context: Any, params: Mapping[str, Any], admission: Mapping[str, str]
) -> dict[str, Any]:
    """Context, harness hand-off and one commit under an already admitted provider."""
    transcripts = bound_service(
        context, "meeting_transcript", reason="meeting transcript is not composed"
    )
    summarizer = bound_service(
        context, "meeting_summarizer", reason="meeting summarizer is not composed"
    )
    transcript = await transcripts.read(
        tenant=context.caller.tenant, meeting_id=params["meeting_id"]
    )
    engine_admission = {
        key: admission[key]
        for key in ("provider", "model", "policy_revision", "admission_digest")
    }
    summary_context = await engine_send(
        context,
        "MeetingSummaryContext",
        {
            "request": {
                "admission": engine_admission,
                "meeting_id": params["meeting_id"],
                "policy": {
                    "max_segment_bytes": params["max_segment_bytes"],
                    "max_spans": params["max_spans"],
                    "max_tokens": params["max_tokens"],
                },
                "segments": list(transcript["segments"]),
                "transcript_digest": transcript["transcript_digest"],
            }
        },
    )
    run: Mapping[str, Any] | None = None
    attempts = 0
    while run is None and attempts < params["max_attempts"]:
        attempts += 1
        try:
            run = await summarizer.run(
                tenant=context.caller.tenant,
                context=summary_context,
                admission=dict(admission),
                attempt=attempts,
            )
        except TimeoutError:
            run = None
    if run is None:
        raise OperationRefused("PROVIDER_UNAVAILABLE", {"attempts": attempts})
    summary_id = (
        "sum-"
        + _digest(
            {
                "meeting": params["meeting_id"],
                "transcript": transcript["transcript_digest"],
                "admission": admission["admission_digest"],
            }
        )[:32]
    )
    committed = await engine_send(
        context,
        "MeetingSummaryCommit",
        {
            "request": {
                "claims": list(run["claims"]),
                "meeting_id": params["meeting_id"],
                "receipt": dict(run["receipt"]),
                "summary_id": summary_id,
            }
        },
        idempotent=True,
    )
    return {
        "value": {
            "summary_id": summary_id,
            "attempts": attempts,
            "commit": committed if isinstance(committed, Mapping) else {},
        }
    }


async def handle_summary_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Admit first, then summarize: a refusal makes no engine call."""
    admission = await admit_provider(context, params)
    return await run_summary(context, params, admission)


#: The commit is replayed by the engine under the invocation's idempotency key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "meetings.summary.create": "medulla:MeetingSummaryCommit",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            verb=Verb.ACT,
            op_id="meetings.summary.create",
            summary="Summarize a meeting transcript under an admitted provider",
            examples=("summarize this meeting",),
            params=MeetingSummaryCreateParams,
            result=MeetingSummaryResult,
            handler="arbora.api.ops.meetings.handle_summary_create",
            scope="kg:write",
        ),
    )


specs = operations

__all__ = [
    "ADMISSION_CODES",
    "NATIVE_IDEMPOTENCY",
    "MeetingSummaryCreateParams",
    "MeetingSummaryResult",
    "admit_provider",
    "handle_summary_create",
    "operations",
    "refuse_admission",
    "run_summary",
    "specs",
]
