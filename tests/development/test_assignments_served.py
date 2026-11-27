"""A delivery assignment starts on a reserved development lane (ARBORA-OPS-R896.12).

The lane authority is the engine's; the doubles below answer its status and reserve methods and
a meristem run starter. Arbora keeps no assignment state, so each decision reads the engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import development_assignments as assignments

DELIVERY = {"id": "d1", "scope_rows": ["R1"]}
ASSIGNMENT = {
    "id": "a1",
    "proposal_ref": "github:org/repo#7",
    "inputs": [{"event_id": "e1"}],
}


class Lanes:
    def __init__(self, active: int) -> None:
        self.active = active
        self.reserved: list[dict[str, Any]] = []

    def status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"tenant_active_count": self.active, "holds": []}

    def reserve(self, request: dict[str, Any]) -> dict[str, Any]:
        self.reserved.append(request)
        self.active += 1
        return {"hold_id": "hold:1"}


class Runner:
    def __init__(self) -> None:
        self.started: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def start(self, context: dict[str, Any], hold: dict[str, Any]) -> str:
        self.started.append((context, hold))
        return "run:1"


async def _start(lanes: Any, runner: Any, **kw: Any) -> dict[str, Any]:
    return await assignments.start_assignment(
        DELIVERY,
        ASSIGNMENT,
        tenant="tenant:one",
        lanes=lanes,
        runner=runner,
        now_ms=lambda: 1,
        **kw,
    )


@pytest.mark.spec("ARBORA-OPS-R896.12")
async def test_assignments_reserve_lanes_and_keep_a_person_slot() -> None:
    runner = Runner()
    free = Lanes(active=0)
    started = await _start(free, runner)
    assert started == {"state": "started", "run_id": "run:1", "hold_id": "hold:1"}
    assert free.reserved[0]["work_item_id"] == "delivery:d1:a1"
    context, hold = runner.started[0]
    assert context == {
        "delivery_id": "d1",
        "scope_rows": ["R1"],
        "proposal_ref": "github:org/repo#7",
        "inputs": [{"event_id": "e1"}],
        "recovery_note": None,
        "round_verdicts": {},
        "background_limit_s": None,
    }
    # At the cap, new work queues and refuses nothing; the extra slot is a person's.
    full = Lanes(active=4)
    queued = await _start(full, runner)
    assert queued == {"state": "queued", "reason": "LANE_QUOTA"} and full.reserved == []
    person = await _start(full, runner, person_input=True)
    assert person["state"] == "started" and len(full.reserved) == 1
    # Past the person slot too, even a person's input queues.
    assert (await _start(full, runner, person_input=True))["state"] == "queued"
    # No lane authority or run starter composed: the start queues.
    assert (await _start(None, None))["state"] == "queued"


@pytest.mark.spec("ARBORA-OPS-R896.12")
async def test_a_lost_lane_lease_ends_the_assignment_through_the_engine() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send(method: str, params: dict[str, Any]) -> None:
        sent.append((method, params))

    await assignments.end_on_lost_lease(send, "d1", "a1")
    assert sent == [
        (
            "DeliveryReport",
            {
                "write": {
                    "delivery_id": "d1",
                    "assignment_id": "a1",
                    "state": "dropped",
                    "summary": "lane lease lost",
                }
            },
        )
    ]
