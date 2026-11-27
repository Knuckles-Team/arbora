"""generic webhook channel adapter (ARBORA-MESSAGING-R009).

Inbound is webhook based: the shared ingress fills the queue after verify_inbound accepts the signature.
Credentials: ``MESSAGING_WEBHOOK_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class WebhookBackend(HttpChannelBackend):
    """generic webhook backend."""

    platform: ClassVar[str] = "webhook"
    inbound_mode: ClassVar[str] = "webhook"
    send_path: ClassVar[str] = ""

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"to": channel_id, "text": text}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("channel_id", ""),
            "user_id": raw.get("sender", ""),
            "text": raw.get("text", ""),
            "id": raw.get("id", ""),
        }
