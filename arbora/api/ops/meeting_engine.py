"""The production meeting ports: engine reads and a fleet-and-policy admission (ARBORA-OPS-R065.3).

``meeting_transcript`` and ``meeting_final`` read the finalized transcript through the engine
method ``MeetingTranscriptRead`` through the generated sender
``ingestion.send_meeting_transcript_read`` (MEDULLA-MEDIA-VOCAB-R001.101.7; a pinned release
without it refuses ``UNAVAILABLE`` naming it). The engine holds digests, so the segment text
comes from the fleet tool that declares ``meeting_transcript``. ``meeting_provider_admission`` admits a provider from the fleet catalog and the engine's
resource grant ledger (``CheckResourceAccess``): the catalog says whether the provider exists
for the caller and what class it runs in, and the engine says whether the tenant's principal may
capture the meeting and, for a hosted provider, send it out. Arbora keeps no policy table and no
state; every port is built per call from the request it is given.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.ops.pin_guards import refusal
from arbora.fleet.fleet_tool_call import call_fleet_tool, refused_result
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods, require_symbol

READ_METHOD = "MeetingTranscriptRead"
READ_SENDER = ("medulla.generated.ingestion", "send_meeting_transcript_read")
GRANT_METHOD = "CheckResourceAccess"
#: The engine's read ceiling is 500 events a page; a larger limit refuses.
PAGE = 500
MAX_PAGES = 64
#: The fleet capability of the tool that serves a meeting's segment text.
TEXT_CAPABILITY = "meeting_transcript"


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _read_page(request: Any, meeting_id: str, after: int, limit: int) -> Any:
    try:
        require_symbol(*READ_SENDER)
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    answer = await engine_send(
        request,
        READ_METHOD,
        {"request": {"meeting_id": meeting_id, "after_seq": after, "limit": limit}},
    )
    return _checked(answer)


def _checked(answer: Any) -> Any:
    """The engine's page when it has the events and the last sequence, else ``UNAVAILABLE``."""
    events = answer.get("events") if isinstance(answer, Mapping) else None
    if not isinstance(events, list) or type(answer.get("last_sequence")) is not int:
        raise unavailable("the engine transcript is missing or invalid")
    return answer


def _same_tenant(request: Any, tenant: str) -> None:
    """A port serves the verified caller's tenant only."""
    if tenant != request.caller.tenant:
        raise OperationRefused("TENANT_DENIED")


async def _text_tool(request: Any) -> Any:
    """The catalog entry of the fleet tool that serves segment text."""
    gateway = request.services.get("fleet_gateway")
    if gateway is None:
        raise unavailable("the fleet gateway is not bound")
    items = await gateway.catalog.with_capability(request.caller, TEXT_CAPABILITY)
    item = next(iter(sorted(items, key=lambda i: i.id)), None)
    if item is None or not item.server:
        raise unavailable("no fleet tool serves the meeting segment text")
    return item


def _segments_of(answer: Any) -> list[dict[str, Any]]:
    rows = answer.get("segments") if isinstance(answer, Mapping) else None
    if refused_result(answer) or not isinstance(rows, list):
        raise unavailable("the meeting segment text is missing or invalid")
    return [
        {"segment_id": str(row["segment_id"]), "text": str(row["text"])}
        for row in rows
        if isinstance(row, Mapping) and "segment_id" in row and "text" in row
    ]


async def _segment_text(
    request: Any, meeting_id: str, seal: str
) -> list[dict[str, Any]]:
    """The segment text of the sealed meeting, from the fleet tool that holds it.

    The engine keeps digests of the transcript and never its text, so the text comes from
    the connector that captured it, through one governed ``fleet.call``.
    """
    item = await _text_tool(request)
    answer = await call_fleet_tool(
        request,
        item.server,
        item.name,
        {"meeting_id": meeting_id, "finalization_digest": seal},
    )
    return _segments_of(answer)


async def _sealed_events(request: Any, meeting_id: str) -> tuple[list[Any], Any, Any]:
    """Every event in order, the seal and the segment digests the last page answered."""
    events: list[Any] = []
    after = 0
    for _ in range(MAX_PAGES):
        answer = await _read_page(request, meeting_id, after, PAGE)
        page = list(answer["events"])
        events.extend(page)
        after = max((int(e["sequence"]) for e in page), default=after)
        if not page or after >= answer["last_sequence"]:
            return (
                events,
                answer.get("finalization_digest"),
                answer.get("segment_digests"),
            )
    raise unavailable("the engine transcript is too long to summarize")


def _text_digest_ok(text: str, expected: Any) -> bool:
    held = str(expected).removeprefix("sha256:")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() == held


def _sealed_ids(events: list[Any]) -> list[str]:
    """The ordered segment ids of the sealed revision, empty when no event carries them."""
    sealed = (e.get("segment_ids") for e in reversed(events))
    return list(next((ids for ids in sealed if ids), []))


