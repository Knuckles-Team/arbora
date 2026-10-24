"""Default-off, zero-effect ASGI transport owned by Arbora admission."""

import asyncio
import json
import logging
from typing import Any

from arbora.api.contracts.session import use_session
from arbora.client.browser_boundary import (
    LOCAL_BROWSER_PATH,
    LocalBrowserBoundary,
    _issue_local_browser_boundary,
)
from arbora.identity.local_browser_channel import LocalBrowserChannelOwner
from arbora.security import anonymous

_LOG = logging.getLogger(__name__)


def local_browser_boundary(owner: LocalBrowserChannelOwner) -> LocalBrowserBoundary:
    """Compose only a canonical configured owner, never caller-supplied claims."""
    if type(owner) is not LocalBrowserChannelOwner or not anonymous.enabled():
        raise TypeError("Configured canonical local browser owner required")

    async def handler(scope: Any, receive: Any, send: Any) -> None:
        try:

            async def bounded_send(message: Any) -> None:
                async with asyncio.timeout(1):
                    await send(message)

            await _serve(owner, scope, receive, bounded_send)
            return
        except Exception:  # noqa: BLE001 - no native/source details cross the socket
            _LOG.warning("Local browser channel refused; retirement may be unresolved")
        async with asyncio.timeout(1):
            await send({"type": "websocket.close", "code": 4403})

    return _issue_local_browser_boundary(handler)


async def _serve(owner: Any, scope: Any, receive: Any, send: Any) -> None:
    if (
        scope.get("type") != "websocket"
        or scope.get("path") != LOCAL_BROWSER_PATH
        or scope.get("raw_path") != LOCAL_BROWSER_PATH.encode("ascii")
        or scope.get("root_path", "") != ""
        or scope.get("query_string") != b""
    ):
        raise PermissionError("Exact local browser route required")
    session = anonymous.session()
    with use_session(session):
        async with owner.channel(scope, session, ()):
            async with asyncio.timeout(5):
                if await receive() != {"type": "websocket.connect"}:
                    raise PermissionError("WebSocket handshake required")
            await owner.validate(scope)
            await send({"type": "websocket.accept"})
            await owner.validate(scope)
            await send(
                {
                    "type": "websocket.send",
                    "text": json.dumps(
                        {
                            "type": "local_channel_ready",
                            "identity": "anonymous",
                            "operation_authority": False,
                        }
                    ),
                }
            )
            await _frames(owner, scope, receive, send)


async def _frames(owner: Any, scope: Any, receive: Any, send: Any) -> None:
    last_frame = 0.0
    for _ in range(32):
        async with asyncio.timeout(5):
            message = await receive()
        if message.get("type") == "websocket.disconnect":
            return
        now = asyncio.get_running_loop().time()
        if now - last_frame < 0.05:
            raise PermissionError("Local browser frame rate exceeded")
        last_frame = now
        text = message.get("text")
        if (
            message.get("type") != "websocket.receive"
            or type(text) is not str
            or message.get("bytes") is not None
            or len(text.encode("utf-8")) > 1024
            or json.loads(text, object_pairs_hook=lambda pairs: pairs)
            != [("type", "ping")]
        ):
            raise PermissionError("Only bounded zero-effect ping frames are accepted")
        await owner.validate(scope)
        await send({"type": "websocket.send", "text": '{"type":"pong"}'})
    raise PermissionError("Local browser frame limit reached")
