"""Buzz community relay channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``MESSAGING_BUZZ_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class BuzzBackend(HttpChannelBackend):
    """Buzz community relay backend."""

    platform: ClassVar[str] = "buzz"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/events/send"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "room": channel_id,
            "content": text,
            **({"thread": thread_id} if thread_id else {}),
            **({"in_reply_to": reply_to_id} if reply_to_id else {}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("room", ""),
            "user_id": raw.get("pubkey", ""),
            "text": raw.get("content", ""),
            "id": raw.get("id", ""),
            "thread_id": raw.get("thread", ""),
        }
