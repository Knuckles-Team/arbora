"""WeCom channel adapter (ARBORA-MESSAGING-R009).

Inbound is webhook based: the shared ingress fills the queue after verify_inbound accepts the signature.
Credentials: ``MESSAGING_WECOM_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class WeComBackend(HttpChannelBackend):
    """WeCom backend."""

    platform: ClassVar[str] = "wecom"
    inbound_mode: ClassVar[str] = "webhook"
    send_path: ClassVar[str] = "/cgi-bin/message/send"
    auth_param: ClassVar[str] = "access_token"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {"touser": channel_id, "msgtype": "text", "text": {"content": text}}

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("FromUserName", ""),
            "user_id": raw.get("FromUserName", ""),
            "text": raw.get("Content", ""),
            "id": str(raw.get("MsgId", "")),
        }
