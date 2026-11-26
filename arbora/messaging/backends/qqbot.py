"""QQ Bot channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``setting('MESSAGING_QQBOT_TOKEN')``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class QQBotBackend(HttpChannelBackend):
    """QQ Bot backend."""

    platform: ClassVar[str] = "qqbot"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/channels/{channel_id}/messages"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"content": text, **({"msg_id": reply_to_id} if reply_to_id else {})}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        data = raw.get("d") or {}
        return {
            "channel_id": data.get("channel_id") or data.get("group_openid", ""),
            "user_id": (data.get("author") or {}).get("id", ""),
            "text": data.get("content", ""),
            "id": data.get("id", ""),
        }
