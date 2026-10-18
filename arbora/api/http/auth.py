"""HTTP caller projection over explicitly supplied verified request authorities."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from arbora.api.mcp.caller import caller_from_session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arbora.api.contracts.session import GraphSession
    from arbora.api.invoke import VerifiedCaller
    from arbora.security.request_identity import VerifiedLocalBearer


class HTTPAuthenticationError(PermissionError):
    """A request has no verified HTTP identity or a valid CSRF proof."""


class AmbientHTTPAuthenticator:
    """Decide console eligibility for an already-verified caller.

    The gate's session marker is set only after a live browser session and
    token-bound CSRF have been checked. A cookie value cannot set the marker.
    """

    def __init__(
        self,
        *,
        console_origin: str | None = None,
        session_for_request: Callable[[Request], Awaitable[Any]] | None = None,
        verify_browser_session: Callable[[Request, Any], Awaitable[int | None]]
        | None = None,
    ) -> None:
        # Configured at the server composition root, never from a request.
        self.console_origin = console_origin
        if session_for_request is not None and not callable(session_for_request):
            raise ValueError("Verified HTTP session resolver required")
        if verify_browser_session is not None and not callable(verify_browser_session):
            raise ValueError("Verified browser session resolver required")
        self._session_for_request = session_for_request
        self._verify_browser_session = verify_browser_session

    def is_console_request(self, request: Request, caller: Any) -> bool:
        """Only a fresh attended browser session may use Surface.CONSOLE."""
        state = request.scope.get("state") or {}
        if state.get("arbora_session_admitted") is not True:
            return False
        if not request.cookies.get("__Host-arbora_session"):
            return False
        if (
            not self.console_origin
            or request.headers.get("origin") != self.console_origin
        ):
            return False
        if (
            caller.credential_kind != "session"
            or caller.principal_kind != "human"
            or caller.delegated
            or caller.mfa_at_ms is None
        ):
            return False
        age_ms = int(time.time() * 1000) - caller.mfa_at_ms
        return 0 <= age_ms <= 900_000

    @staticmethod
    def _require_exact_bearer(request: Request) -> str:
        """Validate one well-formed bearer header; refuse anything ambiguous."""
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1:
            raise HTTPAuthenticationError("Verified bearer required")
        scheme, _, token = authorization[0].partition(" ")
        if scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
            raise HTTPAuthenticationError("Verified bearer required")
        return "bearer"

    def _credential_kind(self, request: Request) -> str:
        """Classify the presented credential; refuse ambiguous or malformed ones."""
        state = request.scope.get("state") or {}
        cookie = request.cookies.get("__Host-arbora_session")
        admitted = state.get("arbora_session_admitted") is True
        if cookie is None and not admitted:
            return self._require_exact_bearer(request)
        if not cookie or not admitted or self._verify_browser_session is None:
            raise HTTPAuthenticationError("Verified browser authority required")
        return "session"

    async def _browser_mfa(
        self, request: Request, session: Any, kind: str
    ) -> int | None:
        """Return the verified browser MFA timestamp; bearer callers have none."""
        if kind != "session":
            return None
        verifier = self._verify_browser_session
        if verifier is None:
            raise HTTPAuthenticationError("Verified browser authority required")
        return await verifier(request, session)

    async def authenticate(self, request: Request) -> VerifiedCaller:
        """Resolve the authority already verified and bound to this request.

        ``session_for_request`` must reject credentials not verified for this
        exact request; mere ambient process authority is insufficient. Browser
        verification must check the live cookie, CSRF and session subject/tenant
        binding, returning only a verified MFA timestamp (or None). Missing
        authority refuses; headers and request state never manufacture a caller.
        """
        if self._session_for_request is None:
            raise HTTPAuthenticationError("Verified HTTP authority unavailable")
        kind = self._credential_kind(request)
        try:
            session = await self._session_for_request(request)
            mfa_at_ms = await self._browser_mfa(request, session, kind)
            return caller_from_session(
                session,
                credential_kind=kind,
                request_id=request.headers.get("x-request-id", "")[:128],
                mfa_at_ms=mfa_at_ms,
            )
        except PermissionError as exc:
            # Static, non-secret reason, so a refused caller is diagnosable.
            logger.warning("HTTP caller refused: %s", exc)
            raise HTTPAuthenticationError("Verified identity required") from None


async def verified_bearer_session_for_request(request: Request) -> GraphSession:
    """Verify this exact request through AU's public served-session flow.

    Each resolution verifies the presented credential afresh. Ambient sessions
    and request-state claims cannot establish authority for this request.
    """
    scope = request.scope
    headers = tuple(scope.get("headers", ()))
    try:
        actor = await _verify_bearer_headers(headers)
        _require_request_snapshot(request, scope, headers)
        return _mint_verified_session(actor)
    except PermissionError:
        raise HTTPAuthenticationError("Verified request session required") from None


async def _verify_bearer_headers(headers: tuple[tuple[bytes, bytes], ...]) -> Any:
    """Verify one presented credential while preserving infrastructure faults."""
    from arbora.security.auth import (
        HTTP_401_UNAUTHORIZED,
        TokenValidationError,
        parse_bearer_authorization,
    )
    from arbora.security.request_identity import actor_from_bearer_token

    token = parse_bearer_authorization(
        [value for key, value in headers if key.lower() == b"authorization"]
    )
    if token is None:
        raise HTTPAuthenticationError("Presented bearer required")
    try:
        return await actor_from_bearer_token(token)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        raise HTTPAuthenticationError("Verified bearer required") from None
    except TokenValidationError as error:
        # Verification raises the framework-free error: a rejected, expired
        # or wrongly signed bearer is a 401, never a 500. A verification-path
        # fault (status 500) still propagates.
        if error.status_code != HTTP_401_UNAUTHORIZED:
            raise
        raise HTTPAuthenticationError("Verified bearer required") from None


def _require_request_snapshot(
    request: Request, scope: Any, headers: tuple[tuple[bytes, bytes], ...]
) -> None:
    """Refuse request or credential replacement during awaited verification."""
    if request.scope is not scope or tuple(scope.get("headers", ())) != headers:
        raise HTTPAuthenticationError("Request changed during verification")


def _mint_verified_session(actor: Any) -> GraphSession:
    """Retain the verified actor and tenant in AU's current session projection."""
    from arbora.security.request_identity import mint_graph_session

    session = mint_graph_session(actor)
    if session.actor is not actor or session.tenant != actor.tenant_id:
        raise HTTPAuthenticationError("Verified session authority mismatch")
    session.ensure_authority_current()
    return session


