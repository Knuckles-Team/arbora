"""Stateless bearer verification (ARBORA-IDENTITY-R017.3 to R017.7, R029.4).

``verify_bearer`` checks one local-issuer bearer in the one order of the spec:
signature, issuer, audience, expiry, not-before, then the required tenant claim.
It holds no state and never fills a missing tenant from a header, cookie or process
identity. The engine authority resolution that follows
(``request_identity.verify_local_bearer_token`` and the engine session) stays
mandatory: this module only proves the token itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import Any

from arbora.security.key_cache import JwksCache

__all__ = [
    "BEARER_CLOCK_SKEW_SECONDS",
    "BearerRefusal",
    "BearerRefused",
    "BearerVerificationProfile",
    "VerifiedBearer",
    "bearer_evidence",
    "precheck_bearer",
    "profile_from_settings",
    "verify_bearer",
]

#: Fixed clock skew allowed on ``exp`` and ``nbf``.
BEARER_CLOCK_SKEW_SECONDS = 30
_MAX_TOKEN_BYTES = 16_384
_ACCESS_TOKEN_MAX_SECONDS = 300


class BearerRefusal(StrEnum):
    """Why a bearer was refused; the first ten answer 401, the last 503."""

    MISSING = "BEARER_MISSING"
    DUPLICATE_HEADER = "BEARER_DUPLICATE_HEADER"
    SIGNATURE_INVALID = "BEARER_SIGNATURE_INVALID"
    ISSUER_UNKNOWN = "BEARER_ISSUER_UNKNOWN"
    AUDIENCE_MISMATCH = "BEARER_AUDIENCE_MISMATCH"
    TENANT_MISSING = "BEARER_TENANT_MISSING"
    EXPIRED = "BEARER_EXPIRED"
    NOT_YET_VALID = "BEARER_NOT_YET_VALID"
    REVOKED = "BEARER_REVOKED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"

    @property
    def status_code(self) -> int:
        return 503 if self is BearerRefusal.AUTHORITY_UNAVAILABLE else 401


class BearerRefused(PermissionError):
    """A bearer refusal; its public body is uniform for every 401 code."""

    def __init__(self, refusal: BearerRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal

    @property
    def status_code(self) -> int:
        return self.refusal.status_code

    @property
    def public_body(self) -> dict[str, str]:
        """One body for every 401, so the code is never an oracle; 503 names itself."""
        if self.status_code == 503:
            return {"error": BearerRefusal.AUTHORITY_UNAVAILABLE.value}
        return {"error": "unauthenticated"}


@dataclass(frozen=True, slots=True)
class BearerVerificationProfile:
    """What a deployment accepts as a bearer (``identity.bearer`` in config)."""

    issuer: str
    audience: str
    key_authority: str
    tenant_claim: str = "tenant_id"
    algorithms: tuple[str, ...] = ("RS256",)
    max_lifetime_seconds: int = _ACCESS_TOKEN_MAX_SECONDS

    def __post_init__(self) -> None:
        text = (self.issuer, self.audience, self.key_authority, self.tenant_claim)
        if any(not isinstance(item, str) or not item.strip() for item in text):
            raise ValueError("BEARER_PROFILE_INVALID")
        if tuple(self.algorithms) != ("RS256",):
            raise ValueError("BEARER_PROFILE_INVALID")
        lifetime = self.max_lifetime_seconds
        if type(lifetime) is not int or not 1 <= lifetime <= _ACCESS_TOKEN_MAX_SECONDS:
            raise ValueError("BEARER_PROFILE_INVALID")

    @property
    def profile_id(self) -> str:
        return hashlib.sha256(
            json.dumps([self.issuer, self.audience, self.key_authority]).encode()
        ).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class VerifiedBearer:
    """A bearer whose token checks passed; it still lacks engine authority."""

    subject: str
    tenant: str
    client: str
    scopes: tuple[str, ...]
    expires_at: int
    evidence: Mapping[str, str] = field(default_factory=dict)
    #: The opaque session reference of ``MEDULLA-IDENTITY-R708`` (R029.4); never token text.
    session_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.tenant.strip():
            raise ValueError("BEARER_TENANT_MISSING")


def _claim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def verify_bearer(
    token: str,
    profile: BearerVerificationProfile,
    now: float,
    *,
    keys: Mapping[str, Any] | None,
    revoked: Callable[[Mapping[str, Any]], bool] = lambda _claims: False,
    expected_tenant: str | None = None,
) -> VerifiedBearer:
    """Verify one bearer in order; raise :class:`BearerRefused` with the first failure.

    ``keys`` is the JWKS of the profile's key authority (``None`` when it could not be
    read: ``AUTHORITY_UNAVAILABLE``). ``revoked`` is the engine's revocation answer for
    the decoded claims. ``expected_tenant`` is the tenant the request was addressed to;
    a different signed tenant refuses ``TENANT_MISMATCH``.
    """
    if not isinstance(token, str) or not token:
        raise BearerRefused(BearerRefusal.MISSING)
    if keys is None:
        raise BearerRefused(BearerRefusal.AUTHORITY_UNAVAILABLE)
    claims = _decode(token, keys, profile)
    if claims.get("iss") != profile.issuer:
        raise BearerRefused(BearerRefusal.ISSUER_UNKNOWN)
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if profile.audience not in audiences:
        raise BearerRefused(BearerRefusal.AUDIENCE_MISMATCH)
    skew = BEARER_CLOCK_SKEW_SECONDS
    expires = claims.get("exp")
    if type(expires) is not int or expires + skew <= now:
        raise BearerRefused(BearerRefusal.EXPIRED)
    not_before = claims.get("nbf", claims.get("iat"))
    if type(not_before) is int and not_before - skew > now:
        raise BearerRefused(BearerRefusal.NOT_YET_VALID)
    issued = claims.get("iat")
    if type(issued) is int and expires - issued > profile.max_lifetime_seconds + skew:
        raise BearerRefused(BearerRefusal.EXPIRED)
    tenant = _claim(claims.get(profile.tenant_claim))
    if not tenant:
        raise BearerRefused(BearerRefusal.TENANT_MISSING)
    if expected_tenant is not None and tenant != expected_tenant:
        raise BearerRefused(BearerRefusal.TENANT_MISMATCH)
    if revoked(claims):
        raise BearerRefused(BearerRefusal.REVOKED)
    scopes = claims.get("scopes", claims.get("scope", ()))
    if isinstance(scopes, str):
        scopes = scopes.split()
    session_ref = _claim(claims.get("session_ref")) or None
    return VerifiedBearer(
        subject=_claim(claims.get("sub")),
        tenant=tenant,
        client=_claim(claims.get("client_id", claims.get("azp"))),
        scopes=tuple(sorted(str(scope) for scope in scopes or ())),
        expires_at=expires,
        session_ref=session_ref,
        evidence={
            "source": "bearer",
            "issuer_profile": profile.profile_id,
            "key_id": _claim(claims.get("_kid")),
        },
    )


def _decode(
    token: str, keys: Mapping[str, Any], profile: BearerVerificationProfile
) -> dict[str, Any]:
    """Signature check only: claims are judged by the caller in the spec's order."""
    from joserfc import jwt
    from joserfc.errors import JoseError
    from joserfc.jwk import KeySet

    if len(token.encode("utf-8", "ignore")) > _MAX_TOKEN_BYTES:
        raise BearerRefused(BearerRefusal.SIGNATURE_INVALID)
    try:
        key_set = KeySet.import_key_set(dict(keys))
        decoded = jwt.decode(token, key_set, algorithms=list(profile.algorithms))
    except (JoseError, ValueError, TypeError, KeyError):
        raise BearerRefused(BearerRefusal.SIGNATURE_INVALID) from None
    claims = dict(decoded.claims)
    claims["_kid"] = str(decoded.header.get("kid") or "")
    return claims


