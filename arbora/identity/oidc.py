"""ID-token claim checks (ARBORA-IDENTITY-R009.2.2).

Mapping and JIT policy are the engine's, applied at ``external_login`` (R009.2.3); no
mapping rule is evaluated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .engine import IdentityUnavailable


class OidcRefusalReason(StrEnum):
    """Why decoded ID-token claims were refused."""

    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    EXPIRED = "expired"
    MISSING_EXPIRY = "missing_expiry"
    NONCE_MISMATCH = "nonce_mismatch"


class OidcTokenRefused(IdentityUnavailable):
    """Decoded ID-token claims failed a check; carries the typed reason."""

    def __init__(self, reason: OidcRefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def check_id_token_claims(
    claims: Mapping[str, Any],
    *,
    issuer: str,
    audience: str,
    nonce: str,
    now: datetime,
    clock_skew: timedelta = timedelta(0),
) -> None:
    """Refuse unless issuer, audience, expiry and nonce all match (no signature check)."""
    if now.tzinfo is None:
        raise IdentityUnavailable("OIDC check time must be timezone-aware")
    if not issuer or not audience or not nonce:
        raise IdentityUnavailable("OIDC check requires issuer, audience and nonce")
    if claims.get("iss") != issuer:
        raise OidcTokenRefused(
            OidcRefusalReason.WRONG_ISSUER, "ID token issuer does not match"
        )
    _check_audience(claims.get("aud"), audience)
    _check_expiry(claims.get("exp"), now, clock_skew)
    if claims.get("nonce") != nonce:
        raise OidcTokenRefused(
            OidcRefusalReason.NONCE_MISMATCH, "ID token nonce does not match"
        )


def _check_audience(aud: object, audience: str) -> None:
    audiences = [aud] if isinstance(aud, str) else aud
    if not isinstance(audiences, (list, tuple)) or audience not in audiences:
        raise OidcTokenRefused(
            OidcRefusalReason.WRONG_AUDIENCE, "ID token audience is not this client"
        )


def _check_expiry(exp: object, now: datetime, clock_skew: timedelta) -> None:
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        raise OidcTokenRefused(
            OidcRefusalReason.MISSING_EXPIRY, "ID token has no usable expiry"
        )
    if now.astimezone(UTC) - clock_skew >= datetime.fromtimestamp(exp, UTC):
        raise OidcTokenRefused(OidcRefusalReason.EXPIRED, "ID token has expired")
