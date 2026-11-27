"""One poll loop for the served change and transcript streams.

A stream is a served read operation repeated behind a cursor. Every poll goes through
``invoke_operation``, so the bearer is verified afresh on every round and on every
reconnect, scopes and visibility are the operation's own, and a revoked session stops
delivery within one poll interval. The loop keeps only the cursor for the length of one
connection; the engine owns every event (ARBORA-OPS-R062.2, R062.4, R065.2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from arbora.api.http.auth import HTTPAuthenticationError
from arbora.api.invoke.pipeline import OperationRefused
from arbora.gateway.ports import gateway_application

logger = logging.getLogger(__name__)

#: Close code of a revoked or unverifiable session; the client clears protected state.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_SCOPE = 4403
CLOSE_STALE_CURSOR = 4409
CLOSE_UNAVAILABLE = 4503
POLL_SECONDS = 1.0
_CLOSE_FOR = {
    "SCOPE_REQUIRED": CLOSE_SCOPE,
    "STALE_CURSOR": CLOSE_STALE_CURSOR,
}


class StopStream(Exception):
    """A test clock ended the stream; production waits on the socket instead."""


Pause = Callable[[WebSocket], Awaitable[None]]
Frame = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


async def wait_for_next_poll(websocket: WebSocket) -> None:
    """Sleep one poll interval, or return early when the client speaks or leaves."""
    try:
        await asyncio.wait_for(websocket.receive_text(), timeout=POLL_SECONDS)
    except TimeoutError:
        return


async def _refuse(websocket: WebSocket, refused: OperationRefused) -> None:
    await websocket.send_json(
        {"type": "error", "code": refused.code, "details": dict(refused.details)}
    )
    await websocket.close(code=_CLOSE_FOR.get(refused.code, CLOSE_UNAVAILABLE))


async def stream_operation(
    websocket: WebSocket,
    *,
    op_id: str,
    params: Callable[[Any], dict[str, Any]],
    frame: Frame,
    cursor: Any,
    pause: Pause | None = None,
) -> None:
    """Accept, then poll ``op_id`` from ``cursor`` until the client or the session ends."""
    await websocket.accept()
    # Looked up per call so a controlled clock can replace the wait (no wall sleeps).
    wait = pause or wait_for_next_poll
    try:
        while True:
            try:
                value = await gateway_application().invoke_operation(
                    op_id, params(cursor), request=websocket
                )
            except HTTPAuthenticationError:
                await websocket.close(code=CLOSE_UNAUTHENTICATED)
                return
            except OperationRefused as refused:
                await _refuse(websocket, refused)
                return
            value = value["value"] if set(value) == {"value"} else value
            message = frame(value)
            if message is not None:
                await websocket.send_json(dict(message))
            cursor = value["cursor"]
            await wait(websocket)
    except (WebSocketDisconnect, StopStream):
        logger.info("%s stream closed", op_id)
