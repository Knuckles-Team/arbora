"""BlueBubbles iMessage relay channel adapter (ARBORA-MESSAGING-R009).

Inbound is webhook based: the shared ingress fills the queue after verify_inbound accepts the signature.
Credentials: ``MESSAGING_BLUEBUBBLES_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class BlueBubblesBackend(HttpChannelBackend):
    """BlueBubbles iMessage relay backend."""

    platform: ClassVar[str] = "bluebubbles"
    inbound_mode: ClassVar[str] = "webhook"
    send_path: ClassVar[str] = "/api/v1/message/text"
    auth_param: ClassVar[str] = "password"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "chatGuid": channel_id,
            "message": text,
            "method": "private-api",
            **({"selectedMessageGuid": reply_to_id} if reply_to_id else {}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        data = raw.get("data") or {}
        chats = data.get("chats") or [{}]
        return {
            "channel_id": chats[0].get("guid", ""),
            "user_id": (data.get("handle") or {}).get("address", ""),
            "text": data.get("text", ""),
            "id": data.get("guid", ""),
            "thread_id": data.get("threadOriginatorGuid", ""),
        }
