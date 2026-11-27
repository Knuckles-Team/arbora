"""Hub federation: forward a topic message group to a registered peer (ARBORA-A2A-R024).

The server reads a message group from the engine stream of a topic (``StreamRead``),
forwards it to a peer hub and appends the reply to the same topic (``StreamPublish``).
The peer comes from the tenant's peer registry (``ARBORA-A2A-R001.7``). The forwarding
scope is the intersection of the caller's topic scope and the peer's registered scope:
a peer registered wider than the caller is refused ``FEDERATION_SCOPE_WIDER``. The
transport and its retries are arbora's; the engine stores only the topic log. The only
engine write is the one ``StreamPublish`` of the reply.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

__all__ = [
    "FEDERATION_SCOPE_WIDER",
    "FederationRefused",
    "HubFederation",
    "PeerTransport",
    "topic_stream",
]

FEDERATION_SCOPE_WIDER = "FEDERATION_SCOPE_WIDER"
_MAX_GROUP = 200

#: ``send(method, params, idempotency_key)`` returns the engine payload.
EngineSend = Callable[[str, dict[str, Any], str | None], Awaitable[Any]]


class FederationRefused(Exception):
    """One closed refusal code; nothing was read from the peer or appended."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PeerTransport(Protocol):
    """The peer-facing transport: arbora owns it and its retries."""

    async def forward(
        self, peer: Mapping[str, Any], messages: Sequence[bytes]
    ) -> Sequence[bytes]: ...


def topic_stream(tenant: str, topic: str) -> str:
    """The engine stream of one tenant's topic."""
    return f"topic.{tenant}.{topic}"


def _covers(scope: Sequence[str], topic: str) -> bool:
    return any(topic == prefix or topic.startswith(prefix + ".") for prefix in scope)


class HubFederation:
    """Read a group, forward it to one peer and append the peer's reply."""

    def __init__(
        self,
        send: EngineSend,
        peers: Callable[[str, str], Mapping[str, Any] | None],
        transport: PeerTransport | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._send = send
        self._peers = peers
        self._transport = transport
        self._clock = clock

    def _check_scope(
        self, peer: Mapping[str, Any] | None, caller_scope: Sequence[str], topic: str
    ) -> Mapping[str, Any]:
        if peer is None:
            # A foreign tenant's peer answers exactly like an absent one.
            raise FederationRefused("PEER_UNKNOWN")
        peer_scope = list(peer.get("topic_scope", ()))
        if any(not _covers(caller_scope, prefix) for prefix in peer_scope):
            raise FederationRefused(FEDERATION_SCOPE_WIDER)
        if not (_covers(caller_scope, topic) and _covers(peer_scope, topic)):
            raise FederationRefused("TOPIC_NOT_IN_SCOPE")
        return peer

    async def forward(
        self,
        *,
        tenant: str,
        peer_name: str,
        topic: str,
        caller_scope: Sequence[str],
        from_offset: int = 0,
        limit: int = _MAX_GROUP,
    ) -> dict[str, Any]:
        peer = self._check_scope(self._peers(tenant, peer_name), caller_scope, topic)
        if self._transport is None:
            raise FederationRefused("TRANSPORT_UNAVAILABLE")
        stream = topic_stream(tenant, topic)
        rows = await self._send(
            "StreamRead",
            {
                "stream": stream,
                "from_offset": from_offset,
                "max": max(1, min(limit, _MAX_GROUP)),
            },
            None,
        )
        group = [bytes(payload) for _offset, payload in getattr(rows, "payload", rows)]
        if not group:
            return {"forwarded": 0, "replies": 0}
        replies = await self._transport.forward(peer, group)
        for reply in replies:
            await self._send(
                "StreamPublish",
                {
                    "stream": stream,
                    "payload": bytes(reply),
                    "now_ms": int(self._clock() * 1000),
                },
                None,
            )
        return {"forwarded": len(group), "replies": len(replies)}
