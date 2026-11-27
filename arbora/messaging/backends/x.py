"""X mentions channel adapter (ARBORA-MESSAGING-R009).

Inbound is polled on a bounded interval with a cursor.
Credentials: ``MESSAGING_X_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class XBackend(HttpChannelBackend):
    """X mentions backend."""

    platform: ClassVar[str] = "x"
    inbound_mode: ClassVar[str] = "poll"
    send_path: ClassVar[str] = "/2/tweets"
    poll_path: ClassVar[str] = "/2/mentions"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "text": text,
            **({"reply": {"in_reply_to_tweet_id": reply_to_id}} if reply_to_id else {}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("author_id", ""),
            "user_id": raw.get("author_id", ""),
            "text": raw.get("text", ""),
            "id": raw.get("id", ""),
            "thread_id": raw.get("conversation_id", ""),
        }
