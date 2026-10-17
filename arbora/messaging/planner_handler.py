"""The default inbound handler: one agent turn and one reply per burst.

For each inbound message the handler does these steps in order:

1. It ignores events that are not messages, and messages with no text.
2. It gives the text to a waiting goal loop when that loop asked the question.
3. It answers a built-in command at once.
4. It collects a burst of messages from one channel into one turn. It gives
   that turn to the bound turn handler and sends the reply as a thread reply.

The turn handler runs the agent. The front end does not run the agent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.core.settings import setting
from arbora.messaging.base import MessagingBackend
from arbora.messaging.coalescer import BurstCoalescer, Clock, Sleep
from arbora.messaging.commands import handle_command
from arbora.messaging.host_ports import host_ports
from arbora.messaging.models import EventType, InboundEvent

__all__ = ["channel_session", "create_planner_handler"]

logger = logging.getLogger(__name__)

InboundHandler = Callable[[InboundEvent, MessagingBackend], Awaitable[None]]


def channel_session(platform: str, channel_id: str) -> str:
    """Return the one stable session key for a chat channel."""
    return f"messaging:{platform}:{channel_id}"


async def _send(backend: MessagingBackend, event: InboundEvent, text: str) -> None:
    message_id = getattr(event.message, "id", "") if event.message else ""
    try:
        if message_id:
            await backend.reply_to(event.channel_id, message_id, text)
        else:
            await backend.send_message(event.channel_id, text)
    except Exception as exc:  # noqa: BLE001 - one failed send must not stop intake
        logger.error("messaging reply send failed: %s", exc)


def _burst_responder(service: Any) -> Callable[[str, list[Any]], Awaitable[None]]:
    async def reply_to_burst(_key: str, items: list[dict[str, Any]]) -> None:
        event, backend = items[-1]["event"], items[-1]["backend"]
        combined = "\n".join(item["content"] for item in items)
        turn_handler = host_ports().turn_handler
        if turn_handler is None:
            logger.error("messaging: no turn handler is bound; burst not answered")
            return
        reply = await turn_handler(
            service._resolve_engine(),
            combined,
            session=channel_session(str(event.platform), event.channel_id),
        )
        if reply:
            await _send(backend, event, reply)

    return reply_to_burst


async def create_planner_handler(
    knowledge_engine: Any = None,
    *,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
) -> InboundHandler:
    """Return the default inbound handler bound to ``knowledge_engine``.

    ``MESSAGING_BURST_WINDOW_S`` and ``MESSAGING_BURST_MAX_S`` set the burst
    quiet window and the burst limit. ``clock`` and ``sleep`` replace the
    loop clock and ``asyncio.sleep`` for the burst timer.
    """
    from arbora.messaging.service import MessagingService

    service = MessagingService.instance(knowledge_engine)
    timers: dict[str, Any] = {"clock": clock}
    if sleep is not None:
        timers["sleep"] = sleep
    coalescer = BurstCoalescer(
        _burst_responder(service),
        window_s=float(setting("MESSAGING_BURST_WINDOW_S", "2.5")),
        max_wait_s=float(setting("MESSAGING_BURST_MAX_S", "12")),
        **timers,
    )

    async def planner_handler(event: InboundEvent, backend: MessagingBackend) -> None:
        if event.event_type != EventType.MESSAGE:
            return
        content = event.content or (event.message.content if event.message else "")
        if not content:
            return
        if service.deliver_reply(str(event.platform), event.channel_id, content):
            return
        command_reply = await handle_command(content, service=service)
        if command_reply is not None:
            await _send(backend, event, command_reply)
            return
        await coalescer.submit(
            f"{event.platform}:{event.channel_id}",
            {"event": event, "backend": backend, "content": content},
        )

    return planner_handler
