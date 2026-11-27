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
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.ops.pin_guards import refusal
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

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


class GraphSubscribeParams(_common.Params):
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    limit: int | None = Field(default=None, ge=1, le=5000)


class GraphChangesResult(_common.Params):
    value: dict[str, Any]


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


GRAPH_CHANGES_PATH = "/ws/graph/changes"


def change_event_id(event: GraphChangeEvent) -> str:
    """The id a client deduplicates by: the commit and the event's place in it."""
    return f"{event.commit_id}:{event.event_ordinal}"


async def handle_graph_changes_subscribe(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One page of committed change events behind the cursor (ARBORA-OPS-R062.2).

    Events are ids and positions, never node content; the engine binds the cursor to
    the caller's authorization revision, so an ``AUTHORIZATION_CHANGED`` refusal is the
    marker that makes the client recover and re-check what it may see. A cursor the
    engine cannot continue answers ``STALE_CURSOR`` and names the engine's reason.
    """
    request = GraphSubscribeParams.model_validate(dict(params))
    try:
        require_methods("OpenGraphSnapshot", "ReadGraphChanges")
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    if request.cursor is None:
        return {"value": await _head(context)}
    return {"value": await _read(context, request.cursor, request.limit)}


async def _head(context: Any) -> dict[str, Any]:
    """No cursor yet: start at the engine's high-water mark, with no events."""
    answer = await engine_send(context, "OpenGraphSnapshot", {})
    head = answer.get("high_water_cursor") if isinstance(answer, Mapping) else None
    if not isinstance(head, str) or not head:
        raise unavailable("engine snapshot is missing or invalid")
    return _page(head, [])


async def _read(context: Any, cursor: str, limit: int | None) -> dict[str, Any]:
    from arbora.core.settings import runtime_config

    batch = min(limit or 5000, int(runtime_config.graph_changes_max_batch))
    try:
        answer = await engine_send(
            context, "ReadGraphChanges", {"cursor": cursor, "limit": batch}
        )
    except EngineRefusal as refused:
        if refused.code in RECOVERY_CODES:
            raise OperationRefused("STALE_CURSOR", {"reason": refused.code}) from None
        raise
    return _page_of(answer, cursor)


def _page(cursor: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cursor": cursor, "stream_path": GRAPH_CHANGES_PATH, "events": events}


def _page_of(answer: Any, cursor: str) -> dict[str, Any]:
    raw = answer.get("events") if isinstance(answer, Mapping) else None
    if not isinstance(raw, list):
        raise unavailable("engine change page is missing or invalid")
    try:
        events = [GraphChangeEvent.model_validate(item) for item in raw]
    except ValidationError:
        raise unavailable("engine change event is invalid") from None
    ordered = sorted(events, key=lambda e: (e.commit_id, e.event_ordinal))
    next_cursor = answer.get("cursor") if isinstance(answer, Mapping) else None
    return _page(
        next_cursor if isinstance(next_cursor, str) and next_cursor else cursor,
        [
            {"event_id": change_event_id(e), **e.model_dump(mode="json")}
            for e in ordered
        ],
    )


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
        _common.build_read_op(
            op_id="graph.changes.subscribe",
            summary="Read committed graph change events behind a cursor",
            examples=("follow graph changes",),
            params=GraphSubscribeParams,
            result=GraphChangesResult,
            handler="arbora.api.ops.graph_changes.handle_graph_changes_subscribe",
            scope="graph:read",
        ),
    )


specs = operations

__all__ = [
    "RECOVERY_CODES",
    "GraphChangeEvent",
    "GraphRecoverParams",
    "GraphRecoverResult",
    "GraphChangesResult",
    "GraphSnapshotRecovery",
    "GraphSubscribeParams",
    "handle_graph_changes_subscribe",
    "handle_graph_snapshot_recover",
    "operations",
    "specs",
]
