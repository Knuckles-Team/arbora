"""The production meeting ports: engine reads and a fleet-and-policy admission (ARBORA-OPS-R065.3).

``meeting_transcript`` and ``meeting_final`` read the finalized transcript through the engine
method ``MeetingTranscriptRead`` (MEDULLA w18; a pinned release refuses ``UNAVAILABLE`` naming
it). ``meeting_provider_admission`` admits a provider from the fleet catalog and the engine's
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
from arbora.api.ops._engine import engine_send, unavailable
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

READ_METHOD = "MeetingTranscriptRead"
GRANT_METHOD = "CheckResourceAccess"
PAGE = 1000
MAX_PAGES = 64


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _read_page(request: Any, meeting_id: str, after: int, limit: int) -> Any:
    try:
        require_methods(READ_METHOD)
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    answer = await engine_send(
        request,
        READ_METHOD,
        {"meeting_id": meeting_id, "after_seq": after, "limit": limit},
    )
    if not isinstance(answer, Mapping) or not isinstance(answer.get("events"), list):
        raise unavailable("the engine transcript is missing or invalid")
    return answer


def _same_tenant(request: Any, tenant: str) -> None:
    """A port serves the verified caller's tenant only."""
    if tenant != request.caller.tenant:
        raise OperationRefused("TENANT_DENIED")


def _segments(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The settled text of each segment: its highest correction, in sequence order."""
    latest: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("state") == "partial":
            continue
        key = str(event["segment_id"])
        held = latest.get(key)
        if held is None or (event["correction_revision"], event["sequence"]) > (
            held["correction_revision"],
            held["sequence"],
        ):
            latest[key] = event
    ordered = sorted(latest.values(), key=lambda e: e["sequence"])
    return [
        {
            "segment_id": str(e["segment_id"]),
            "text": str(e.get("text", "")),
            "start_ms": e.get("start_ms", 0),
            "end_ms": e.get("end_ms", 0),
            "source_id": e.get("source_id", ""),
        }
        for e in ordered
    ]


class EngineMeetingTranscript:
    """``meeting_transcript``: the settled segments of a finalized meeting."""

    async def read(
        self, *, request: Any, tenant: str, meeting_id: str
    ) -> dict[str, Any]:
        _same_tenant(request, tenant)
        events: list[Mapping[str, Any]] = []
        after = 0
        for _ in range(MAX_PAGES):
            answer = await _read_page(request, meeting_id, after, PAGE)
            page = list(answer["events"])
            events.extend(page)
            last = answer.get("last_sequence")
            after = max((int(e["sequence"]) for e in page), default=after)
            if not page or not isinstance(last, int) or after >= last:
                break
        else:
            raise unavailable("the engine transcript is too long to summarize")
        if not answer.get("finalization_digest"):
            raise OperationRefused(
                "POLICY_DENIED", {"reason": "SUMMARY_BEFORE_FINALIZE"}
            )
        segments = _segments(events)
        return {
            "segments": segments,
            "transcript_digest": _digest(
                {"segments": segments, "seal": answer["finalization_digest"]}
            ),
        }


class EngineMeetingFinal:
    """``meeting_final``: the engine's seal of a meeting transcript.

    ``transcript_revision`` and ``context_revision`` are the engine's when it names them;
    otherwise the transcript revision is its last sequence and the context revision is
    the seal itself, which binds both.
    """

    async def get_final(
        self, *, request: Any, tenant: str, meeting_id: str
    ) -> dict[str, Any]:
        _same_tenant(request, tenant)
        answer = await _read_page(request, meeting_id, 0, 1)
        seal = str(answer.get("finalization_digest") or "")
        last = answer.get("last_sequence")
        return {
            "finalization_digest": seal,
            "transcript_revision": answer.get("transcript_revision", last),
            "context_revision": answer.get("context_revision", seal),
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
