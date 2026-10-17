"""The default inbound handler: filter, loop delivery, commands, one reply per burst."""

from __future__ import annotations

from typing import Any

import pytest

from graph_os.messaging import planner_handler
from graph_os.messaging.host_ports import bind_host_ports
from graph_os.messaging.models import EventType, InboundEvent, Message
from graph_os.messaging.service import MessagingService

from ._fakes import FakeMessagingBackend

pytestmark = pytest.mark.asyncio


class _Service:
    def __init__(self, waiting: bool = False) -> None:
        self.waiting = waiting

    def _resolve_engine(self) -> str:
        return "engine"

    def deliver_reply(self, *_args: Any) -> bool:
        return self.waiting


def _event(text: str, kind: EventType = EventType.MESSAGE) -> InboundEvent:
    message = Message(id="m1", content=text, channel_id="42", platform="telegram")
    return InboundEvent(
        event_type=kind,
        platform="telegram",
        channel_id="42",
        message=message,
        content=text,
    )


@pytest.fixture
def turns(monkeypatch) -> list[tuple[Any, str, str]]:
    seen: list[tuple[Any, str, str]] = []

    async def turn(engine: Any, text: str, *, session: str) -> str:
        seen.append((engine, text, session))
        return f"re: {text}"

    bind_host_ports(turn_handler=turn)
    return seen


async def _handler(monkeypatch, timer, *, waiting: bool = False):
    monkeypatch.setattr(
        MessagingService, "instance", lambda _engine=None: _Service(waiting)
    )
    return await planner_handler.create_planner_handler(
        clock=timer.clock, sleep=timer.sleep
    )


async def test_burst_becomes_one_turn_and_one_threaded_reply(
    monkeypatch, timer, turns
) -> None:
    handler = await _handler(monkeypatch, timer)
    backend = FakeMessagingBackend()
    await handler(_event("hello"), backend)
    await handler(_event("again"), backend)
    await timer.advance(3.0)
    assert turns == [("engine", "hello\nagain", "messaging:telegram:42")]
    assert backend.replies == [("42", "m1", "re: hello\nagain")]


@pytest.mark.parametrize(
    ("event", "waiting", "replies"),
    [
        (_event("hi", EventType.REACTION_ADD), False, []),
        (_event(""), False, []),
        (_event("an answer"), True, []),
        (_event("/skill deploy"), False, [("42", "m1", "re: /skill deploy")]),
    ],
    ids=["not-a-message", "empty", "goal-loop-answer", "agent-command-falls-through"],
)
async def test_events_that_do_not_start_a_turn(
    monkeypatch, timer, turns, event, waiting, replies
) -> None:
    handler = await _handler(monkeypatch, timer, waiting=waiting)
    backend = FakeMessagingBackend()
    await handler(event, backend)
    await timer.advance(3.0)
    assert backend.replies == replies


async def test_builtin_command_is_answered_without_a_turn(
    monkeypatch, timer, turns
) -> None:
    handler = await _handler(monkeypatch, timer)
    backend = FakeMessagingBackend()
    await handler(_event("/help"), backend)
    await timer.advance(3.0)
    assert turns == []
    assert backend.replies[0][2].startswith("I'm your")


async def test_no_bound_turn_handler_sends_nothing(monkeypatch, timer) -> None:
    handler = await _handler(monkeypatch, timer)
    backend = FakeMessagingBackend()
    await handler(_event("hello"), backend)
    await timer.advance(3.0)
    assert backend.replies == [] and backend.sent == []
