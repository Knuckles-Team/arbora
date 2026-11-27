"""The engine's build-slot lease family behind the ``CapacityLeases`` port (ARBORA-CAPACITY-R754.1).

The engine decides and records every transition: the pool, the fair queue, the priority
table and the lease state are its own. This module only shapes the engine request from the
holder and tenant the handler derived from the verified caller, and returns the lease the
engine answered. ``StartCapacity`` and ``RevokeCapacity`` are engine ``main`` methods the
pinned release lacks (PIN-LAG), so every send goes through ``send_main``, which refuses
``UNAVAILABLE`` naming the missing method.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import unavailable
from arbora.api.ops.pin_guards import send_main

__all__ = ["EngineCapacityLeases"]

SCHEMA_VERSION = "1"
DEFAULT_TTL_MS = 60_000
DEFAULT_QUEUE_TTL_MS = 7_200_000
BUILD_SLOT = "build_slot"
_ACQUIRED = frozenset({"accepted", "replayed"})
_MUTATIONS = frozenset({"StartCapacity", "RenewCapacity", "ReleaseCapacity"})
_STATUS_PAGE = 1


def _one_lease(answer: Any, what: str) -> dict[str, Any]:
    leases = answer.get("leases") if isinstance(answer, Mapping) else None
    if not isinstance(leases, list) or len(leases) != 1:
        raise unavailable(f"the engine did not answer {what} with exactly one lease")
    return dict(leases[0])


class EngineCapacityLeases:
    """``CapacityLeases`` over the engine's capacity methods."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    async def request(
        self, context: Any, *, owner: str, tenant: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        answer = await send_main(
            context,
            "AcquireCapacity",
            {
                "request": {
                    "schema_version": SCHEMA_VERSION,
                    "tenant_ref": tenant,
                    "work_item_id": params["work_item_id"],
                    "owner_digest": owner,
                    "idempotency_key": context.idempotency_key,
                    "priority": params["priority"],
                    "demands": [
                        {
                            "cell_id": params["cell_id"],
                            "resource_class": BUILD_SLOT,
                            "amount": 1,
                        }
                    ],
                    "lease_id": None,
                    "ttl_ms": params.get("ttl_ms") or DEFAULT_TTL_MS,
                    "now_ms": self._now_ms(),
                    "cost_budget_micros": None,
                    "token_budget": None,  # nosec B105 - a token budget, not a credential
                    "queue_ttl_ms": DEFAULT_QUEUE_TTL_MS,
                }
            },
            idempotent=True,
        )
        decision = answer.get("decision") if isinstance(answer, Mapping) else None
        if decision not in _ACQUIRED:
            raise OperationRefused("CAPACITY_UNAVAILABLE", {"decision": str(decision)})
        return {
            "lease": _one_lease(answer, "the lease request"),
            "queue_position": answer.get("queue_position"),
        }

    async def mutate(
        self,
        context: Any,
        method: str,
        *,
        owner: str,
        tenant: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        if method not in _MUTATIONS:
            raise ValueError(f"{method} is not a lease mutation")
        answer = await send_main(
            context,
            method,
            {
                "request": {
                    "schema_version": SCHEMA_VERSION,
                    "tenant_ref": tenant,
                    "owner_digest": owner,
                    "leases": [
                        {
                            "lease_id": params["lease_id"],
                            "lease_epoch": params["lease_epoch"],
                            "fence_token": params["fence_token"],
                        }
                    ],
                    "now_ms": self._now_ms(),
                    "ttl_ms": params.get("ttl_ms"),
                    "idempotency_key": context.idempotency_key,
                }
            },
            idempotent=True,
        )
        return {"lease": _one_lease(answer, method), "queue_position": None}

    async def revoke(
        self, context: Any, *, tenant: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        answer = await send_main(
            context,
            "RevokeCapacity",
            {
                "request": {
                    "schema_version": SCHEMA_VERSION,
                    "tenant_ref": tenant,
                    "lease_id": params["lease_id"],
                    "reason": params["reason"],
                    "now_ms": self._now_ms(),
                }
            },
            idempotent=True,
        )
        return {"lease": _one_lease(answer, "the revoke"), "queue_position": None}

    async def status(
        self, context: Any, *, tenant: str, lease_id: str
    ) -> dict[str, Any]:
        answer = await send_main(
            context,
            "CapacityStatus",
            {
                "request": {
                    "schema_version": SCHEMA_VERSION,
                    "tenant_ref": tenant,
                    "cell_id": None,
                    "lease_id": lease_id,
                    "max_count": _STATUS_PAGE,
                    "cursor": None,
                }
            },
        )
        leases = answer.get("leases") if isinstance(answer, Mapping) else None
        if not isinstance(leases, list) or not leases:
            # A foreign lease answers exactly like an absent one.
            raise OperationRefused("NOT_FOUND", {"resource": "lease"})
        entries: list[dict[str, Any]] = []
        for group in answer.get("transitions") or ():
            if group.get("lease_id") == lease_id:
                entries = [dict(entry) for entry in group.get("entries") or ()]
        return {"lease": dict(leases[0]), "transitions": entries}
