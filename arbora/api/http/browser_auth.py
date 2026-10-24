"""Same-owner browser caller lifetime and pre-invocation authority fence."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from arbora.api.mcp.caller import caller_from_session
from arbora.identity.broker import ArboraBrowserAuthority

from .auth import (
    AmbientHTTPAuthenticator,
    BoundBrowserVerifier,
    HTTPAuthenticationError,
)


class BrowserRateLimited(HTTPException):
    """Host-generated retry metadata, never arbitrary provider error headers."""

    def __init__(self, retry_after: float):
        import math

        self.retry_after_seconds = max(1, math.ceil(retry_after))
        super().__init__(status_code=429, detail="Rate limit exceeded")


@dataclass(slots=True)
class _RequestCaller:
    scope: Any
    task: Any
    caller: Any
    used: bool = False


class BoundBrowserHTTPAuthenticator(AmbientHTTPAuthenticator):
    """One qualified owner supplies both caller resolution and use-time checks.

    The private binding is not an ASGI state field or serializable authority.
    An operation scope owns its lifetime, including cancellation cleanup. The
    fence supplements, never replaces, native policy/CAS/effect journal checks.
    """

    def __init__(self, authority: ArboraBrowserAuthority, *, console_origin: str):
        if not isinstance(authority, ArboraBrowserAuthority):
            raise ValueError("one qualified browser authority required")
        from arbora.gateway.rate_limit import GatewayRateLimitMiddleware

        self._rate_limiter = GatewayRateLimitMiddleware(None)
        self._policy_started = False
        self._authority = authority
        self._active: ContextVar[_RequestCaller | None] = ContextVar(
            "arbora_http_browser_caller", default=None
        )
        super().__init__(
            console_origin=console_origin,
            session_for_request=authority.session_for_request,
            verify_browser_session=BoundBrowserVerifier(
                authority=authority, console_origin=console_origin
            ),
        )

    def configure_rate_policy(self, *, rate: float, burst: float) -> None:
        """Apply the effective trusted listener policy before serving starts."""
        import math

        from arbora.gateway.rate_limit import GatewayRateLimitMiddleware

        if self._policy_started or any(
            not math.isfinite(value) or value < 0 for value in (rate, burst)
        ):
            raise ValueError("valid pre-serving rate policy required")
        self._rate_limiter = GatewayRateLimitMiddleware(None, rate=rate, burst=burst)

    @asynccontextmanager
    async def operation_scope(self, request: Any):
        """Retain the exact caller instance only for this request/task."""
        self._policy_started = True
        if self._active.get() is not None:
            raise HTTPAuthenticationError("nested browser operation scope refused")
        caller = await self.authenticate(request)
        allowed, retry_after = self._rate_limiter.consume_verified_caller(caller)
        if not allowed:
            raise BrowserRateLimited(retry_after)
        record = _RequestCaller(request.scope, asyncio.current_task(), caller)
        token = self._active.set(record)
        try:
            yield caller
        finally:
            self._active.reset(token)

    def _bound(self, request: Any, caller: Any) -> _RequestCaller:
        record = self._active.get()
        if (
            record is None
            or record.scope is not request.scope
            or record.task is not asyncio.current_task()
            or record.caller is not caller
        ):
            raise HTTPAuthenticationError("private request caller binding required")
        return record

    async def before_invocation(self, request: Any, caller: Any) -> None:
        """Recheck after body/provider awaits, immediately before shared invoke."""
        record = self._bound(request, caller)
        if record.used:
            raise HTTPAuthenticationError("browser operation fence already consumed")
        record.used = True
        try:
            await self._authority.before_invocation(request.scope, caller.session)
            current_mfa = await self._browser_mfa(
                request, caller.session, caller.credential_kind
            )
            if self._bound(request, caller) is not record:
                raise HTTPAuthenticationError("browser caller binding changed")
            current = caller_from_session(
                caller.session,
                credential_kind=caller.credential_kind,
                request_id=caller.request_id,
                mfa_at_ms=current_mfa,
            )
            if current != caller:
                raise HTTPAuthenticationError("browser caller authority changed")
        except PermissionError:
            raise HTTPAuthenticationError(
                "current browser authority required"
            ) from None
