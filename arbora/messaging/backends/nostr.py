"""Nostr channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``setting('MESSAGING_NOSTR_TOKEN')``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class NostrBackend(HttpChannelBackend):
    """Nostr backend."""

    platform: ClassVar[str] = "nostr"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/events/dm"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "recipient": channel_id,
            "content": text,
            **({"reply_to": reply_to_id} if reply_to_id else {}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("pubkey", ""),
            "user_id": raw.get("pubkey", ""),
            "text": raw.get("content", ""),
            "id": raw.get("id", ""),
            "thread_id": raw.get("root", ""),
        }
