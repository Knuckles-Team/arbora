"""A delivery assignment starts on a reserved development lane (ARBORA-OPS-R896.12).

For each pending assignment of the engine's delivery, arbora reserves a development lane
through the engine lane methods, then starts one meristem run with the delivery context
(``DeliveryAssignmentContext`` of ``MERISTEM-SCM-R012.1``). The lane quota of the engine
caps parallel assignments at ``development.delivery.max_parallel`` (4); one extra slot stays
for a person's input, so new work never blocks a person. A start without a free slot queues
and refuses nothing. A run that loses its lane lease ends its assignment through
``DeliveryReport`` and the engine recovers it. Arbora keeps no assignment state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

MAX_PARALLEL_DEFAULT = 4
PERSON_SLOTS = 1


class LanePort(Protocol):
    """The engine lane methods (``EngineNativeDevelopmentLaneTransport``)."""

    def status(self, request: Mapping[str, Any]) -> dict[str, Any]: ...

    def reserve(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class RunStarter(Protocol):
    """Starts one meristem run for a delivery assignment on a reserved lane."""

    async def start(
        self, context: Mapping[str, Any], hold: Mapping[str, Any]
    ) -> str: ...


def assignment_context(
    delivery: Mapping[str, Any], assignment: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``DeliveryAssignmentContext`` fields, every one a received engine value."""
    return {
        "delivery_id": str(delivery["id"]),
        "scope_rows": list(delivery.get("scope_rows") or ()),
        "proposal_ref": assignment.get("proposal_ref"),
        "inputs": list(assignment.get("inputs") or ()),
        "recovery_note": assignment.get("recovery_note"),
        "round_verdicts": dict(assignment.get("round_verdicts") or {}),
        "background_limit_s": assignment.get("background_limit_s"),
    }


async def start_assignment(
    delivery: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    tenant: str,
    lanes: LanePort | None,
    runner: RunStarter | None,
    max_parallel: int = MAX_PARALLEL_DEFAULT,
    person_input: bool = False,
    now_ms: Callable[[], int],
) -> dict[str, Any]:
    """Reserve a lane and start the run, or queue; never refuses for want of a slot."""
    if lanes is None or runner is None:
        return {"state": "queued", "reason": "lane authority not composed"}
    status = await asyncio.to_thread(
        lanes.status, {"tenant_ref": tenant, "limit": 1, "now_ms": now_ms()}
    )
    active = int(status.get("tenant_active_count", 0))
    # Delivery work holds ``max_parallel`` slots; the one beyond them is a person's.
    if active >= max_parallel + (PERSON_SLOTS if person_input else 0):
        return {"state": "queued", "reason": "LANE_QUOTA"}
    hold = await asyncio.to_thread(
        lanes.reserve,
        {
            "tenant_ref": tenant,
            "work_item_id": f"delivery:{delivery['id']}:{assignment['id']}",
            "idempotency_key": f"delivery:{delivery['id']}:{assignment['id']}:lane",
            "now_ms": now_ms(),
        },
    )
    run_id = await runner.start(assignment_context(delivery, assignment), hold)
    return {"state": "started", "run_id": run_id, "hold_id": hold.get("hold_id")}


async def end_on_lost_lease(
    send: Callable[[str, dict[str, Any]], Awaitable[Any]],
    delivery_id: str,
    assignment_id: str,
) -> None:
    """A run that lost its lane lease reports its assignment dropped; the engine recovers it."""
    await send(
        "DeliveryReport",
        {
            "write": {
                "delivery_id": delivery_id,
                "assignment_id": assignment_id,
                "state": "dropped",
                "summary": "lane lease lost",
            }
        },
    )


__all__ = ["assignment_context", "end_on_lost_lease", "start_assignment"]
