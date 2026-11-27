"""``/ws/graph/changes``: the authenticated committed-change stream (ARBORA-OPS-R062.2, R062.4).

Every poll is ``graph.changes.subscribe`` behind the client's cursor, so the bearer and
the scope ``graph:read`` are verified again on every round and on every reconnect. A
revoked session ends delivery within one poll interval and closes with 4401. A cursor the
engine can no longer continue, including one bound to an older authorization revision,
answers ``STALE_CURSOR`` and closes with 4409; the client then calls
``graph.snapshot.recover`` and so re-checks what it may see.
"""

from __future__ import annotations

from typing import Any

from starlette.websockets import WebSocket

from arbora.api.ops.graph_changes import GRAPH_CHANGES_PATH
from arbora.gateway.stream_poll import stream_operation


def change_frame(value: Any) -> dict[str, Any] | None:
    if not value["events"]:
        return None
    return {"type": "changes", "cursor": value["cursor"], "events": value["events"]}


async def graph_changes_websocket(websocket: WebSocket) -> None:
    await stream_operation(
        websocket,
        op_id="graph.changes.subscribe",
        params=lambda cursor: {"cursor": cursor} if cursor else {},
        frame=change_frame,
        cursor=websocket.query_params.get("cursor") or None,
    )


def register_graph_change_routes(app: Any) -> None:
    if not any(getattr(r, "path", None) == GRAPH_CHANGES_PATH for r in app.routes):
        app.add_api_websocket_route(GRAPH_CHANGES_PATH, graph_changes_websocket)
