"""The hub-federation transport to a registered peer (ARBORA-A2A-R024).

A peer is called through the one A2A client of the SDK (``scion_sdk.a2a.A2AClient``):
each message of the group is one ``message/send`` to the peer's endpoint, and the
peer's text reply is one reply. The peer's credential is only a reference in the
registry (``ARBORA-A2A-R001.7``); it is resolved here, per forward, and rides as a
bearer header on the client's HTTP transport. Nothing else holds it.

Retries are bounded and belong to arbora. A forward is all or nothing: one message
that the peer never answers refuses the whole group, so nothing is appended to the
topic for a half-answered group.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from .hub_federation import FederationRefused

__all__ = [
    "A2APeerTransport",
    "PEER_CREDENTIAL_UNAVAILABLE",
    "PEER_UNREACHABLE",
    "runtime_credential",
]

PEER_CREDENTIAL_UNAVAILABLE = "PEER_CREDENTIAL_UNAVAILABLE"
PEER_UNREACHABLE = "PEER_UNREACHABLE"
MESSAGE_NOT_TEXT = "MESSAGE_NOT_TEXT"
DEFAULT_ATTEMPTS = 3
_BACKOFF_S = 1.0

#: Resolve one credential reference to its bearer value; raises when unavailable.
ResolveCredential = Callable[[str], str]


def runtime_credential(reference: str) -> str:
    """Resolve a registry reference (``openbao:kv/x``) through the SDK secret resolver."""
    from scion_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    scheme, _sep, target = reference.partition(":")
    scheme = "vault" if scheme == "openbao" else scheme
    return resolve_runtime_secret_reference(f"{scheme}://{target}")


class _Bearer(httpx.AsyncBaseTransport):
    """Add the peer's bearer credential to every request of one client."""

    def __init__(self, token: str, inner: httpx.AsyncBaseTransport) -> None:
        self._token = token
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["Authorization"] = f"Bearer {self._token}"
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class A2APeerTransport:
    """Forward a message group to one peer hub over the SDK A2A client."""

    def __init__(
        self,
        *,
        resolve_credential: ResolveCredential = runtime_credential,
        inner: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        attempts: int = DEFAULT_ATTEMPTS,
        timeout_s: float = 60.0,
    ) -> None:
        if not 1 <= attempts <= 10:
            raise ValueError("attempts must be within [1, 10]")
        self._resolve = resolve_credential
        self._inner = inner
        self._sleep = sleep
        self._attempts = attempts
        self._timeout = timeout_s

    async def forward(
        self, peer: Mapping[str, Any], messages: Sequence[bytes]
    ) -> list[bytes]:
        from scion_sdk.a2a import A2AClient

        try:
            texts = [bytes(m).decode("utf-8") for m in messages]
        except UnicodeDecodeError:
            raise FederationRefused(MESSAGE_NOT_TEXT) from None
        try:
            token = self._resolve(str(peer["credential_ref"]))
        except Exception:  # never name the reference or the value
            raise FederationRefused(PEER_CREDENTIAL_UNAVAILABLE) from None
        client = A2AClient(
            timeout=self._timeout,
            transport=_Bearer(token, self._inner or httpx.AsyncHTTPTransport()),
        )
        return [await self._send(client, str(peer["endpoint"]), text) for text in texts]

    async def _send(self, client: Any, endpoint: str, text: str) -> bytes:
        for attempt in range(self._attempts):
            result = await client.send(endpoint, text)
            if result.ok and result.content:
                return str(result.content).encode("utf-8")
            if attempt + 1 < self._attempts:
                await self._sleep(_BACKOFF_S * (attempt + 1))
        raise FederationRefused(PEER_UNREACHABLE)
