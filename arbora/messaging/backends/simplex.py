"""SimpleX Chat channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``MESSAGING_SIMPLEX_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class SimplexBackend(HttpChannelBackend):
    """SimpleX Chat backend."""

    platform: ClassVar[str] = "simplex"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/cmd"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"corrId": "arbora", "cmd": f"@{channel_id} {text}"}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        item = (raw.get("resp") or {}).get("chatItem") or {}
        content = ((item.get("chatItem") or {}).get("content") or {}).get(
            "msgContent"
        ) or {}
        contact = ((item.get("chatInfo") or {}).get("contact") or {}).get(
            "localDisplayName", ""
        )
        return {
            "channel_id": contact,
            "user_id": contact,
            "text": content.get("text", ""),
            "id": str((item.get("chatItem") or {}).get("meta", {}).get("itemId", "")),
        }
