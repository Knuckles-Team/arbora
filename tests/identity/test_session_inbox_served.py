"""Authenticated session inbox operations (ARBORA-IDENTITY-R026.8)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import messaging
from tests.identity._session_fakes import Engine, caller, context


@pytest.mark.spec("ARBORA-IDENTITY-R026.8")
def test_authorized_delivery_and_zero_enqueue_on_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Engine().install(monkeypatch)
    engine.add_session("person:ada", "a1")
    engine.add_session("person:ada", "a2")
    engine.add_session("person:bob", "b1")
    ada = context(caller("person:ada"))
    send = {"recipient_session_id": "a2", "body": "hello"}
    asyncio.run(messaging.handle_inbox_send(ada, send, None))
    assert len(ada.events) == 1 and ada.events[0]["sender"] == "person:ada"
    read = asyncio.run(messaging.handle_inbox_read(ada, {"session_id": "a2"}, None))
    assert len(read["value"]["messages"]) == 1
    before = len(engine.topics.get("inbox/tenant:one/b1", []))
    for target, code in (
        ("b1", "INBOX_SEND_DENIED"),
        ("zz", "INBOX_RECIPIENT_UNKNOWN"),
    ):
        with pytest.raises(OperationRefused) as refused:
            asyncio.run(
                messaging.handle_inbox_send(
                    ada, {"recipient_session_id": target, "body": "x"}, None
                )
            )
        assert refused.value.code == code
    assert len(engine.topics.get("inbox/tenant:one/b1", [])) == before == 0
    with pytest.raises(OperationRefused):  # another principal's inbox is not readable
        asyncio.run(messaging.handle_inbox_read(ada, {"session_id": "b1"}, None))
    ids = {op.id for op in messaging.operations()}
    assert {"messaging.inbox.send", "messaging.inbox.read"} <= ids
