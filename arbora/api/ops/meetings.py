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
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import (
    FrozenState,
    Params,
    bound_service,
    build_read_op,
    build_write_op,
)
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.ops.pin_guards import refusal
from arbora.api.registry import Confirm, OpSpec, Verb
from arbora.pin_lag.medulla import PinLagUnavailable, require_symbol

#: The refusals of a provider admission, in the order they are checked.
ADMISSION_CODES = frozenset(
    {"CAPTURE_DENIED", "EGRESS_DENIED", "TENANT_DENIED", "PROVIDER_UNAVAILABLE"}
)
MAX_ATTEMPTS = 3
SUMMARY_GET_SENDER = ("medulla.generated.ingestion", "send_meeting_summary_get")
TRANSCRIPT_READ_SENDER = ("medulla.generated.ingestion", "send_meeting_transcript_read")


class TranscriptEvent(FrozenState):
    """One transcript revision on the live stream (ARBORA-OPS-R065.1).

    This is the engine's ``TranscriptEventRecord``: the engine keeps digests and never the
    text. ``op`` is ``append``, ``correct``, ``delete_segment`` or ``finalize``, and
    ``finalization_digest`` is set only on the seal, and ``segment_ids`` (the revision's ordered
    segments) only on a sealed revision.
    """

    schema_version: int = Field(default=1, ge=1)
    meeting_id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(ge=0)
    revision: int = Field(ge=0)
    op: Literal["append", "correct", "delete_segment", "finalize"] | None = None
    text_digest: str = Field(min_length=1, max_length=256)
    finalization_digest: str | None = Field(default=None, min_length=1, max_length=256)
    segment_ids: list[str] = Field(default_factory=list, max_length=100_000)


class ProviderChoice(FrozenState):
    """One summary provider, with the explicit class that decides its egress."""

    provider_id: str = Field(min_length=1, max_length=128)
    execution_class: Literal["local", "hosted"]
    model_id: str = Field(min_length=1, max_length=128)


class SummaryRequest(FrozenState):
    schema_version: int = Field(default=1, ge=1)
    meeting_id: str = Field(min_length=1, max_length=256)
    transcript_revision: int = Field(ge=0)
    provider_id: str = Field(min_length=1, max_length=128)
    context_revision: int = Field(ge=0)


class SummaryClaim(FrozenState):
    claim_id: str = Field(min_length=1, max_length=256)
    text: str = Field(max_length=65_536)
    citations: list[dict[str, Any]]


class SummaryRecord(FrozenState):
    """The engine's ``MeetingSummaryRecord``: the stored summary and its receipt."""

    summary_id: str = Field(min_length=1, max_length=256)
    stale: bool
    transcript_digest: str
    context_digest: str
    receipt_id: str
    receipt_digest: str
    output_digest: str
    provider: str
    model: str
    policy_revision: str
    claims: list[SummaryClaim]


class SummaryResult(FrozenState):
    """The engine's ``MeetingSummaryGetAnswer``: ``null`` for an unknown or unseen summary."""

    summary: SummaryRecord | None


class MeetingSummaryRunParams(Params):
    """The internal pipeline parameters shared with ``meeting.summarize`` (R895)."""

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
        request=context,
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
        request=context, tenant=context.caller.tenant, meeting_id=params["meeting_id"]
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
                request=context,
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


class MeetingSummaryCreateParams(Params):
    """The summary request of ARBORA-OPS-R065.1: the exact finalized revision."""

    schema_version: int = Field(default=1, ge=1)
    meeting_id: str = Field(min_length=1, max_length=256)
    transcript_revision: int = Field(ge=0)
    provider_id: str = Field(min_length=1, max_length=128)
    context_revision: int = Field(ge=0)


class MeetingSummaryGetParams(Params):
    meeting_id: str = Field(min_length=1, max_length=256)
    summary_id: str | None = Field(default=None, min_length=1, max_length=256)


class ProvidersListParams(Params):
    pass


class TranscriptSubscribeParams(Params):
    meeting_id: str = Field(min_length=1, max_length=256)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


def stream_path(meeting_id: str) -> str:
    return f"/ws/meetings/{meeting_id}/transcript"


