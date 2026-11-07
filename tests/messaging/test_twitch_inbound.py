"""An inbound Twitch chat message reaches the backend's event queue.

The bot class is defined inside ``TwitchBackend.connect``; its handler must
put events on the backend's queue, not look the queue up on the bot.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from arbora.messaging.backends import twitch
from arbora.messaging.models import MessagingConfig, PlatformId

pytestmark = pytest.mark.spec("ARBORA-MESSAGING-R800.1")


class _Bot:
    created: list[_Bot] = []

    def __init__(self, **kwargs: Any) -> None:
        _Bot.created.append(self)

    async def start(self) -> None:
        return None


def _fake_twitchio(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = types.ModuleType("twitchio.ext.commands")
    commands.Bot = _Bot  # type: ignore[attr-defined]
    ext = types.ModuleType("twitchio.ext")
    ext.commands = commands  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "twitchio", types.ModuleType("twitchio"))
    monkeypatch.setitem(sys.modules, "twitchio.ext", ext)
    monkeypatch.setitem(sys.modules, "twitchio.ext.commands", commands)


@pytest.mark.asyncio
async def test_inbound_message_is_queued_on_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_twitchio(monkeypatch)
    _Bot.created.clear()

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(twitch.asyncio, "sleep", no_wait)
    backend = twitch.TwitchBackend(
        MessagingConfig(
            token="-".join(["fake", "chat", "login"]), extra={"channels": ["demo"]}
        )
    )
    await backend.connect()
    bot: Any = _Bot.created[-1]
    message = types.SimpleNamespace(
        echo=False,
        id="m1",
        content="hello",
        channel=types.SimpleNamespace(name="demo"),
        author=types.SimpleNamespace(name="viewer", display_name="Viewer"),
    )
    await bot.event_message(message)
    event = await asyncio.wait_for(backend._event_queue.get(), timeout=1.0)
    assert event.content == "hello"
    assert event.platform == PlatformId.TWITCH
    assert event.channel_id == "demo"
