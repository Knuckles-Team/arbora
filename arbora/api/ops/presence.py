"""City presence: one durable city state, rendered by many viewers.

Arbora serves agent presence for the tenant's runs as a snapshot read, not a
stream of per-frame positions (ARBORA-OPS-R915). The snapshot is a
tenant-scoped, revisioned projection of the Medulla city state
(``MEDULLA-WORLD-VIEW-R004``); Arbora never stores or advances positions.
The read returns the last snapshot with its interval and its computed-at
time, and a consumer polling faster than the interval receives the same
snapshot flagged ``fresh: false``, not a new computation.

The companion governed heartbeat (ARBORA-OPS-R915.1) lets a human publish
only its own virtual avatar position: the subject is the verified caller
principal, never a request field; the update is fenced by
``expected_revision`` and rate-limited to one accepted update per five
seconds, idempotent by ``(subject, revision)``; a stale revision refuses
``REVISION_CONFLICT`` and the anonymous sandbox has no write.

An uncomposed city-state port fails closed with a typed ``UNAVAILABLE``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import FrozenState, Params, build_read_op
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Verb,
)

CityKind = Literal["npc", "ai", "human"]

#: One accepted heartbeat update per subject per this many seconds.
HEARTBEAT_WINDOW_S = 5.0

#: Replay owner of the heartbeat write: the Medulla city-state store's own
#: compare-and-set on (subject, revision), so a replayed identical request
#: applies at most once.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "fleet.city.presence.heartbeat": "medulla:city_state",
}


class CityPresenceEntity(FrozenState):
    """One city-state fact: a stable opaque id, one kind, a virtual position."""

    id: str
    kind: CityKind
    x: float
    y: float
    heading: float
    activity: str
    target: str | None
    observed_at: datetime
    provenance: str
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _expiry_is_human_only(self) -> CityPresenceEntity:
        if self.kind != "human" and self.expires_at is not None:
            raise ValueError("only human presence carries an expiry")
        return self


class CityStateSnapshot(FrozenState):
    """One engine city-state read: the durable facts and their revision."""

    tenant: str
    revision: int = Field(ge=0)
    simulated_at: datetime
    layout_revision: int = Field(ge=0)
    entities: tuple[CityPresenceEntity, ...] = ()


@runtime_checkable
class CityStatePort(Protocol):
    """The one typed port Arbora calls the Medulla city state through.

    The port is composed as ``city_state`` by the served process. A refusal
    raised by the port (``REVISION_CONFLICT``, ``SUBJECT_ACCESS_DENIED``) is
    a closed arbora code and changes no state.
    """

    async def snapshot(
        self, *, tenant: str, context: ExecutionContext
    ) -> CityStateSnapshot: ...

    async def heartbeat(
        self,
        *,
        tenant: str,
        subject: str,
        expected_revision: int,
        x: float,
        y: float,
        heading: float,
        context: ExecutionContext,
    ) -> HeartbeatReceipt: ...


class HeartbeatReceipt(FrozenState):
    """The engine's proof that one subject-bound update was accepted."""

    tenant: str
    subject: str
    revision: int = Field(ge=0)
    accepted_at: datetime


class HeartbeatParams(Params):
    """The only fields a human may publish: its own avatar move, fenced.

    The subject is never a request field: it is the verified caller
    principal. The closed ``extra="forbid`` config (inherited from
    ``Params``) refuses any physical-location or foreign-subject field.
    """

    expected_revision: int = Field(ge=0)
    x: float
    y: float
    heading: float


class PresenceSnapshotParams(Params):
    """The snapshot read takes no request fields; the tenant is verified."""


class PresenceSnapshotValue(FrozenState):
    """The served snapshot: the engine facts plus the presentation cadence."""

    tenant: str
    revision: int = Field(ge=0)
    simulated_at: datetime
    layout_revision: int = Field(ge=0)
    interval_s: float = Field(gt=0)
    computed_at: datetime
    fresh: bool
    entities: tuple[CityPresenceEntity, ...] = ()


class PresenceSnapshotResult(Params):
    value: PresenceSnapshotValue


class HeartbeatResult(Params):
    value: HeartbeatReceipt
    receipt: str = ""


#: In-process rate fence: the last accepted update per (tenant, subject) and
#: the stored outcome per (tenant, subject, expected_revision) for replay.
#: The fence is a presentation guard; the engine's compare-and-set is the
#: source of truth, so a refusal never consumes the window.
_accepted_heartbeats: dict[tuple[str, str, int], HeartbeatReceipt] = {}
_last_heartbeat: dict[tuple[str, str], datetime] = {}
_fence_lock = threading.Lock()


