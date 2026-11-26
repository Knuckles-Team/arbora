"""Graph change models and snapshot recovery (ARBORA-OPS-R062.1, ARBORA-OPS-R062.3).

The engine owns the committed change feed (MEDULLA-DURABLE-KERNEL-R043.100):
``ReadGraphChanges`` pages events behind a cursor bound to the graph, the
graph incarnation and the caller's authorization revision, and
``OpenGraphSnapshot`` answers the state a client replaces its own with.
``graph.snapshot.recover`` returns that snapshot whenever the engine refuses a
cursor as expired, stale or authorized differently, so a client never sees a
silent gap. Arbora keeps no cursor, no event and no snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

READ_SCOPE = "kg:read"
#: The engine refusals that mean "this cursor can no longer be continued".
RECOVERY_CODES = frozenset(
    {"CURSOR_EXPIRED", "GRAPH_INCARNATION_STALE", "AUTHORIZATION_CHANGED"}
)


class GraphChangeEvent(_common.Params):
    """One committed change event, as the engine publishes it."""

    schema_version: int = Field(ge=0)
    commit_id: str = Field(min_length=1)
    event_ordinal: int = Field(ge=0)
    committed_at_ms: int = Field(ge=0)
    visible_at_ms: int = Field(ge=0)
    commit_to_visible_ms: int = Field(ge=0)
    change: dict[str, Any]


class GraphSnapshotRecovery(_common.Params):
    """The engine snapshot a client replaces its protected state with."""

    schema_version: int = Field(ge=0)
    incarnation: int = Field(ge=0)
    committed_revision: int = Field(ge=0)
    snapshot_digest: str = Field(min_length=1)
    visibility_revision: int = Field(ge=0)
    high_water_cursor: str = Field(min_length=1)


class GraphRecoverParams(_common.Params):
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class GraphRecoverResult(_common.Params):
    state: Literal["continue", "recover"]
    cursor: str
    snapshot: GraphSnapshotRecovery | None = None


async def handle_graph_snapshot_recover(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Probe the cursor with the engine; hand back a snapshot when it cannot continue."""
    try:
        require_methods("OpenGraphSnapshot", "ReadGraphChanges")
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    cursor = params.get("cursor")
    if cursor is not None:
        try:
            await engine_send(
                context, "ReadGraphChanges", {"cursor": cursor, "limit": 0}
            )
        except EngineRefusal as refused:
            if refused.code not in RECOVERY_CODES:
                raise
        else:
            return {"state": "continue", "cursor": cursor, "snapshot": None}
    answer = await engine_send(context, "OpenGraphSnapshot", {})
    try:
        snapshot = GraphSnapshotRecovery.model_validate(answer)
    except ValidationError:
        raise unavailable("engine snapshot is missing or invalid") from None
    return {
        "state": "recover",
        "cursor": snapshot.high_water_cursor,
        "snapshot": snapshot.model_dump(mode="json"),
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_read_op(
            op_id="graph.snapshot.recover",
            summary="Recover graph state from an engine snapshot when a change cursor expired",
            examples=("recover the graph after a gap",),
            params=GraphRecoverParams,
            result=GraphRecoverResult,
            handler="arbora.api.ops.graph_changes.handle_graph_snapshot_recover",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "RECOVERY_CODES",
    "GraphChangeEvent",
    "GraphRecoverParams",
    "GraphRecoverResult",
    "GraphSnapshotRecovery",
    "handle_graph_snapshot_recover",
    "operations",
    "specs",
]
