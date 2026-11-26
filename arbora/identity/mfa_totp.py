"""TOTP second-factor routes: a pass-through to the engine (ARBORA-IDENTITY-R006.2.1).

The engine holds the TOTP secret and checks the code. Arbora forwards the verified
session, its tenant and the typed code to the engine ``Identity`` ``mfa`` family and
returns the engine's answer unchanged: it holds no secret, computes no code, counts no
failure and keeps no window. The provisioning URI is returned once, by ``enroll``.

The ``mfa`` operations ``enroll``, ``confirm``, ``verify`` and ``disable`` are engine
``main`` shapes the pinned release lacks (PIN-LAG: remove the guard after the re-pin that
carries ``IdentityOpMfaEnroll``, ``IdentityOpMfaConfirm``, ``IdentityOpMfaVerify`` and
``IdentityOpMfaDisable``): until then each route answers ``UNAVAILABLE`` naming the
missing shape and nothing falls back to a local check.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.responses import JSONResponse

from arbora.pin_lag.medulla import PinLagUnavailable, require_models

#: Route path to the engine ``mfa`` operation.
TOTP_ROUTES: dict[str, str] = {
    "/auth/mfa/totp/enroll": "enroll",
    "/auth/mfa/totp/confirm": "confirm",
    "/auth/mfa/totp/verify": "verify",
    "/auth/mfa/totp/disable": "disable",
}
#: The operations that take a code; ``enroll`` takes none.
CODE_OPERATIONS = frozenset({"confirm", "verify", "disable"})
TOTP_MODELS: tuple[str, ...] = (
    "IdentityOpMfaEnroll",
    "IdentityOpMfaConfirm",
    "IdentityOpMfaVerify",
    "IdentityOpMfaDisable",
)
#: The closed refusals the engine answers; anything else is an unavailable engine.
REFUSALS = frozenset(
    {
        "MFA_CODE_INVALID",
        "MFA_CODE_REPLAYED",
        "MFA_LOCKED",
        "MFA_ENROLLMENT_REQUIRED",
    }
)
CODE_LENGTH = 6
BODY_LIMIT = 1024


class MfaBodyInvalid(PermissionError):
    """The body is not exactly the one field the operation takes."""


async def totp_body(scope: Any, receive: Any, operation: str) -> str | None:
    """The six-digit code of a code-taking operation, or ``None`` for ``enroll``."""
    import json

    from .browser import _headers

    if _headers(scope, b"content-type") != (b"application/json",):
        raise MfaBodyInvalid("JSON body required")
    data = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise MfaBodyInvalid("request interrupted")
        data.extend(message.get("body", b""))
        if len(data) > BODY_LIMIT:
            raise MfaBodyInvalid("body too large")
        if not message.get("more_body"):
            break
    try:
        body = json.loads(data or b"{}")
    except (ValueError, UnicodeError):
        raise MfaBodyInvalid("invalid body") from None
    if operation not in CODE_OPERATIONS:
        if body != {}:
            raise MfaBodyInvalid("no fields are taken")
        return None
    code = (
        body.get("code") if isinstance(body, dict) and set(body) == {"code"} else None
    )
    if not isinstance(code, str) or len(code) != CODE_LENGTH or not code.isdigit():
        raise MfaBodyInvalid("a six-digit code is required")
    return code


async def totp_pass_through(
    client: Any,
    graph: str,
    operation: str,
    *,
    tenant: str,
    session_token: str,
    code: str | None,
) -> JSONResponse:
    """Forward one TOTP operation to the engine and return its answer unchanged."""
    from medulla import EngineResponseError
    from medulla.generated.security import decode_identity, send_identity

    try:
        require_models(TOTP_MODELS)
    except PinLagUnavailable as missing:
        return JSONResponse(
            {"error": "UNAVAILABLE", "missing": missing.missing}, status_code=503
        )
    request: dict[str, Any] = {"tenant": tenant, "session": session_token}
    if code is not None:
        request["code"] = code
    try:
        async with asyncio.timeout(5):
            response = await send_identity(
                client,
                {"op": {"family": "mfa", "op": operation, "request": request}},
                graph,
            )
            decode_identity(response)
    except EngineResponseError as refused:
        if refused.code in REFUSALS:
            return refusal_response(refused)
        return JSONResponse({"error": "UNAVAILABLE"}, status_code=503)
    except Exception:  # noqa: BLE001 - an unanswered question steps nothing up
        return JSONResponse({"error": "UNAVAILABLE"}, status_code=503)
    payload = response.payload
    value = payload.get("value") if isinstance(payload, dict) else None
    return JSONResponse(value if isinstance(value, dict) else {"ok": True})


def refusal_response(refused: Any) -> JSONResponse:
    """The engine's closed refusal; a lockout carries the time it lifts."""
    if refused.code == "MFA_LOCKED":
        retry_at = _retry_at_ms(refused.detail)
        body: dict[str, Any] = {"error": "MFA_LOCKED"}
        if retry_at is not None:
            body["retry_at_ms"] = retry_at
        return JSONResponse(body, status_code=429)
    return JSONResponse({"error": refused.code}, status_code=403)


def _retry_at_ms(detail: str | None) -> int | None:
    """``retry_at_ms=<n>`` out of the engine's diagnostic, the only field read from it."""
    for part in (detail or "").replace(",", " ").split():
        name, _, value = part.partition("=")
        if name == "retry_at_ms" and value.isdigit():
            return int(value)
    return None