def _heartbeat_result(receipt: HeartbeatReceipt, context: Any) -> dict[str, Any]:
    return {
        "value": receipt.model_dump(mode="json"),
        "receipt": getattr(context, "audit_ref", "") or "",
    }


async def handle_city_heartbeat(
    context: Any,
    params: Mapping[str, Any],
    op: OpSpec,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Publish one human's own virtual avatar move, subject-bound and fenced.

    The subject is the verified caller principal, never a request field. A
    replay of an accepted (subject, revision) update returns the stored
    outcome; a new update inside the five-second window refuses
    ``RATE_LIMITED``; a stale revision refuses ``REVISION_CONFLICT`` from the
    engine. Every refusal changes no state.
    """
    request = HeartbeatParams.model_validate(dict(params))
    port = context.services.get("city_state")
    if port is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "city state engine unbound"})
    tenant = context.caller.tenant
    subject = context.caller.principal
    key = (tenant, subject, request.expected_revision)
    with _fence_lock:
        replay = _accepted_heartbeats.get(key)
        if replay is not None:
            return _heartbeat_result(replay, context)
        last = _last_heartbeat.get((tenant, subject))
        if last is not None and (clock() - last).total_seconds() < HEARTBEAT_WINDOW_S:
            raise OperationRefused(
                "RATE_LIMITED",
                {"subject": subject, "window_s": HEARTBEAT_WINDOW_S},
            )
    receipt = await port.heartbeat(
        tenant=tenant,
        subject=subject,
        expected_revision=request.expected_revision,
        x=request.x,
        y=request.y,
        heading=request.heading,
        context=context,
    )
    try:
        receipt = (
            receipt
            if isinstance(receipt, HeartbeatReceipt)
            else HeartbeatReceipt.model_validate(receipt)
        )
    except (ValidationError, TypeError):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "city state receipt malformed"}
        ) from None
    if receipt.tenant != tenant or receipt.subject != subject:
        raise OperationRefused("SUBJECT_ACCESS_DENIED", {"subject": subject})
    with _fence_lock:
        _accepted_heartbeats[key] = receipt
        _last_heartbeat[(tenant, subject)] = clock()
    return _heartbeat_result(receipt, context)


async def handle_city_presence(
    context: Any,
    params: Mapping[str, Any],
    op: OpSpec,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Serve one tenant-scoped city presence snapshot at the bounded cadence."""
    PresenceSnapshotParams.model_validate(dict(params))
    port = context.services.get("city_state")
    if port is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "city state engine unbound"})
    raw = await port.snapshot(tenant=context.caller.tenant, context=context)
    try:
        snapshot = (
            raw
            if isinstance(raw, CityStateSnapshot)
            else CityStateSnapshot.model_validate(raw)
        )
    except (ValidationError, TypeError):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "city state snapshot malformed"}
        ) from None
    if snapshot.tenant != context.caller.tenant:
        raise OperationRefused("SUBJECT_ACCESS_DENIED", {"subject": snapshot.tenant})
    interval = float(context.services["config"].presence_snapshot_interval_s)
    age = (clock() - snapshot.simulated_at).total_seconds()
    value = PresenceSnapshotValue(
        tenant=snapshot.tenant,
        revision=snapshot.revision,
        simulated_at=snapshot.simulated_at,
        layout_revision=snapshot.layout_revision,
        interval_s=interval,
        computed_at=snapshot.simulated_at,
        fresh=age <= interval,
        entities=snapshot.entities,
    )
    return {"value": value.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="fleet.city.presence",
            summary="Read the tenant's city presence snapshot",
            examples=("who is in the city right now",),
            params=PresenceSnapshotParams,
            result=PresenceSnapshotResult,
            handler="arbora.api.ops.presence.handle_city_presence",
            scope="fleet:read",
        ),
        OpSpec(
            id="fleet.city.presence.heartbeat",
            verb=Verb.WRITE,
            summary="Publish one human's own virtual avatar position",
            examples=("move my avatar in the city",),
            params=HeartbeatParams,
            result=HeartbeatResult,
            binding=Composite(handler="arbora.api.ops.presence.handle_city_heartbeat"),
            scopes=frozenset({"scene:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.NATURAL,
            audit=AuditClass.EVENT,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
        ),
    )
