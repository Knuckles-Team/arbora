"""The Arbora inbox reaper keeps failed deliveries pending for another attempt."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arbora.messaging import inbox
from arbora.messaging.host_ports import bind_host_ports
from arbora.messaging.models import SendResult
from arbora.messaging.router import InboundRouter
from arbora.messaging.service import MessagingService

from ._fakes import FakeMessagingBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_reaper_uses_bound_turn_handler_and_preserves_send_result(
    monkeypatch: pytest.MonkeyPatch, delivered: bool
) -> None:
    engine = object()
    router = InboundRouter()
    router._running = True
    seen: list[Any] = []

    class Backend(FakeMessagingBackend):
        async def send_message(
            self, channel_id: str, text: str, **kwargs: Any
        ) -> SendResult:
            seen.append((channel_id, text))
            return SendResult(
                success=delivered, platform=self.id, channel_id=channel_id
            )

    class Service:
        def _resolve_engine(self) -> object:
            return engine

    async def reply(bound_engine: object, text: str, *, session: str) -> str:
        seen.append((bound_engine, text, session))
        return "reply text"

    async def retry(bound_engine: object, send: Any) -> int:
        assert bound_engine is engine
        seen.append(
            await send(
                {
                    "platform": "telegram",
                    "channel_id": "42",
                    "text": "hello",
                    "session": "messaging:telegram:42",
                }
            )
        )
        router._running = False
        return int(delivered)

    router._backends.append(cast(Any, Backend()))
    monkeypatch.setenv("MESSAGING_INBOX_RETRY_S", "0")
    monkeypatch.setattr(MessagingService, "instance", lambda: Service())
    bind_host_ports(turn_handler=reply)
    monkeypatch.setattr(inbox, "retry_unanswered", retry)

    await router._inbox_reaper_loop()
    assert seen == [
        (engine, "hello", "messaging:telegram:42"),
        ("42", "reply text"),
        delivered,
    ]
