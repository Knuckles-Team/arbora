"""A trust-flag event notifies the principals whose stored query reads the asset (ARBORA-OPS-R855.3.1)."""

from __future__ import annotations

import asyncio
from typing import Any

import msgpack
import pytest

from arbora.api.domain.fleet.outbox_consumer import (
    TRUST_FLAG_TOPIC,
    consume_topic,
    trust_flag_handler,
)
from arbora.messaging.action_dispatch import NotificationSpec


class Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, spec: NotificationSpec, message: str) -> dict[str, Any]:
        self.sent.append((spec.recipient, message))
        return {"delivered": True}


class Engine:
    def __init__(self, readers: list[str]) -> None:
        self.readers, self.acked = readers, 0
        self.calls: list[Any] = []
        event = {"asset": "asset:orders", "flag": "deprecation"}
        self.lease = {
            "record": {
                "intent": {
                    "topic": TRUST_FLAG_TOPIC,
                    "key": "k",
                    "headers": {},
                    "payload": msgpack.packb(event),
                }
            }
        }

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "Uql":
            return {"rows": [{"principal": p} for p in self.readers]}
        if params["op"]["op"] == "pull":
            return {"leases": [self.lease] if not self.acked else []}
        self.acked += 1
        return {}


# spec: ARBORA-OPS-R855.3.1
def test_deprecation_event_notifies_reading_principals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    # The structural double of a re-pinned engine: both outbox shapes declared.
    monkeypatch.setattr(models, "MutationOutboxOpPull", object, raising=False)
    monkeypatch.setattr(models, "MutationOutboxOpAck", object, raising=False)
    engine, notifier = Engine(["p:ann", "p:bob", "p:ann"]), Notifier()

    count = asyncio.run(
        consume_topic(
            engine.send,
            graph="g",
            consumer="trust-flags",
            topic=TRUST_FLAG_TOPIC,
            handle=trust_flag_handler(engine.send, notifier),
        )
    )

    assert count == 1 and engine.acked == 1
    # The engine was asked with the asset as a bound parameter, not spliced text.
    (uql,) = [p for m, p in engine.calls if m == "Uql"]
    assert uql["params"] == {"asset": {"Str": "asset:orders"}}
    assert "asset:orders" not in uql["text"]
    # One notice per distinct reading principal.
    assert sorted(r for r, _ in notifier.sent) == ["p:ann", "p:bob"]
    assert all("asset:orders" in m and "deprecation" in m for _, m in notifier.sent)

    # An event nobody reads is still acknowledged and notifies no one.
    quiet, other = Engine([]), Notifier()
    asyncio.run(
        consume_topic(
            quiet.send,
            graph="g",
            consumer="trust-flags",
            topic=TRUST_FLAG_TOPIC,
            handle=trust_flag_handler(quiet.send, other),
        )
    )
    assert quiet.acked == 1 and other.sent == []
