"""SMS channel adapter (ARBORA-MESSAGING-R009).

Inbound is webhook based: the shared ingress fills the queue after verify_inbound accepts the signature.
Credentials: ``setting('MESSAGING_SMS_TOKEN')``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class SmsBackend(HttpChannelBackend):
    """SMS backend."""

    platform: ClassVar[str] = "sms"
    inbound_mode: ClassVar[str] = "webhook"
    send_path: ClassVar[str] = "/messages"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"to": channel_id, "body": text}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("from", ""),
            "user_id": raw.get("from", ""),
            "text": raw.get("body", ""),
            "id": raw.get("id", ""),
        }
