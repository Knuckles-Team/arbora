"""DingTalk channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``MESSAGING_DINGTALK_TOKEN``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class DingTalkBackend(HttpChannelBackend):
    """DingTalk backend."""

    platform: ClassVar[str] = "dingtalk"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/v1.0/robot/groupMessages/send"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "openConversationId": channel_id,
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": text}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("conversationId", ""),
            "user_id": raw.get("senderStaffId", ""),
            "text": (raw.get("text") or {}).get("content", ""),
            "id": raw.get("msgId", ""),
        }
