"""Second-factor sign-in ceremonies: WebAuthn and recovery codes (ARBORA-IDENTITY-R006.2.2, .2.3, .4).

A login that needs a second factor leaves a pending engine session and an interim cookie
that grants nothing. These ceremonies run on that session. The engine holds the
credentials, the one-use challenge, the recovery-code hashes and every counter; Arbora
passes the session token and what the browser sent, checks the one thing only it can
(the WebAuthn assertion against the engine's public key, challenge and the public
origin), and answers the engine's refusals unchanged. A success returns ``None`` and the
application finishes the login by resolving the now-complete session.

PIN-LAG: ``IssueChallenge`` and ``ConsumeChallenge`` are engine ``main`` shapes the pinned
release lacks; until it carries ``IdentityOpMfaIssueChallenge`` and
``IdentityOpMfaConsumeChallenge`` the two WebAuthn routes answer ``UNAVAILABLE`` naming the
missing shape. The recovery route needs only ``IdentityOpMfaConsumeRecoveryCode``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from arbora.pin_lag.medulla import PinLagUnavailable, require_models

from .mfa_totp import BODY_LIMIT, MfaBodyInvalid, refusal_response
from .webauthn import (
    Assertion,
    WebauthnRefused,
    b64url_decode,
    b64url_encode,
    verify_assertion,
)

BEGIN, FINISH, RECOVERY = (
    "/auth/mfa/webauthn/begin",
    "/auth/mfa/webauthn/finish",
    "/auth/mfa/recovery",
)
CEREMONY_ROUTES = frozenset({BEGIN, FINISH, RECOVERY})
WEBAUTHN_MODELS = (
    "IdentityOpMfaWebauthnCredentials",
    "IdentityOpMfaIssueChallenge",
    "IdentityOpMfaConsumeChallenge",
    "IdentityOpMfaVerifyWebauthn",
)
RECOVERY_MODELS = ("IdentityOpMfaConsumeRecoveryCode",)
#: Recovery codes are 10 characters, each valid once.
RECOVERY_CODE_LENGTH = 10
#: A challenge lives five minutes at the engine; the interim cookie lives as long.
INTERIM_SECONDS = 300
CHALLENGE_BYTES = 32
FINISH_FIELDS = frozenset(
    {
        "challenge_id",
        "credential_id",
        "client_data_json",
        "authenticator_data",
        "signature",
    }
)
#: Refusals the engine answers by name; anything else is an unavailable engine.
ENGINE_REFUSALS = frozenset(
    {
        "MFA_CODE_INVALID",
        "MFA_CODE_REPLAYED",
        "MFA_LOCKED",
        "MFA_ENROLLMENT_REQUIRED",
        "MFA_NO_PENDING_LOGIN",
        "RECOVERY_CODE_USED",
        "WEBAUTHN_CHALLENGE_USED",
        "WEBAUTHN_COUNTER_REGRESSED",
        "WEBAUTHN_NO_CREDENTIAL",
    }
)
_audit_log = logging.getLogger("arbora.identity.audit")

Audit = Callable[[Mapping[str, str]], None]


def log_audit(event: Mapping[str, str]) -> None:
    """The default audit sink: one structured record per ceremony, never a secret."""
    _audit_log.info("identity.mfa.ceremony", extra={"audit": dict(event)})


def challenge_id_of(challenge: str) -> str:
    """The handle a client echoes back: a digest of the challenge, never the challenge."""
    return hashlib.sha256(challenge.encode("ascii")).hexdigest()[:32]


async def ceremony_body(
    scope: Any, receive: Any, fields: frozenset[str], *, limit: int = BODY_LIMIT
) -> dict[str, str]:
    """A JSON body of exactly ``fields``, each a non-empty string; empty for no fields."""
    from .browser import _headers

    if _headers(scope, b"content-type") != (b"application/json",):
        raise MfaBodyInvalid("JSON body required")
    data = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise MfaBodyInvalid("request interrupted")
        data.extend(message.get("body", b""))
        if len(data) > limit:
            raise MfaBodyInvalid("body too large")
        if not message.get("more_body"):
            break
    try:
        body = json.loads(data or b"{}")
    except (ValueError, UnicodeError):
        raise MfaBodyInvalid("invalid body") from None
    if not isinstance(body, dict) or set(body) != set(fields):
        raise MfaBodyInvalid("exactly the documented fields are required")
    if not all(isinstance(value, str) and value for value in body.values()):
        raise MfaBodyInvalid("every field is a non-empty string")
    return dict(body)


def _unavailable(missing: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": "UNAVAILABLE"}
    if missing:
        body["missing"] = missing
    return JSONResponse(body, status_code=503)


class MfaCeremonies:
    """The WebAuthn and recovery routes over one engine client and one public origin."""

    def __init__(
        self, client: Any, graph: str, origin: str, audit: Audit = log_audit
    ) -> None:
        self._client, self._graph, self._origin = client, graph, origin
        self._audit = audit
        self._rp_id = urlsplit(origin).hostname or ""

    async def _mfa(self, op: str, request: dict[str, Any]) -> Any:
        """One engine ``mfa`` call; the answer's ``value``. Engine refusals propagate."""
        from medulla.generated.security import decode_identity, send_identity

        async with asyncio.timeout(5):
            response = await send_identity(
                self._client,
                {"op": {"family": "mfa", "op": op, "request": request}},
                self._graph,
            )
            decode_identity(response)
        payload = response.payload
        return payload.get("value") if isinstance(payload, dict) else None

    def _done(self, method: str, outcome: str) -> None:
        self._audit({"method": method, "outcome": outcome})

    async def begin(self, token: str) -> JSONResponse:
        """Issue one WebAuthn challenge bound to the interim session (R006.4)."""
        from medulla import EngineResponseError

        try:
            require_models(WEBAUTHN_MODELS)
        except PinLagUnavailable as missing:
            return _unavailable(missing.missing)
        try:
            credentials = await self._mfa(
                "webauthn_credentials", {"session_token": token}
            )
            if not isinstance(credentials, list) or not credentials:
                self._done("webauthn", "WEBAUTHN_NO_CREDENTIAL")
                return JSONResponse(
                    {"error": "WEBAUTHN_NO_CREDENTIAL"}, status_code=403
                )
            challenge = b64url_encode(secrets.token_bytes(CHALLENGE_BYTES))
            await self._mfa(
                "issue_challenge",
                {"challenge": challenge, "kind": "webauthn", "session_token": token},
            )
        except EngineResponseError as refused:
            return self._engine_refusal("webauthn", refused)
        except Exception:  # noqa: BLE001 - an unanswered question steps nothing up
            return _unavailable()
        return JSONResponse(
            {
                "challenge_id": challenge_id_of(challenge),
                "options": {
                    "challenge": challenge,
                    "rpId": self._rp_id,
                    "timeout": INTERIM_SECONDS * 1000,
                    "userVerification": "preferred",
                    "allowCredentials": [
                        {
                            "type": "public-key",
                            "id": item["credential_id"],
                            "transports": list(item.get("transports") or []),
                        }
                        for item in credentials
                    ],
                },
            }
        )

    async def finish(self, token: str, body: Mapping[str, str]) -> JSONResponse | None:
        """Verify one assertion (R006.2.2); ``None`` when the engine recorded the success."""
        from medulla import EngineResponseError

        try:
            require_models(WEBAUTHN_MODELS)
        except PinLagUnavailable as missing:
            return _unavailable(missing.missing)
        try:
            counter, credential = await self._assert(token, body)
            await self._mfa(
                "verify_webauthn",
                {
                    "credential_id": credential,
                    "new_sign_count": counter,
                    "session_token": token,
                },
            )
        except WebauthnRefused as refused:
            self._done("webauthn", refused.code)
            return JSONResponse({"error": refused.code}, status_code=403)
        except EngineResponseError as refused:
            return self._engine_refusal("webauthn", refused)
        except Exception:  # noqa: BLE001 - an unanswered question steps nothing up
            return _unavailable()
        self._done("webauthn", "ok")
        return None

    async def _assert(self, token: str, body: Mapping[str, str]) -> tuple[int, str]:
        """Consume the challenge, find the credential and verify; the new counter and id."""
        credentials = await self._mfa("webauthn_credentials", {"session_token": token})
        credential_id = body["credential_id"]
        stored = next(
            (
                item
                for item in credentials or ()
                if isinstance(item, dict) and item.get("credential_id") == credential_id
            ),
            None,
        )
        # The challenge is consumed first: it is valid once whatever the assertion is.
        issued = await self._mfa(
            "consume_challenge", {"kind": "webauthn", "session_token": token}
        )
        challenge = issued.get("challenge") if isinstance(issued, dict) else None
        if not isinstance(challenge, str) or not challenge:
            raise WebauthnRefused("WEBAUTHN_CHALLENGE_USED")
        if body["challenge_id"] != challenge_id_of(challenge):
            raise WebauthnRefused("WEBAUTHN_CHALLENGE_MISMATCH")
        if stored is None:
            raise WebauthnRefused("WEBAUTHN_NO_CREDENTIAL")
        counter = verify_assertion(
            Assertion(
                b64url_decode(body["client_data_json"]),
                b64url_decode(body["authenticator_data"]),
                b64url_decode(body["signature"]),
            ),
            challenge=challenge,
            origin=self._origin,
            rp_id=self._rp_id,
            public_key_cose=stored["public_key_cose"],
            stored_sign_count=int(stored["sign_count"]),
        )
        return counter, credential_id

    async def recovery(self, token: str, code: str) -> JSONResponse | None:
        """Consume one recovery code (R006.2.3); ``None`` when the engine accepted it."""
        from medulla import EngineResponseError

        if len(code) != RECOVERY_CODE_LENGTH or not code.isalnum():
            self._done("recovery", "MFA_CODE_INVALID")
            return JSONResponse({"error": "MFA_CODE_INVALID"}, status_code=400)
        try:
            require_models(RECOVERY_MODELS)
        except PinLagUnavailable as missing:
            return _unavailable(missing.missing)
        try:
            await self._mfa(
                "consume_recovery_code", {"code": code, "session_token": token}
            )
        except EngineResponseError as refused:
            return self._engine_refusal("recovery", refused)
        except Exception:  # noqa: BLE001 - an unanswered question steps nothing up
            return _unavailable()
        self._done("recovery", "ok")
        return None

    def _engine_refusal(self, method: str, refused: Any) -> JSONResponse:
        if refused.code in ENGINE_REFUSALS:
            self._done(method, refused.code)
            return refusal_response(refused)
        return _unavailable()