def _covers(ids: list[str], rows: dict[str, Any], held: Mapping[str, Any]) -> bool:
    return bool(ids) and set(ids) == set(rows) == set(held)


def _verified_segments(
    rows: list[dict[str, Any]], events: list[Any], digests: Any
) -> list[dict[str, Any]]:
    """The text segments the engine's seal covers, in the seal's order, each hash-checked.

    The engine returns ``segment_digests`` only while its stored segments still reproduce
    the seal, and ``segment_ids`` only on the sealed revision; a source whose segment is
    missing, extra or does not hash to the engine's digest is refused.
    """
    ids = _sealed_ids(events)
    by_id = {row["segment_id"]: row for row in rows}
    held = digests if isinstance(digests, Mapping) else {}
    if not _covers(ids, by_id, held):
        raise unavailable(
            "the segment text does not match the engine's sealed segments"
        )
    if not all(_text_digest_ok(by_id[i]["text"], held[i]) for i in ids):
        raise unavailable("a segment text does not hash to the engine's digest")
    return [by_id[i] for i in ids]


def _events_digest(events: list[Any], seal: str) -> str:
    rows = [[e["sequence"], e["revision"], e["text_digest"]] for e in events]
    return _digest({"events": rows, "seal": seal})


class EngineMeetingTranscript:
    """``meeting_transcript``: the sealed segments of a finalized meeting."""

    async def read(
        self, *, request: Any, tenant: str, meeting_id: str
    ) -> dict[str, Any]:
        _same_tenant(request, tenant)
        events, seal, digests = await _sealed_events(request, meeting_id)
        if not seal:
            raise OperationRefused(
                "POLICY_DENIED", {"reason": "SUMMARY_BEFORE_FINALIZE"}
            )
        rows = await _segment_text(request, meeting_id, str(seal))
        return {
            "segments": _verified_segments(rows, events, digests),
            "transcript_digest": _events_digest(events, str(seal)),
        }


class EngineMeetingFinal:
    """``meeting_final``: the engine's seal of a meeting transcript.

    The engine answers the seal and both revisions on every read, so nothing is derived.
    """

    async def get_final(
        self, *, request: Any, tenant: str, meeting_id: str
    ) -> dict[str, Any]:
        _same_tenant(request, tenant)
        answer = await _read_page(request, meeting_id, 0, 1)
        return {
            "finalization_digest": str(answer.get("finalization_digest") or ""),
            "transcript_revision": answer.get("transcript_revision"),
            "context_revision": answer.get("context_revision"),
        }


class FleetPolicyAdmission:
    """``meeting_provider_admission``: catalog existence, then capture and egress grants."""

    async def admit(
        self,
        *,
        request: Any,
        tenant: str,
        principal: str,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        if tenant != request.caller.tenant:
            return {"allowed": False, "code": "TENANT_DENIED"}
        entry = await self._entry(request, provider)
        if entry is None or str(entry.get("model_id")) != model:
            return {"allowed": False, "code": "PROVIDER_UNAVAILABLE"}
        hosted = entry.get("execution_class") == "hosted"
        steps = (("capture", "CAPTURE_DENIED"),) + (
            (("egress", "EGRESS_DENIED"),) if hosted else ()
        )
        revisions: list[int] = []
        for operation, code in steps:
            decision = await self._decision(request, principal, provider, operation)
            if decision.get("allow") is not True:
                return {"allowed": False, "code": code}
            revisions.append(int(decision.get("revocation_revision", 0)))
        policy_revision = str(max(revisions))
        return {
            "allowed": True,
            "policy_revision": policy_revision,
            "admission_id": "adm-"
            + _digest([tenant, principal, provider, model, policy_revision])[:32],
        }

    @staticmethod
    async def _entry(request: Any, provider: str) -> Mapping[str, Any] | None:
        gateway = request.services.get("fleet_gateway")
        if gateway is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the fleet catalog is not composed"}
            )
        items = await gateway.catalog.with_capability(request.caller, "meeting_summary")
        for item in items:
            if item.id == provider:
                return item.annotations or {}
        return None

    @staticmethod
    async def _decision(
        request: Any, principal: str, provider: str, operation: str
    ) -> Mapping[str, Any]:
        try:
            require_methods(GRANT_METHOD)
        except PinLagUnavailable as missing:
            raise refusal(missing) from None
        answer = await engine_send(
            request,
            GRANT_METHOD,
            {
                "agent_id": principal,
                "resource_ref": f"meeting-summary:{provider}",
                "protocol": "mcp",
                "operation": operation,
            },
        )
        if not isinstance(answer, Mapping) or "allow" not in answer:
            raise unavailable("the engine answered no access decision")
        return answer


__all__ = ["EngineMeetingFinal", "EngineMeetingTranscript", "FleetPolicyAdmission"]
