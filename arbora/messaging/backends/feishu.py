"""Feishu and Lark channel adapter (ARBORA-MESSAGING-R009).

Inbound is webhook based: the shared ingress fills the queue after verify_inbound accepts the signature.
Credentials: ``MESSAGING_FEISHU_TOKEN``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class FeishuBackend(HttpChannelBackend):
    """Feishu and Lark backend."""

    platform: ClassVar[str] = "feishu"
    inbound_mode: ClassVar[str] = "webhook"
    send_path: ClassVar[str] = "/open-apis/im/v1/messages"
    reply_path: ClassVar[str] = "/open-apis/im/v1/messages/{message_id}/reply"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "receive_id": channel_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        event = raw.get("event") or {}
        message = event.get("message") or {}
        try:
            text = json.loads(message.get("content") or "{}").get("text", "")
        except ValueError:
            text = ""
        return {
            "channel_id": message.get("chat_id", ""),
            "user_id": ((event.get("sender") or {}).get("sender_id") or {}).get(
                "open_id", ""
            ),
            "text": text,
            "id": message.get("message_id", ""),
            "thread_id": message.get("root_id", ""),
        }
