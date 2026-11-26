"""The agent-run delegation of an admitted A2A run (ARBORA-A2A-R001.2.2).

At admission Arbora records one ``agent_run`` delegation in the engine for the
verified caller: the run's id, the operation set the run may use, and an expiry at
the item's lease horizon. The WorkItem is submitted with ``metadata.delegation_id``
naming it. The engine holds the record, so a restart loses nothing and Arbora keeps
no credential. A revoked, expired or no-longer-held delegation makes the engine
refuse ``SubmitWorkItem`` and a named ``ClaimWorkItem`` with ``DELEGATION_INVALID``.

The record shape is engine ``main`` and the pinned release lacks it (PIN-LAG: remove
the guard after the re-pin that carries ``DelegationRecord.kind``, ``run_id`` and
``operations``). Until then recording refuses ``UNAVAILABLE`` naming the field.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.pin_lag.medulla import (
    agent_run_delegation_served,
    require_agent_run_delegation,
)

__all__ = [
    "AGENT_RUN_KIND",
    "DELEGATION_INVALID",
    "DELEGATION_KEY",
    "EngineRunDelegations",
    "RECLAIM_ATTEMPTS",
    "delegation_refused",
    "engine_run_delegations",
    "engine_identity_sender",
    "run_delegation_id",
]

AGENT_RUN_KIND = "agent_run"
#: The WorkItem metadata key that names the run's delegation.
DELEGATION_KEY = "delegation_id"
#: The engine's refusal of a revoked, expired or no-longer-held delegation.
DELEGATION_INVALID = "DELEGATION_INVALID"
#: How many lease terms the delegation outlives: the item's lease horizon is the
#: run lease times the reclaims one item may take.
RECLAIM_ATTEMPTS = 3

#: Sends one engine ``Identity`` op for the verified caller and answers its reply.
IdentitySender = Callable[[Mapping[str, Any]], Awaitable[Any]]


def run_delegation_id(task_id: str) -> str:
    """The delegation id of a run: one per task, so a retried admission finds it."""
    return f"a2a-run:{task_id}"


def delegation_refused(error: BaseException) -> bool:
    """Whether the engine refused with ``DELEGATION_INVALID``."""
    return getattr(error, "code", None) == DELEGATION_INVALID or str(error).startswith(
        DELEGATION_INVALID
    )


class EngineRunDelegations:
    """Record agent-run delegations through the engine ``Identity`` delegation family."""

    def __init__(
        self,
        send: IdentitySender,
        *,
        clock: Callable[[], float],
        lease_s: float,
    ) -> None:
        self._send = send
        self._clock = clock
        self._horizon_ms = int(lease_s * RECLAIM_ATTEMPTS * 1000)

    def expires_at_ms(self) -> int:
        """The lease horizon of an item admitted now, on the injected clock."""
        return int(self._clock() * 1000) + self._horizon_ms

    async def record(
        self,
        *,
        tenant: str,
        principal: str,
        task_id: str,
        run_id: str,
        operations: Sequence[str],
    ) -> str:
        """Record the run's delegation and answer its id; refuse on the pinned release."""
        require_agent_run_delegation()
        if not operations:
            raise ValueError("an agent-run delegation names at least one operation")
        delegation_id = run_delegation_id(task_id)
        await self._send(
            {
                "family": "delegation",
                "op": "record",
                "request": {
                    "work_item_id": delegation_id,
                    "record": {
                        "tenant": tenant,
                        "principal": principal,
                        "tool": "",
                        "kind": AGENT_RUN_KIND,
                        "run_id": run_id,
                        "operations": sorted(set(operations)),
                        "expires_at": self.expires_at_ms(),
                    },
                },
            }
        )
        return delegation_id


def engine_identity_sender(engine_provider: Callable[[], Any]) -> IdentitySender:
    """Send one ``Identity`` op through the engine client of the verified session."""

    async def send(op: Mapping[str, Any]) -> Any:
        from medulla.generated.security import decode_identity, send_identity

        from arbora import work_items
        from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

        engine = engine_provider()
        if engine is None:
            raise RuntimeError("Arbora task authority is unavailable")
        routed = getattr(engine, "_session_client", None)
        client = routed() if callable(routed) else engine
        try:
            graph = work_items.native_owner_graph(
                getattr(engine, "_work_item_engine", engine)
            )
        except work_items.WorkItemBackendUnavailable:
            graph = CONTROL_GRAPH
        answer = await send_identity(client, {"op": dict(op)}, graph)
        return decode_identity(answer)

    return send


def engine_run_delegations(
    engine_provider: Callable[[], Any],
    *,
    clock: Callable[[], float],
    lease_s: float,
) -> EngineRunDelegations | None:
    """The delegation recorder, or ``None`` while the installed engine lacks the kind."""
    if not agent_run_delegation_served():
        return None
    return EngineRunDelegations(
        engine_identity_sender(engine_provider), clock=clock, lease_s=lease_s
    )