async def provider_choices(context: Any) -> list[ProviderChoice]:
    """Fleet catalog entries that declare ``meeting_summary`` with an explicit class."""
    gateway = bound_service(
        context, "fleet_gateway", reason="the fleet catalog is not composed"
    )
    items = await gateway.catalog.with_capability(context.caller, "meeting_summary")
    choices = []
    for item in items:
        note = item.annotations or {}
        if note.get("execution_class") in {"local", "hosted"} and note.get("model_id"):
            choices.append(
                ProviderChoice(
                    provider_id=item.id,
                    execution_class=note["execution_class"],
                    model_id=str(note["model_id"]),
                )
            )
    return choices


async def handle_providers_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    choices = await provider_choices(context)
    return {"value": {"providers": [c.model_dump(mode="json") for c in choices]}}


def egress_refusal(refused: OperationRefused) -> OperationRefused:
    """A hosted summary without capture or egress approval is ``POLICY_DENIED``."""
    if refused.code in {"CAPTURE_DENIED", "EGRESS_DENIED"}:
        return OperationRefused("POLICY_DENIED", {"reason": refused.code})
    return refused


async def require_exact_receipt(
    context: Any, request: MeetingSummaryCreateParams
) -> None:
    """The finalized transcript receipt must be the exact one the caller names."""
    finals = bound_service(
        context, "meeting_final", reason="meeting finalization reader is not composed"
    )
    current = await finals.get_final(
        request=context, tenant=context.caller.tenant, meeting_id=request.meeting_id
    )
    if not current.get("finalization_digest"):
        raise OperationRefused("POLICY_DENIED", {"reason": "SUMMARY_BEFORE_FINALIZE"})
    if (
        current.get("transcript_revision") != request.transcript_revision
        or current.get("context_revision") != request.context_revision
    ):
        raise OperationRefused("STALE_REVISION", {"meeting_id": request.meeting_id})


async def chosen_provider(context: Any, provider_id: str) -> ProviderChoice:
    """The catalog entry the caller named; an unknown one is ``UNAVAILABLE``."""
    chosen = [
        c for c in await provider_choices(context) if c.provider_id == provider_id
    ]
    if not chosen:
        raise OperationRefused("UNAVAILABLE", {"reason": "no such summary provider"})
    return chosen[0]


async def admitted(context: Any, run: Mapping[str, Any]) -> dict[str, str]:
    try:
        return await admit_provider(context, run)
    except OperationRefused as refused:
        raise egress_refusal(refused) from refused


def provenance_of(choice: ProviderChoice, request: MeetingSummaryCreateParams) -> dict:
    return {
        "provider": choice.provider_id,
        "model": choice.model_id,
        "context_revision": str(request.context_revision),
        "transcript_revision": str(request.transcript_revision),
    }


async def handle_summary_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Provider, exact receipt, then admission, then one run: a refusal calls no provider."""
    request = MeetingSummaryCreateParams.model_validate(dict(params))
    choice = await chosen_provider(context, request.provider_id)
    await require_exact_receipt(context, request)
    run = MeetingSummaryRunParams(
        meeting_id=request.meeting_id,
        provider=choice.provider_id,
        model=choice.model_id,
    ).model_dump()
    result = await run_summary(context, run, await admitted(context, run))
    result["value"]["provenance"] = provenance_of(choice, request)
    return result


async def handle_summary_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The stored summary, read by the engine (``ingestion.send_meeting_summary_get``)."""
    request = MeetingSummaryGetParams.model_validate(dict(params))
    try:
        require_symbol(*SUMMARY_GET_SENDER)
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    answer = await engine_send(
        context,
        "MeetingSummaryGet",
        {"request": request.model_dump(mode="json", exclude_none=True)},
    )
    try:
        stored = SummaryResult.model_validate(answer)
    except ValidationError:
        raise unavailable("the engine summary is missing or invalid") from None
    return {"value": stored.model_dump(mode="json")}


def transcript_events(
    answer: Any, request: TranscriptSubscribeParams
) -> tuple[list[TranscriptEvent], int]:
    """The engine's events as models, in sequence order, and its last sequence."""
    if not isinstance(answer, Mapping) or not isinstance(answer.get("events"), list):
        raise unavailable("the engine transcript is missing or invalid")
    last = answer.get("last_sequence")
    if type(last) is not int or request.cursor > last:
        raise OperationRefused("STALE_CURSOR", {"meeting_id": request.meeting_id})
    try:
        events = [
            TranscriptEvent.model_validate({**e, "meeting_id": request.meeting_id})
            for e in answer["events"]
        ]
    except (ValidationError, TypeError):
        raise unavailable("the engine transcript event is invalid") from None
    return sorted(events, key=lambda e: e.sequence), last


