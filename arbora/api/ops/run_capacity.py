"""Run admission reserves engine capacity; the lease is the only state.

``agents.plan_run`` reserves one slot of the tenant's run capacity cell through the
engine's ``AcquireCapacity`` before the WorkItem is claimed, and releases it through
``ReleaseCapacity`` when the terminal outcome is committed (ARBORA-HOST-R031.1). The
cell, its capacity and the lease belong to the engine; Arbora keeps the fence only for
the length of one served call. A lease nobody releases expires with its TTL and the
engine reclaims it, so a crashed host never strands a slot.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send, unavailable

LOG = logging.getLogger(__name__)

#: The engine capacity cell every run admission draws on, per tenant.
RUN_CELL_ID = "run:agents"
RUN_RESOURCE_CLASS = "worker"
RUN_PRIORITY = "orchestration"
RUN_SLOTS = 1
#: Matches the WorkItem claim lease; an unreleased lease is reclaimed after it.
RUN_LEASE_TTL_MS = 600_000
_GRANTED = frozenset({"accepted", "replayed"})
_REFUSED = frozenset({"exhausted", "backpressure"})


@dataclass(frozen=True, slots=True)
class CapacityReservation:
    """The engine lease fence of one admitted run."""

    cell_id: str
    lease_id: str
    lease_epoch: int
    fence_token: int


@runtime_checkable
class RunCapacity(Protocol):
    async def reserve(
        self, context: Any, *, run_id: str, work_item_id: str, owner_digest: str
    ) -> CapacityReservation: ...

    async def release(
        self, context: Any, reservation: CapacityReservation, *, owner_digest: str
    ) -> None: ...


def _now_ms() -> int:
    return int(time.time() * 1000)


class EngineRunCapacity:
    """``RunCapacity`` over the engine's capacity cell and lease methods."""

    async def reserve(
        self, context: Any, *, run_id: str, work_item_id: str, owner_digest: str
    ) -> CapacityReservation:
        answer = await engine_send(
            context,
            "AcquireCapacity",
            {
                "request": {
                    "demands": [
                        {
                            "amount": RUN_SLOTS,
                            "cell_id": RUN_CELL_ID,
                            "resource_class": RUN_RESOURCE_CLASS,
                        }
                    ],
                    # One lease per run: a replayed admission finds its own lease.
                    "idempotency_key": run_id,
                    "now_ms": _now_ms(),
                    "owner_digest": owner_digest,
                    "priority": RUN_PRIORITY,
                    "schema_version": "1",
                    "tenant_ref": str(context.caller.tenant),
                    "ttl_ms": RUN_LEASE_TTL_MS,
                    "work_item_id": work_item_id,
                }
            },
        )
        return _granted(answer)

    async def release(
        self, context: Any, reservation: CapacityReservation, *, owner_digest: str
    ) -> None:
        answer = await engine_send(
            context,
            "ReleaseCapacity",
            {
                "request": {
                    "idempotency_key": f"release:{reservation.lease_id}",
                    "leases": [
                        {
                            "fence_token": reservation.fence_token,
                            "lease_epoch": reservation.lease_epoch,
                            "lease_id": reservation.lease_id,
                        }
                    ],
                    "now_ms": _now_ms(),
                    "owner_digest": owner_digest,
                    "schema_version": "1",
                    "tenant_ref": str(context.caller.tenant),
                }
            },
        )
        decision = answer.get("decision") if isinstance(answer, Mapping) else None
        if decision not in {"released", "accepted", "replayed", "not_found"}:
            raise unavailable("the engine did not release the run capacity lease")


def _granted(answer: Any) -> CapacityReservation:
    """A grant names the lease; an exhausted cell is a throttle, never a retry here."""
    decision = answer.get("decision") if isinstance(answer, Mapping) else None
    if decision in _REFUSED:
        raise OperationRefused("CAPACITY_THROTTLED", {"reason": str(decision)})
    leases = answer.get("leases") if isinstance(answer, Mapping) else None
    if decision not in _GRANTED or not isinstance(leases, list) or len(leases) != 1:
        raise unavailable("the engine did not grant run capacity")
    lease = leases[0]
    try:
        return CapacityReservation(
            cell_id=str(lease["cell_id"]),
            lease_id=str(lease["lease_id"]),
            lease_epoch=int(lease["lease_epoch"]),
            fence_token=int(lease["fence_token"]),
        )
    except (KeyError, TypeError, ValueError):
        raise unavailable("the engine run capacity lease is malformed") from None


def run_capacity_of(context: Any) -> RunCapacity:
    """The composed port, else the engine one; never process-local counting."""
    return context.services.get("run_capacity") or EngineRunCapacity()


async def release_quietly(
    context: Any, reservation: CapacityReservation | None, *, owner_digest: str | None
) -> None:
    """Release after the terminal commit; a failed release is logged and bounded.

    The commit already made the outcome durable, so a failed release must not turn a
    finished run into an error. The lease TTL bounds the slot's hold.
    """
    if reservation is None:
        return
    try:
        await run_capacity_of(context).release(
            context, reservation, owner_digest=owner_digest or ""
        )
    except Exception:  # reported below; the TTL reclaims the lease
        LOG.warning(
            "run capacity lease %s was not released; it expires at its TTL",
            reservation.lease_id,
            exc_info=True,
        )