async def verify_local_bearer_request(request: Request) -> VerifiedLocalBearer:
    """Verify this request's exact local token; do not manufacture a caller.

    The result lacks current policy/delegation/engine authority. Composition
    must obtain those from the qualified owner before producing a session.
    No ambient identity or legacy actor/session minter is consulted here.
    """
    from arbora.security.auth import parse_bearer_authorization
    from arbora.security.request_identity import verify_local_bearer_token

    try:
        token = parse_bearer_authorization(
            [
                value
                for key, value in request.scope.get("headers", ())
                if key.lower() == b"authorization"
            ]
        )
        if token is None:
            raise PermissionError("Presented bearer required")
        return await verify_local_bearer_token(token)
    except PermissionError:
        raise HTTPAuthenticationError("Verified local bearer required") from None


class BoundBrowserVerifier:
    """Bind the WebUI exporter to an explicit qualified live-session owner."""

    def __init__(self, *, authority: Any, console_origin: str) -> None:
        if not callable(getattr(authority, "verify_request", None)) or not callable(
            getattr(authority, "session_for_request", None)
        ):
            raise ValueError("Verified browser session authority required")
        if not isinstance(console_origin, str) or not console_origin:
            raise ValueError("Verified browser origin required")
        self._authority = authority
        self._console_origin = console_origin

    async def __call__(self, request: Request, session: Any) -> int | None:
        from agent_webui.oidc_session import verify_browser_session

        # The same producer that exports evidence must own this exact session.
        # Equal subject/tenant values cannot prove a cookie-to-caller pairing.
        if await self._authority.session_for_request(request) is not session:
            raise PermissionError("Browser caller session instance was substituted")
        return await verify_browser_session(
            request.scope,
            session,
            authority=self._authority,
            console_origin=self._console_origin,
        )
