"""Notification and webhook dispatch for completed ontology Actions.

The engine applies an Action's effects and emits an outbox event; this module
delivers the notification and webhook side. Every dispatch returns a plain
outcome record, and a failed or unconfigured channel is recorded rather than
raised, so a dispatch never blocks the Action that triggered it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "NotificationSpec",
    "Notifier",
    "RecordingNotifier",
    "WebhookSpec",
    "send_notification",
    "send_webhook",
]

HttpPost = Callable[[str, str, dict[str, Any], dict[str, str]], int]


class NotificationSpec(BaseModel):
    channel: str = "default"
    recipient: str = ""
    template: str = ""


class WebhookSpec(BaseModel):
    url: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    def notify(self, spec: NotificationSpec, message: str) -> dict[str, Any]: ...


class RecordingNotifier:
    """Journals each notification; delivery waits for a live channel."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.records: list[dict[str, Any]] = []
        self._clock = clock

    def notify(self, spec: NotificationSpec, message: str) -> dict[str, Any]:
        record = {
            "kind": "notification",
            "channel": spec.channel,
            "recipient": spec.recipient,
            "message": message,
            "delivered": False,
            "transport": "recorded",
            "timestamp": self._clock(),
        }
        self.records.append(record)
        return record


def send_notification(
    spec: NotificationSpec,
    message: str,
    notifier: Notifier,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Deliver through ``notifier``; a raising sink yields a recorded error."""
    try:
        return notifier.notify(spec, message)
    except Exception as exc:  # noqa: BLE001 - dispatch never blocks the action
        return {
            "kind": "notification",
            "channel": spec.channel,
            "recipient": spec.recipient,
            "message": message,
            "delivered": False,
            "transport": "error",
            "error": str(exc),
            "timestamp": clock(),
        }


def send_webhook(
    spec: WebhookSpec,
    payload: dict[str, Any],
    post: HttpPost | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Send through ``post`` (method, url, body, headers -> status), or record.

    Without a transport the attempt is journaled with ``transport="recorded"``.
    """
    body = {**spec.payload, **payload}
    base = {
        "kind": "webhook",
        "url": spec.url,
        "method": spec.method,
        "payload": body,
        "timestamp": clock(),
    }
    if post is None:
        return {**base, "delivered": False, "transport": "recorded"}
    try:
        status = post(spec.method, spec.url, body, spec.headers)
    except Exception as exc:  # noqa: BLE001 - network failure is captured
        return {**base, "delivered": False, "transport": "http", "error": str(exc)}
    return {
        **base,
        "delivered": 200 <= status < 300,
        "transport": "http",
        "status_code": status,
    }