def transcript_page(answer: Any, request: TranscriptSubscribeParams) -> dict[str, Any]:
    """Order by sequence; withhold past a gap; ``finalized`` only when contiguous."""
    events, last = transcript_events(answer, request)
    delivered, missing = _contiguous(events, request.cursor)
    complete = missing is None and bool(answer.get("finalization_digest"))
    delivered = [e for e in delivered if complete or e.finalization_digest is None]
    cursor = max((e.sequence for e in delivered), default=request.cursor)
    return {
        "cursor": cursor,
        "stream_path": stream_path(request.meeting_id),
        "events": [e.model_dump(mode="json") for e in delivered],
        "gap": None if missing is None else {"after": cursor, "missing": missing},
        "finalized": complete and cursor == last,
    }


def _contiguous(
    events: list[TranscriptEvent], cursor: int
) -> tuple[list[TranscriptEvent], int | None]:
    """The prefix whose sequences follow ``cursor`` with no hole, and the first hole."""
    kept: list[TranscriptEvent] = []
    expected = cursor + 1
    for event in events:
        if event.sequence == expected:
            kept.append(event)
            expected += 1
        else:
            return kept, expected
    return kept, None


async def handle_transcript_subscribe(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One page behind the cursor; the route repeats it (``ingestion.send_meeting_transcript_read``)."""
    request = TranscriptSubscribeParams.model_validate(dict(params))
    try:
        require_symbol(*TRANSCRIPT_READ_SENDER)
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    answer = await engine_send(
        context,
        "MeetingTranscriptRead",
        {
            "request": {
                "meeting_id": request.meeting_id,
                "after_seq": request.cursor,
                "limit": request.limit,
            }
        },
    )
    return {"value": transcript_page(answer, request)}


#: The commit is replayed by the engine under the invocation's idempotency key.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "meetings.summary.create": "medulla:MeetingSummaryCommit",
    }
)


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            verb=Verb.ACT,
            op_id="meetings.summary.create",
            summary="Summarize a finalized meeting transcript under a chosen provider",
            examples=("summarize this meeting",),
            params=MeetingSummaryCreateParams,
            result=MeetingSummaryResult,
            handler="arbora.api.ops.meetings.handle_summary_create",
            scope="kg:write",
            confirm=Confirm.PLAN,
        ),
        build_read_op(
            op_id="meetings.summary.get",
            summary="Read the stored summary of a meeting",
            examples=("show the summary of this meeting",),
            params=MeetingSummaryGetParams,
            result=MeetingSummaryResult,
            handler="arbora.api.ops.meetings.handle_summary_get",
            scope="source:read",
        ),
        build_read_op(
            op_id="meetings.providers.list",
            summary="List the providers that can summarize a meeting",
            examples=("which providers can summarize a meeting",),
            params=ProvidersListParams,
            result=MeetingSummaryResult,
            handler="arbora.api.ops.meetings.handle_providers_list",
            scope="source:read",
        ),
        build_read_op(
            op_id="meetings.transcript.subscribe",
            summary="Read ordered transcript events behind a cursor",
            examples=("follow this meeting transcript",),
            params=TranscriptSubscribeParams,
            result=MeetingSummaryResult,
            handler="arbora.api.ops.meetings.handle_transcript_subscribe",
            scope="source:read",
        ),
    )


specs = operations

__all__ = [
    "ADMISSION_CODES",
    "ProviderChoice",
    "SummaryRequest",
    "SummaryResult",
    "TranscriptEvent",
    "NATIVE_IDEMPOTENCY",
    "MeetingSummaryCreateParams",
    "MeetingSummaryGetParams",
    "MeetingSummaryResult",
    "MeetingSummaryRunParams",
    "SummaryClaim",
    "SummaryRecord",
    "TranscriptSubscribeParams",
    "handle_providers_list",
    "handle_summary_get",
    "handle_transcript_subscribe",
    "admit_provider",
    "handle_summary_create",
    "operations",
    "refuse_admission",
    "run_summary",
    "specs",
]
