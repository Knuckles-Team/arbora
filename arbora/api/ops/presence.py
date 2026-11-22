"""City presence: one durable city state, served on one channel (ARBORA-OPS-R915).

Arbora serves agent presence for the tenant's runs as a snapshot read, not a
stream of per-frame positions. The snapshot is a tenant-scoped, revisioned
projection of the Medulla city state (``MEDULLA-WORLD-VIEW-R004``); Arbora
never stores or advances positions. The read returns the last snapshot with
its interval and its computed-at time, and a consumer polling faster than
the interval receives the same snapshot flagged ``fresh: false``, not a new
computation. An uncomposed city-state port fails closed with a typed
``UNAVAILABLE``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import FrozenState, Params, build_read_op
from arbora.api.registry import (
    OpSpec,
)

CityKind = Literal["npc", "ai", "human"]


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
    from arbora.core.settings import runtime_config

    interval = float(runtime_config.presence_snapshot_interval_s)
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
    )
