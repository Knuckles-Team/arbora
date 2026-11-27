"""ntfy push topics channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``MESSAGING_NTFY_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class NtfyBackend(HttpChannelBackend):
    """ntfy push topics backend."""

    platform: ClassVar[str] = "ntfy"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = ""

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"topic": channel_id, "message": text}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if raw.get("event") != "message":
            return None
        return {
            "channel_id": raw.get("topic", ""),
            "user_id": raw.get("topic", ""),
            "text": raw.get("message", ""),
            "id": raw.get("id", ""),
        }
