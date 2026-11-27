"""The PKCE challenge, callback wait and refresh of scoped credential reuse.

``ARBORA-IDENTITY-R023.3``: :class:`BrokerPkceBridge` is the production implementation of
``challenge``, ``complete`` and ``refresh`` of :class:`~arbora.access.credential_use.CredentialUse`.
It drives the remote OAuth broker of one declared provider: ``challenge`` begins one
authorization transaction (PKCE S256, state, 120 s), ``complete`` waits for the gateway's
``/oauth/callback`` to deliver the stored token, and ``refresh`` rotates the stored refresh
token. Nothing here stores a credential: the stored token is handed to the resolver, which
persists it through the host vault before it is ready.
"""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from arbora.access.credential_use import (
    CHALLENGE_DEADLINE_SECONDS,
    AuthorizationChallenge,
    CredentialRefusal,
    CredentialRefused,
    CredentialUseGrant,
    CredentialUseRequest,
)

__all__ = ["CALLBACK_WAITERS", "BrokerPkceBridge", "CallbackWaiters"]


class CallbackWaiters:
    """In-flight callback waits by ``state``; a token is delivered to its waiter once.

    This holds one pending wait per started challenge and nothing durable: a callback that
    lands on another replica, or after the deadline, finds no waiter and the waiting call
    ends ``INPUT_TIMEOUT`` with no usable grant.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[
            str, tuple[asyncio.AbstractEventLoop, asyncio.Future[Mapping[str, Any]]]
        ] = {}

    def expect(self, state: str) -> asyncio.Future[Mapping[str, Any]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        with self._lock:
            self._waiting[state] = (loop, future)
        return future

    def drop(self, state: str) -> None:
        with self._lock:
            self._waiting.pop(state, None)

    def deliver(self, state: str, token: Mapping[str, Any]) -> bool:
        """Hand the stored token to the waiter of ``state``; ``False`` when none waits."""
        with self._lock:
            waiter = self._waiting.pop(state, None)
        if waiter is None:
            return False
        loop, future = waiter
        loop.call_soon_threadsafe(
            lambda: future.done() or future.set_result({"state": state, "token": token})
        )
        return True


CALLBACK_WAITERS = CallbackWaiters()


class BrokerPkceBridge:
    """``challenge``, ``complete`` and ``refresh`` over the declared providers' brokers."""

    def __init__(
        self,
        brokers: Any,
        providers: Callable[[str], Mapping[str, Any] | None],
        *,
        waiters: CallbackWaiters = CALLBACK_WAITERS,
        clock: Callable[[], float] = time.time,
        actor: Callable[[], Any] | None = None,
    ) -> None:
        self._brokers = brokers
        self._providers = providers
        self._waiters = waiters
        self._clock = clock
        self._actor = actor
        self._pending: dict[str, asyncio.Future[Mapping[str, Any]]] = {}

    def _current_actor(self) -> Any:
        if self._actor is not None:
            return self._actor()
        from arbora.security.actor_context import current_actor

        return current_actor()

    def _broker(self, provider_id: str) -> tuple[Any, Any]:
        config = self._providers(provider_id)
        if config is None:
            raise CredentialRefused(CredentialRefusal.PROVIDER_UNAVAILABLE)
        return self._brokers.broker_for(config)

    async def challenge(
        self, request: CredentialUseRequest, attempt: int
    ) -> AuthorizationChallenge | None:
        broker, descriptor = self._broker(request.provider)
        actor = self._current_actor()
        session_ref = "credential-reuse:" + secrets.token_urlsafe(12)
        try:
            url = await asyncio.to_thread(
                broker.begin,
                provider_id=descriptor.provider_id,
                actor=actor,
                browser_session_id=session_ref,
                requested_scopes=tuple(request.scopes),
            )
        except Exception:  # one stable code; the cause never leaves
            raise CredentialRefused(CredentialRefusal.PROVIDER_UNAVAILABLE) from None
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        transaction = broker.transactions.peek(query.get("state", ""))
        if transaction is None:
            raise CredentialRefused(CredentialRefusal.PROVIDER_UNAVAILABLE)
        self._pending[transaction.state] = self._waiters.expect(transaction.state)
        return AuthorizationChallenge(
            request=request,
            issuer=f"{parts.scheme}://{parts.netloc}",
            client_id=query["client_id"],
            callback=descriptor.redirect_uri,
            state=transaction.state,
            nonce=transaction.nonce,
            pkce_challenge=query["code_challenge"],
            attempt=attempt,
            deadline=int(self._clock()) + CHALLENGE_DEADLINE_SECONDS,
        )

    async def complete(
        self, challenge: AuthorizationChallenge, deadline: float
    ) -> Mapping[str, Any]:
        future = self._pending.pop(challenge.state)
        try:
            answer = await asyncio.wait_for(future, deadline)
        except TimeoutError:
            raise CredentialRefused(CredentialRefusal.INPUT_TIMEOUT) from None
        finally:
            self._waiters.drop(challenge.state)
        token = answer["token"]
        return {
            "state": answer["state"],
            "token": dataclasses.asdict(token)
            if dataclasses.is_dataclass(token)
            else token,
        }

    async def refresh(
        self, record: Mapping[str, Any], grant: CredentialUseGrant
    ) -> Mapping[str, Any]:
        broker, descriptor = self._broker(grant.request.provider)
        import httpx

        def rotate() -> Any:
            client = httpx.Client(timeout=15)
            try:
                return broker.tokens.refresh(
                    actor=self._current_actor(),
                    provider=descriptor,
                    resource_url=descriptor.resource_url,
                    audience=descriptor.resource_url,
                    http_client=client,
                )
            finally:
                client.close()

        try:
            fresh = await asyncio.to_thread(rotate)
        except Exception:  # the narrowed authority never widens on failure
            raise CredentialRefused(CredentialRefusal.EXPIRED) from None
        return dataclasses.asdict(fresh)
