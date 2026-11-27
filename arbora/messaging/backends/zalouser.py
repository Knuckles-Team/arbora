"""Zalo personal account channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``MESSAGING_ZALOUSER_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class ZaloUserBackend(HttpChannelBackend):
    """Zalo personal account backend."""

    platform: ClassVar[str] = "zalouser"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/v1/messages"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "to": channel_id,
            "text": text,
            **({"quote": reply_to_id} if reply_to_id else {}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("thread", ""),
            "user_id": raw.get("from", ""),
            "text": raw.get("text", ""),
            "id": raw.get("id", ""),
        }
