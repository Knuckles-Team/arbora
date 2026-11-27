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
from dataclasses import dataclass
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
    "ResolvedRunDelegation",
    "RunDelegationRefused",
    "delegation_refused",
    "engine_delegation_reader",
    "engine_run_delegations",
    "engine_identity_sender",
    "resolve_run_delegation",
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


#: Reads one delegation by id: the engine's ``DelegationState`` mapping, or ``None``
#: when the engine answers ``DELEGATION_UNKNOWN`` (a foreign tenant answers the same).
DelegationStateReader = Callable[[str], Awaitable[Mapping[str, Any] | None]]
_OWNER_NAMESPACE = "a2a-owner"


class RunDelegationRefused(Exception):
    """The resumed run has no delegation held by its owner; it must end ``failed``.

    One code answers every refusal (absent, foreign, revoked, expired), so the
    outcome tells a caller nothing about which delegation exists.
    """

    code = DELEGATION_INVALID

    def __init__(self, reason: str) -> None:
        super().__init__(f"{DELEGATION_INVALID}: {reason}")
        #: The internal reason, for the host's own log; never sent to a client.
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ResolvedRunDelegation:
    """The delegation a reclaimed run acts under, proven to belong to its owner."""

    delegation_id: str
    principal: str
    operations: tuple[str, ...]


def owner_ref_of(tenant: str, principal: str) -> str:
    """The durable owner reference the item stores for ``principal`` in ``tenant``."""
    from scion_sdk.privacy import persistence_reference

    return persistence_reference(
        "actor", principal, namespace=f"{_OWNER_NAMESPACE}:{tenant}"
    )


async def resolve_run_delegation(
    item: Mapping[str, Any],
    claim: Mapping[str, Any],
    *,
    read: DelegationStateReader | None,
    tenant: str,
) -> ResolvedRunDelegation:
    """Prove the claim's ``agent_run`` delegation belongs to the run's owner, or refuse.

    The delegation id comes from ``ClaimWorkItem`` (``delegation_id``), as the
    tool-task consumer takes ``authority_ref``. The record's principal must be the
    run's owner (the item's ``a2a_owner_ref``); a missing, foreign, revoked or expired
    delegation, or one for another run, refuses and the run ends ``failed``.
    """
    delegation_id = str(claim.get("delegation_id") or "")
    if not delegation_id or read is None:
        raise RunDelegationRefused("absent")
    try:
        state = await read(delegation_id)
    except Exception as exc:  # an unreadable record grants nothing
        raise RunDelegationRefused("unreadable") from exc
    record = state.get("record") if isinstance(state, Mapping) else None
    if not isinstance(record, Mapping):
        raise RunDelegationRefused("absent")
    metadata = item.get("metadata") or {}
    principal = str(record.get("principal") or "")
    delegator = claim.get("delegator_id")
    run_id = str(metadata.get("a2a_run_id") or "")
    task_id = str(item.get("work_item_id") or "").rsplit(":", 1)[-1]
    refusal = (
        "tenant"
        if record.get("tenant") != tenant
        else "kind"
        if str(getattr(record.get("kind"), "value", record.get("kind")))
        != AGENT_RUN_KIND
        else "run"
        if record.get("run_id") not in {run_id, task_id}
        else "owner"
        if not principal
        or not metadata.get("a2a_owner_ref")
        or owner_ref_of(tenant, principal) != metadata.get("a2a_owner_ref")
        or (delegator is not None and delegator != principal)
        else "standing"
        if str(getattr(state.get("standing"), "value", state.get("standing")))
        != "active"
        or record.get("revoked_at") is not None
        or state.get("principal_active") is not True
        or state.get("holds_tool_scope") is not True
        else ""
    )
    if refusal:
        raise RunDelegationRefused(refusal)
    return ResolvedRunDelegation(
        delegation_id=delegation_id,
        principal=principal,
        operations=tuple(str(op) for op in record.get("operations") or ()),
    )


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


def engine_delegation_reader(
    engine_provider: Callable[[], Any],
) -> DelegationStateReader:
    """Read one delegation (``GetDelegationRecord``) as the verified session of the host."""

    async def read(delegation_id: str) -> Mapping[str, Any] | None:
        from medulla.generated.security import (
            decode_get_delegation_record,
            send_get_delegation_record,
        )

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
        try:
            answer = await send_get_delegation_record(
                client, {"work_item_id": delegation_id}, graph
            )
        except Exception as exc:  # DELEGATION_UNKNOWN is an absent record
            if getattr(exc, "code", None) == "DELEGATION_UNKNOWN":
                return None
            raise
        state = decode_get_delegation_record(answer)
        dump = getattr(state, "model_dump", None)
        return dump(mode="json") if callable(dump) else state

    return read


def acting_session(session: Any, delegation: ResolvedRunDelegation) -> Any:
    """The session a reclaimed run acts under: the host's, carrying the run's delegation.

    The engine then runs every call of the run as the delegation's principal, the run's
    owner (``MEDULLA-IDENTITY-R744``). A release that cannot carry the claim, or a host
    session without the service-only ``identity:delegate`` scope, refuses: the run must
    not fall back to acting as the host.
    """
    from arbora.pin_lag.medulla import (
        DELEGATE_SCOPE,
        PinLagUnavailable,
        require_delegated_identity,
    )

    try:
        require_delegated_identity()
    except PinLagUnavailable as unavailable:
        raise RunDelegationRefused(f"unavailable:{unavailable.missing}") from None
    if DELEGATE_SCOPE not in session.scopes:
        raise RunDelegationRefused("host_scope")
    return session.with_delegation(delegation.delegation_id)
