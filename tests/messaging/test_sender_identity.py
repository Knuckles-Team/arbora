"""Per-sender identity for inbound turns (ARBORA-MESSAGING-R007.1)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.messaging import planner_handler
from arbora.messaging.host_ports import bind_host_ports
from arbora.messaging.models import EventType, InboundEvent
from arbora.messaging.sender_identity import (
    SENDER_UNMAPPED,
    TOKEN_EXCHANGE_REFUSED,
    SenderRefused,
    session_for_sender,
)


class _Service:
    def _resolve_engine(self) -> str:
        return "engine"


class _Backend:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        self.sent.append(args)

    async def reply_to(self, *args: Any, **kwargs: Any) -> None:
        self.sent.append(args)


def _event() -> InboundEvent:
    return InboundEvent(
        event_type=EventType.MESSAGE,
        platform="telegram",
        channel_id="42",
        user_id="7",
        content="hi",
    )


@pytest.fixture
def mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_SENDER_PRINCIPALS", '{"telegram:7": "principal-7"}')


@pytest.mark.spec("ARBORA-MESSAGING-R007.1")
async def test_mapped_sender_runs_turn_under_principal(mapped: None) -> None:
    exchanged: list[str] = []
    turns: list[Any] = []

    def exchange(principal: str) -> str:
        exchanged.append(principal)
        return "exchanged-token"

    async def turn(engine: Any, text: str, *, session: Any) -> str:
        turns.append(session)
        return "reply"

    bind_host_ports(turn_handler=turn)
    session = session_for_sender("telegram", "7", exchange=exchange)
    assert (session.principal, session.token) == ("principal-7", "exchanged-token")
    assert exchanged == ["principal-7"]

    # The burst responder takes the exchanged session, not a fixed channel key.
    import arbora.messaging.sender_identity as identity

    identity_exchange = identity._exchange_for_principal
    identity._exchange_for_principal = exchange  # type: ignore[assignment]
    try:
        backend = _Backend()
        responder = planner_handler._burst_responder(_Service())
        await responder("k", [{"event": _event(), "backend": backend, "content": "hi"}])
    finally:
        identity._exchange_for_principal = identity_exchange  # type: ignore[assignment]
    assert turns[0].principal == "principal-7"
    assert turns[0].key == "messaging:telegram:7"
    assert backend.sent == [("42", "reply")]


@pytest.mark.spec("ARBORA-MESSAGING-R007.1")
async def test_refused_exchange_runs_no_turn(
    mapped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns: list[Any] = []

    async def turn(engine: Any, text: str, *, session: Any) -> str:
        turns.append(session)
        return "reply"

    bind_host_ports(turn_handler=turn)

    def refuse(principal: str) -> str:
        raise RuntimeError("exchange denied")

    with pytest.raises(SenderRefused) as refused:
        session_for_sender("telegram", "7", exchange=refuse)
    assert refused.value.code == TOKEN_EXCHANGE_REFUSED
    with pytest.raises(SenderRefused) as unmapped:
        session_for_sender("telegram", "8", exchange=lambda p: "t")
    assert unmapped.value.code == SENDER_UNMAPPED

    monkeypatch.setattr(
        "arbora.messaging.sender_identity._exchange_for_principal", refuse
    )
    backend = _Backend()
    responder = planner_handler._burst_responder(_Service())
    await responder("k", [{"event": _event(), "backend": backend, "content": "hi"}])
    assert turns == [] and backend.sent == []
