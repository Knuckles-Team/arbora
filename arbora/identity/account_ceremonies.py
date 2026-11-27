"""Account ceremonies of the native browser sessions (ARBORA-IDENTITY-R005.2).

Register, password change, reset request and reset confirm. Each calls the engine
``Identity`` method and keeps no account store: the engine holds the user, the password
verifier, the reset token and every throttle (its authenticate outcome ``throttled`` is
answered like a wrong password). Public bodies are uniform, so none of them is an
existence oracle: register and reset request answer the same 202 whether the account
exists, and a spent, unknown or expired reset token answers one ``RESET_TOKEN_INVALID``.
Every ceremony emits one audit event naming a digest of the user and the outcome, never
a password or a token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.responses import JSONResponse, Response

from .engine import IdentityUnavailable
from .mfa_ceremonies import ceremony_body
from .mfa_totp import MfaBodyInvalid

REGISTER = "/auth/register"
CHANGE_ROUTE = "/auth/password/change"
RESET_REQUEST = "/auth/password/reset/request"
RESET_CONFIRM = "/auth/password/reset/confirm"
ACCOUNT_ROUTES = frozenset({REGISTER, CHANGE_ROUTE, RESET_REQUEST, RESET_CONFIRM})
#: Reset tokens live fifteen minutes at the engine.
RESET_TTL_MS = 900_000
_FIELDS = {
    REGISTER: frozenset({"username", "password"}),
    CHANGE_ROUTE: frozenset({"current", "new"}),
    RESET_REQUEST: frozenset({"username"}),
    RESET_CONFIRM: frozenset({"token", "new_password"}),
}
#: Engine refusals that mean the new password breaks the policy.
_POLICY = frozenset({"IDENTITY_WEAK_PASSWORD", "IDENTITY_PASSWORD_REUSED"})
_audit_log = logging.getLogger("arbora.identity.audit")

Audit = Callable[[Mapping[str, str]], None]
Mailer = Callable[[str, str], Awaitable[bool]]


def log_audit(event: Mapping[str, str]) -> None:
    """The default audit sink: one structured record per ceremony."""
    _audit_log.info("identity.account.ceremony", extra={"audit": dict(event)})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class AccountCeremonies:
    """The four account routes over one engine client; ``acting`` binds a session's claims."""

    def __init__(
        self,
        client: Any,
        graph: str,
        *,
        acting: Callable[[Any], Any] | None = None,
        audit: Audit = log_audit,
        mail: Mailer | None = None,
    ) -> None:
        self._client, self._graph = client, graph
        self._acting, self._audit, self._mail = acting, audit, mail

    async def _identity(
        self, client: Any, family: str, op: str, request: dict[str, Any]
    ) -> Any:
        """One engine ``Identity`` call; the answer's ``value``. Refusals propagate."""
        from medulla.generated.security import decode_identity, send_identity

        async with asyncio.timeout(5):
            response = await send_identity(
                client,
                {"op": {"family": family, "op": op, "request": request}},
                self._graph,
            )
            decode_identity(response)
        payload = response.payload
        return payload.get("value") if isinstance(payload, Mapping) else None

    def _done(self, ceremony: str, subject: str, outcome: str) -> None:
        self._audit(
            {"ceremony": ceremony, "user": _digest(subject), "outcome": outcome}
        )

    async def body(
        self, scope: Any, receive: Any, path: str
    ) -> dict[str, str] | JSONResponse:
        """The ceremony's exact JSON body, or the uniform 400."""
        try:
            return await ceremony_body(scope, receive, _FIELDS[path], limit=4096)
        except MfaBodyInvalid:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)

    async def register(self, body: Mapping[str, str]) -> Response:
        """Create a human account; an existing or refused one answers the same 202."""
        from medulla import EngineResponseError

        outcome = "accepted"
        try:
            await self._identity(
                self._client,
                "user",
                "create",
                {"kind": "human", **body},
            )
        except EngineResponseError as refused:
            if refused.code in _POLICY:
                self._done("register", body["username"], "PASSWORD_POLICY_FAILED")
                return JSONResponse(
                    {"error": "PASSWORD_POLICY_FAILED"}, status_code=422
                )
            outcome = "refused"
        self._done("register", body["username"], outcome)
        return JSONResponse({"outcome": "accepted"}, status_code=202)

    async def change(self, session: Any, body: Mapping[str, str]) -> Response:
        """Change the signed-in person's password; the engine ends their other sessions."""
        from medulla import EngineResponseError

        subject = str(session.actor.actor_id)
        if self._acting is None:
            raise IdentityUnavailable("no client acts as a signed-in person")
        try:
            await self._identity(
                self._acting(session),
                "credential",
                "change_password",
                {"current": body["current"], "new": body["new"]},
            )
        except EngineResponseError as refused:
            if refused.code in _POLICY:
                self._done("password_change", subject, "PASSWORD_POLICY_FAILED")
                return JSONResponse(
                    {"error": "PASSWORD_POLICY_FAILED"}, status_code=422
                )
            self._done("password_change", subject, "refused")
            raise PermissionError("password change refused") from None
        self._done("password_change", subject, "ok")
        return Response(status_code=204)

    async def reset_request(self, body: Mapping[str, str]) -> Response:
        """Issue a one-use reset token; the same 202 whether or not the account exists."""
        from medulla import EngineResponseError

        token = secrets.token_urlsafe(32)
        outcome = "issued"
        try:
            delivery = await self._identity(
                self._client,
                "token",
                "issue_password_reset",
                {"token": token, "ttl_ms": RESET_TTL_MS, "username": body["username"]},
            )
            await self._hand_off(delivery, token)
        except EngineResponseError:
            outcome = "refused"
        self._done("reset_request", body["username"], outcome)
        return JSONResponse({"outcome": "accepted"}, status_code=202)

    async def _hand_off(self, delivery: Any, token: str) -> None:
        email = delivery.get("email") if isinstance(delivery, Mapping) else None
        if self._mail is not None and isinstance(email, str) and email:
            await self._mail(email, token)

    async def reset_confirm(self, body: Mapping[str, str]) -> Response:
        """Redeem the token and set the new password in one engine call."""
        from medulla import EngineResponseError

        try:
            await self._identity(
                self._client,
                "token",
                "redeem_one_time",
                {
                    "purpose": "credential_reset",
                    "token": body["token"],
                    "new_password": body["new_password"],
                },
            )
        except EngineResponseError as refused:
            if refused.code in _POLICY:
                self._done("reset_confirm", "-", "PASSWORD_POLICY_FAILED")
                return JSONResponse(
                    {"error": "PASSWORD_POLICY_FAILED"}, status_code=422
                )
            self._done("reset_confirm", "-", "RESET_TOKEN_INVALID")
            return JSONResponse({"error": "RESET_TOKEN_INVALID"}, status_code=400)
        self._done("reset_confirm", "-", "ok")
        return Response(status_code=204)
