"""Raft workspace channel adapter (ARBORA-MESSAGING-R009).

Inbound is a long-lived outbound connection with a bounded reconnect backoff.
Credentials: ``setting('MESSAGING_RAFT_TOKEN')``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from arbora.messaging.backends._channel_http import HttpChannelBackend


class RaftBackend(HttpChannelBackend):
    """Raft workspace backend."""

    platform: ClassVar[str] = "raft"
    inbound_mode: ClassVar[str] = "stream"
    send_path: ClassVar[str] = "/bridge/messages"

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        return {
            "channel": channel_id,
            "text": text,
            **({"thread": thread_id} if thread_id else {}),
        }

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return {
            "channel_id": raw.get("channel", ""),
            "user_id": raw.get("author", ""),
            "text": raw.get("text", ""),
            "id": raw.get("id", ""),
            "thread_id": raw.get("thread", ""),
        }
