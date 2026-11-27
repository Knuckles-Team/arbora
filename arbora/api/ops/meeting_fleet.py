"""The production ``meeting_summarizer`` port: one governed ``fleet.call`` (ARBORA-OPS-R065.3).

``meetings.summary.create`` hands the engine's bounded summary context to the provider the
caller chose through this port. The provider is a fleet catalog tool that declares the
``meeting_summary`` capability, and the hand-off is the same governed ``fleet.call`` every
other connector call uses, so there is one provider-dispatch mechanism and no second client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.fleet_tool_call import call_fleet_tool, refused_result


class FleetMeetingSummarizer:
    """Run one summary attempt through the provider's catalog tool."""

    async def run(
        self,
        *,
        request: Any,
        tenant: str,
        context: Mapping[str, Any],
        admission: Mapping[str, str],
        attempt: int,
    ) -> Mapping[str, Any]:
        gateway = request.services.get("fleet_gateway")
        if gateway is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "fleet gateway is not bound"}
            )
        items = await gateway.catalog.with_capability(request.caller, "meeting_summary")
        item = next((i for i in items if i.id == admission["provider"]), None)
        if item is None or not item.server:
            raise OperationRefused("PROVIDER_UNAVAILABLE", {"attempt": attempt})
        answer = await call_fleet_tool(
            request,
            item.server,
            item.name,
            {
                "tenant": tenant,
                "model": admission["model"],
                "admission_digest": admission["admission_digest"],
                "context": dict(context),
                "attempt": attempt,
            },
        )
        if refused_result(answer) or not _is_run(answer):
            raise OperationRefused("PROVIDER_UNAVAILABLE", {"attempt": attempt})
        return answer


def _is_run(answer: Any) -> bool:
    return (
        isinstance(answer, Mapping)
        and isinstance(answer.get("claims"), list)
        and isinstance(answer.get("receipt"), Mapping)
    )
