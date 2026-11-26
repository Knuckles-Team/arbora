"""Signed webhook delivery with retries (ARBORA-MESSAGING-R700.2, R700.4).

``deliver_with_retry`` signs one payload and sends it through ``send_webhook``
of ``action_dispatch`` for each attempt; it adds no second transport. A 2xx
status ends delivery. Any other status or a transport error retries after
``min(retry_cap_s, retry_base_s * 2**attempt)`` seconds. After
``retry_max_attempts`` attempts the delivery is dead and one record goes to the
KV namespace ``webhook_dead_letters``. Each delivery writes one journal entry
``{delivery_id, subscription_id, topic, outcome, attempts, status_code}`` that
holds no payload. The secret never enters a log.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.messaging.action_dispatch import HttpPost, WebhookSpec, send_webhook

logger = logging.getLogger(__name__)

DEAD_LETTER_NAMESPACE = "webhook_dead_letters"
RETRY_MAX_ATTEMPTS = (5, (1, 12))
RETRY_BASE_S = (2.0, (0.5, 60.0))
RETRY_CAP_S = (300.0, (1.0, 3600.0))
TIMEOUT_S = (10.0, (1.0, 60.0))

Sleep = Callable[[float], Awaitable[None]]
DeadLetter = Callable[[dict[str, Any]], Awaitable[None]]
Journal = Callable[[dict[str, Any]], None]


def clamp_setting(value: Any, spec: tuple[Any, tuple[Any, Any]]) -> Any:
    """``value`` limited to the range of ``spec``; the default when unreadable."""
    default, (low, high) = spec
    try:
        number = type(default)(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 over ``<timestamp>.<body>``."""
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def canonical_body(payload: dict[str, Any]) -> bytes:
    """The exact bytes that are signed and sent."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def delivery_id(subscription_id: str, event_key: str) -> str:
    """A stable id per event and subscription, so a receiver can deduplicate."""
    return hashlib.sha256(f"{subscription_id}:{event_key}".encode()).hexdigest()[:32]


@dataclasses.dataclass(frozen=True)
class DeliveryRequest:
    """One delivery of one event to one subscription."""

    url: str
    secret: str = dataclasses.field(repr=False)
    subscription_id: str
    topic: str
    event_key: str
    payload: dict[str, Any]
    post: HttpPost | None
    dead_letter: DeadLetter
    journal: Journal
    sleep: Sleep = asyncio.sleep
    clock: Callable[[], float] = time.time
    retry_max_attempts: int = 5
    retry_base_s: float = 2.0
    retry_cap_s: float = 300.0


async def deliver_with_retry(request: DeliveryRequest) -> str:
    """Return ``"delivered"`` or ``"dead"`` after at most ``retry_max_attempts`` tries."""
    body = canonical_body(request.payload)
    ident = delivery_id(request.subscription_id, request.event_key)
    status_code = 0
    attempts = 0
    outcome = "dead"
    while attempts < request.retry_max_attempts:
        stamp = str(int(request.clock()))
        spec = WebhookSpec(
            url=request.url,
            headers={
                "X-Arbora-Signature": sign_payload(request.secret, stamp, body),
                "X-Arbora-Timestamp": stamp,
                "X-Arbora-Event": request.topic,
                "X-Arbora-Delivery": ident,
                "Content-Type": "application/json",
            },
        )
        record = await asyncio.to_thread(
            send_webhook, spec, request.payload, request.post, request.clock
        )
        attempts += 1
        status_code = int(record.get("status_code", 0) or 0)
        if record.get("delivered"):
            outcome = "delivered"
            break
        if attempts < request.retry_max_attempts:
            await request.sleep(
                min(request.retry_cap_s, request.retry_base_s * 2 ** (attempts - 1))
            )
    entry = {
        "delivery_id": ident,
        "subscription_id": request.subscription_id,
        "topic": request.topic,
        "outcome": outcome,
        "attempts": attempts,
        "status_code": status_code,
    }
    request.journal(entry)
    if outcome == "dead":
        await request.dead_letter(
            {**entry, "event_key": request.event_key, "failed_at": request.clock()}
        )
        logger.warning("webhook delivery dead: %s", ident)
    return outcome