def bearer_evidence(
    *,
    request_id: str,
    outcome: VerifiedBearer | BearerRefused,
    profile: BearerVerificationProfile,
    policy_revision: str = "",
    principal: str = "",
    tenant: str = "",
    client: str = "",
) -> dict[str, str]:
    """The audit event of one bearer decision; it never holds token text."""
    event = {
        "source": "bearer",
        "request_id": request_id,
        "issuer_profile": profile.profile_id,
        "policy_revision": policy_revision,
    }
    if isinstance(outcome, VerifiedBearer):
        event.update(
            principal=outcome.subject,
            tenant=outcome.tenant,
            client=outcome.client,
            key_id=str(outcome.evidence.get("key_id", "")),
            result_status="verified",
        )
    else:
        event.update(
            principal=principal,
            tenant=tenant,
            client=client,
            refusal_code=outcome.refusal.value,
            result_status="refused",
        )
    return event


def profile_from_settings() -> BearerVerificationProfile | None:
    """The deployment's bearer profile, or ``None`` when no local issuer is configured."""
    from arbora.security.settings import security_settings

    config = security_settings()
    issuer = str(config.auth_jwt_issuer or "").strip()
    audience = str(config.auth_jwt_audience or "").strip()
    authority = str(config.auth_jwt_jwks_uri or "").strip()
    if not (issuer and audience and authority):
        return None
    return BearerVerificationProfile(
        issuer=issuer, audience=audience, key_authority=authority
    )


async def precheck_bearer(
    token: str,
    *,
    key_cache: JwksCache,
    emit: Callable[[Mapping[str, str]], None] | None = None,
    request_id: str = "",
    fetch_keys: Callable[[str], Any] | None = None,
    now: float | None = None,
) -> VerifiedBearer | None:
    """Run :func:`verify_bearer` ahead of the engine authority resolution.

    Both ingress paths call this before ``verify_local_bearer_token``. It answers
    ``None`` only when the deployment configures no local issuer (the engine
    resolution then refuses as before); an unreadable key authority refuses
    ``AUTHORITY_UNAVAILABLE`` and a decoded-claims helper never admits a bearer.
    """
    import time as _time

    profile = profile_from_settings()
    if profile is None:
        return None
    keys: Mapping[str, Any] | None
    try:
        if fetch_keys is None:
            from arbora.security.auth import _fetch_jwks

            fetch_keys = partial(_fetch_jwks, key_cache)
        keys = await fetch_keys(profile.key_authority)
    except Exception:  # noqa: BLE001 - an unreadable authority admits nothing
        keys = None
    try:
        verified = verify_bearer(
            token, profile, _time.time() if now is None else now, keys=keys
        )
    except BearerRefused as refused:
        if emit is not None:
            emit(
                bearer_evidence(request_id=request_id, outcome=refused, profile=profile)
            )
        raise
    if emit is not None:
        emit(bearer_evidence(request_id=request_id, outcome=verified, profile=profile))
    return verified
