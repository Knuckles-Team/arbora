"""``/ws/meetings/{meeting_id}/transcript``: ordered transcript events (ARBORA-OPS-R065.2).

Each poll is ``meetings.transcript.subscribe`` behind the client's cursor, so the scope
``meetings:read`` and the bearer are checked again on every round and on reconnect. A frame
carries events in sequence order, a ``gap`` when a sequence is missing (distinct from an
unavailable engine) and ``finalized`` only when the contiguous sequence is complete.
"""

from __future__ import annotations

from typing import Any

from starlette.websockets import WebSocket

from arbora.gateway.stream_poll import stream_operation

MEETING_WS_PATH = "/ws/meetings/{meeting_id}/transcript"


def transcript_frame(value: Any) -> dict[str, Any] | None:
    """The page as a frame; an empty page with no gap and no finality sends nothing."""
    if not (value["events"] or value["gap"] or value["finalized"]):
        return None
    return {
        "type": "transcript",
        "cursor": value["cursor"],
        "events": value["events"],
        "gap": value["gap"],
        "finalized": value["finalized"],
    }


def cursor_of(websocket: WebSocket) -> int:
    raw = websocket.query_params.get("cursor", "0")
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 18 else 0


async def meeting_transcript_websocket(websocket: WebSocket) -> None:
    meeting_id = websocket.path_params["meeting_id"]
    await stream_operation(
        websocket,
        op_id="meetings.transcript.subscribe",
        params=lambda cursor: {"meeting_id": meeting_id, "cursor": cursor},
        frame=transcript_frame,
        cursor=cursor_of(websocket),
    )


def register_meeting_routes(app: Any) -> None:
    if not any(getattr(r, "path", None) == MEETING_WS_PATH for r in app.routes):
        app.add_api_websocket_route(MEETING_WS_PATH, meeting_transcript_websocket)
