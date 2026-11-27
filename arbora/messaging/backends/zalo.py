"""Zalo Official Account channel adapter (ARBORA-MESSAGING-R009).

Inbound is webhook based: the shared ingress fills the queue after verify_inbound accepts the signature.
Credentials: ``MESSAGING_ZALO_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class ZaloBackend(HttpChannelBackend):
    """Zalo Official Account backend."""

    platform: ClassVar[str] = "zalo"
    inbound_mode: ClassVar[str] = "webhook"
    send_path: ClassVar[str] = "/v3.0/oa/message/cs"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"recipient": {"user_id": channel_id}, "message": {"text": text}}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        message = raw.get("message") or {}
        sender = (raw.get("sender") or {}).get("id", "")
        return {
            "channel_id": sender,
            "user_id": sender,
            "text": message.get("text", ""),
            "id": message.get("msg_id", ""),
        }
