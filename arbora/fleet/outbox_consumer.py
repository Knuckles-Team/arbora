"""Scheduled consumers of engine outbox topics (ARBORA-OPS-R855.3.1, R918.6).

The engine owns the outbox and its cursor: a consumer pulls leases for its
topic, handles each event and acknowledges it. A handler that raises, or a
notice that does not leave, leaves the lease unacknowledged, so the event comes
back from the outbox and nothing is lost. Arbora keeps no subscription table
and no state of its own beyond the engine's per-consumer cursor.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import msgpack

from arbora.api.ops._engine import decode, unavailable
from arbora.messaging.action_dispatch import (
    NotificationSpec,
    Notifier,
    send_notification,
)

__all__ = [
    "NoticeFailed",
    "OutboxConsumer",
    "tick",
    "OutboxEvent",
    "TRUST_FLAG_TOPIC",
    "consume_topic",
    "notify_each",
    "trust_flag_handler",
]

TRUST_FLAG_TOPIC = "catalog.trust_flag"

#: ``send(method, params)`` reaches the engine as the bound service client.
Send = Callable[[str, dict[str, Any]], Awaitable[Any]]
Handler = Callable[["OutboxEvent"], Awaitable[None]]


class NoticeFailed(RuntimeError):
    """A notice did not leave; the event stays in the outbox for a retry."""


class OutboxEvent:
    """One decoded outbox record."""

    __slots__ = ("headers", "key", "payload", "topic")

    def __init__(
        self,
        topic: str,
        key: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> None:
        self.topic, self.key = topic, key
        self.headers, self.payload = dict(headers), dict(payload)


def _payload(raw: Any) -> dict[str, Any]:
    data = bytes(raw) if isinstance(raw, (bytes, bytearray, list, tuple)) else raw
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, bytes):
        raise ValueError("outbox payload is not bytes")
    try:
        value = json.loads(data)
    except ValueError:
        value = msgpack.unpackb(data, raw=False)
    if not isinstance(value, dict):
        raise ValueError("outbox payload is not an object")
    return value


def _event(lease: Mapping[str, Any]) -> OutboxEvent:
    intent = lease["record"]["intent"]
    return OutboxEvent(
        str(intent["topic"]),
        str(intent["key"]),
        intent.get("headers") or {},
        _payload(intent["payload"]),
    )


def _pull_served() -> bool:
    from medulla.generated import models

    return hasattr(models, "MutationOutboxOpPull") and hasattr(
        models, "MutationOutboxOpAck"
    )


async def consume_topic(
    send: Send,
    *,
    graph: str,
    consumer: str,
    topic: str,
    handle: Handler,
    limit: int = 50,
) -> int:
    """Pull one batch of ``topic``, handle each event, acknowledge the handled.

    Returns the number acknowledged. Refuses ``UNAVAILABLE`` while the pinned
    engine release does not serve outbox pull and acknowledge. A handler failure
    stops the batch at that event: the earlier events stay acknowledged and the
    failed one is pulled again.
    """
    if not _pull_served():
        raise unavailable("the installed engine does not serve outbox pull and ack")
    target = {"target": "graph", "graph": graph}
    pulled = decode(
        await send(
            "MutationOutbox",
            {
                "op": {
                    "op": "pull",
                    "consumer": consumer,
                    "limit": limit,
                    "target": target,
                    "topics": [topic],
                }
            },
        )
    )
    acknowledged = 0
    for lease in (pulled or {}).get("leases") or ():
        try:
            await handle(_event(lease))
        except NoticeFailed:
            break
        await send(
            "MutationOutbox",
            {
                "op": {
                    "op": "ack",
                    "consumer": consumer,
                    "lease": lease,
                    "target": target,
                }
            },
        )
        acknowledged += 1
    return acknowledged


@dataclass(frozen=True, slots=True)
class OutboxConsumer:
    """One topic, the engine consumer name that holds its cursor, and its handler."""

    topic: str
    consumer: str
    handle: Handler


async def tick(
    consumers: Sequence[OutboxConsumer], send: Send, *, graph: str
) -> dict[str, int]:
    """Run every consumer once; a consumer the engine cannot serve yet counts zero."""
    from arbora.api.invoke.pipeline import OperationRefused

    done: dict[str, int] = {}
    for item in consumers:
        try:
            done[item.topic] = await consume_topic(
                send,
                graph=graph,
                consumer=item.consumer,
                topic=item.topic,
                handle=item.handle,
            )
        except OperationRefused as refused:
            if refused.code != "UNAVAILABLE":
                raise
            done[item.topic] = 0
    return done


def notify_each(
    notifier: Notifier, recipients: list[str], message: str, *, channel: str = "default"
) -> int:
    """Send one notice to each distinct recipient; raise if any did not leave."""
    failed = 0
    sent = 0
    for recipient in sorted(set(recipients)):
        record = send_notification(
            NotificationSpec(channel=channel, recipient=recipient), message, notifier
        )
        if record.get("error"):
            failed += 1
        else:
            sent += 1
    if failed:
        raise NoticeFailed(f"{failed} notice(s) did not leave")
    return sent


#: UQL for the principals whose stored query reads one asset.
READERS_UQL = (
    "MATCH (q:StoredQuery) WHERE q.text CONTAINS $asset RETURN q.author AS principal"
)


def trust_flag_handler(send: Send, notifier: Notifier) -> Handler:
    """Notify every principal whose stored query reads a warned or deprecated asset."""

    async def handle(event: OutboxEvent) -> None:
        asset = event.payload.get("asset")
        flag = event.payload.get("flag")
        if not isinstance(asset, str) or not asset:
            return
        answer = decode(
            await send(
                "Uql", {"text": READERS_UQL, "params": {"asset": {"Str": asset}}}
            )
        )
        rows = (answer or {}).get("rows") or ()
        principals = [
            str(row["principal"])
            for row in rows
            if isinstance(row, Mapping) and row.get("principal")
        ]
        notify_each(
            notifier,
            principals,
            f"Asset {asset} was flagged {flag}; a stored query of yours reads it.",
        )

    return handle
