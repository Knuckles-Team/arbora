"""Yuanbao channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``setting('MESSAGING_YUANBAO_TOKEN')``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class YuanbaoBackend(HttpChannelBackend):
    """Yuanbao backend."""

    platform: ClassVar[str] = "yuanbao"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/v1/bot/messages"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"conversation": channel_id, "text": text}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("conversation", ""),
            "user_id": raw.get("user", ""),
            "text": raw.get("text", ""),
            "id": raw.get("id", ""),
        }
