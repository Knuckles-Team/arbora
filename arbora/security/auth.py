#!/usr/bin/python
from __future__ import annotations

"""JWT bearer authentication for HTTP and WebSocket boundaries.

CONCEPT:MERISTEM-OS.config.secrets-authentication — Secrets & Authentication

Validates bearer tokens against a configured JWKS endpoint. Compatible with
OIDC providers including Azure AD, Okta, Keycloak, and Auth0. Loopback-only
listeners may run without authentication; non-loopback listeners require this
JWT boundary.
"""


import json
import logging
import time
from typing import Any, cast
from urllib.parse import urlsplit

from arbora.security.key_cache import JwksCache

logger = logging.getLogger(__name__)

HTTP_401_UNAUTHORIZED = 401
HTTP_500_INTERNAL_SERVER_ERROR = 500


class TokenValidationError(Exception):
    """A bearer token failed validation (``status_code`` 401) or the
    verification path itself failed (``status_code`` 500).

    Framework-free: the serving boundary (arbora) maps it to its own
    transport response; the harness never serves requests.
    """

    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = dict(headers or {})


_JWKS_MAX_BYTES = 1024 * 1024
_MAX_CREDENTIAL_BYTES = 16_384


def parse_bearer_authorization(authorization: list[bytes]) -> str | None:
    """Parse one unambiguous ASGI ``Authorization: Bearer`` credential.

    The raw-header boundary is intentionally shared by every HTTP/MCP caller.
    Duplicate, oversized, non-ASCII, unsupported, and whitespace-ambiguous
    credentials fail closed before any prevalidated identity can be reused.
    """
    if len(authorization) > 1:
        raise PermissionError("ambiguous credentials")
    if not authorization:
        return None
    value = authorization[0]
    if not isinstance(value, bytes) or len(value) > _MAX_CREDENTIAL_BYTES:
        raise PermissionError("invalid credential size")
    try:
        raw = value.decode("ascii")
    except UnicodeDecodeError:
        raise PermissionError("malformed authorization") from None
    scheme, separator, token = raw.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not token
        or any(ord(character) < 33 or ord(character) == 127 for character in token)
    ):
        raise PermissionError("malformed authorization")
    return token


# ---------------------------------------------------------------------------
# JWKS Fetching & Caching
# ---------------------------------------------------------------------------


def _require_jwks_uri(jwks_uri: str) -> None:
    """Refuse a JWKS endpoint that is not an absolute, credential-free HTTPS URL."""
    parsed = urlsplit(jwks_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("AUTH_JWT_JWKS_URI must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("AUTH_JWT_JWKS_URI cannot contain credentials or a fragment")
    if parsed.scheme != "https" and parsed.hostname.lower() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise RuntimeError("JWKS transport requires HTTPS outside loopback")


async def _download_jwks(jwks_uri: str) -> Any:
    """The parsed JWKS document, read under the response size bound."""
    from arbora.security.oidc_discovery import oidc_async_http_client

    async with oidc_async_http_client(timeout=10) as client:
        body = bytearray()
        async with client.stream("GET", jwks_uri) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _JWKS_MAX_BYTES:
                    raise RuntimeError(
                        "JWKS response exceeds the configured safety bound"
                    )
    return json.loads(body)


def _checked_jwks(document: Any) -> dict[str, Any]:
    """The document when it is a key set of one to a hundred keys, else a refusal."""
    entries = document.get("keys") if isinstance(document, dict) else None
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= 100
        or not all(isinstance(entry, dict) for entry in entries)
    ):
        raise RuntimeError("JWKS response has an invalid shape")
    return document


async def fetch_jwks(keys: JwksCache, jwks_uri: str) -> Any:
    """Fetch and cache JSON Web Key Set from a remote URI.

    Args:
        keys: The host's key cache, keyed by endpoint so a runtime configuration
            change can never validate against the wrong issuer.
        jwks_uri: URL of the JWKS endpoint.

    Returns:
        The parsed JWKS dict.
    """
    now = time.monotonic()
    cached = keys.get(jwks_uri, now)
    if cached is not None:
        return cached
    _require_jwks_uri(jwks_uri)
    try:
        document = _checked_jwks(await _download_jwks(jwks_uri))
        keys.put(jwks_uri, document, now)
        logger.debug("JWKS refreshed (%d keys)", len(document["keys"]))
        return document
    except Exception:
        # Endpoint details and response bodies can contain deployment-specific
        # identifiers. Keep logs stable and fail closed once a cache entry expires.
        logger.warning("Failed to refresh the configured JWKS endpoint")
        raise


def _decode_jwt(
    token: str, jwks: dict, *, issuer: str | None, audience: str | None
) -> dict:
    """Decode and validate a JWT token using authlib.

    CONCEPT:MERISTEM-OS.config.secrets-authentication — Secrets & Authentication

    Args:
        token: The raw JWT string.
        jwks: The JWKS dict containing public keys.
        issuer: Expected issuer claim (optional).
        audience: Expected audience claim (optional).

    Returns:
        The validated claims dict.

    Raises:
        TokenValidationError: If the token is invalid, expired, or claims fail validation.
    """
    try:
        from joserfc import jwt
        from joserfc.errors import (
            BadSignatureError,
            DecodeError,
            ExpiredTokenError,
            InvalidClaimError,
        )
        from joserfc.jwk import KeySet
    except ImportError as exc:
        raise TokenValidationError(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The base installation is incomplete: joserfc is required "
            "for JWT authentication.",
        ) from exc

    try:
        # Import the keyset
        key_set = KeySet.import_key_set(cast(Any, jwks))

        # Decode with signature verification
        from arbora.security.settings import security_settings

        config = security_settings()

        token_obj = jwt.decode(
            token,
            key_set,
            algorithms=config.auth_jwt_algorithms,
        )
        claims = token_obj.claims

        # Build validation options
        options: dict[str, Any] = {"exp": {"essential": True}}
        if issuer:
            options["iss"] = {"essential": True, "value": issuer}
        if audience:
            options["aud"] = {"essential": True, "value": audience}

        registry = jwt.JWTClaimsRegistry(leeway=30, **options)
        registry.validate(claims)

        # Manual issuer/audience check (joserfc validation handles this, but keep manual as fallback/strict check)
        if issuer and claims.get("iss") != issuer:
            raise InvalidClaimError("iss")
        if audience:
            aud = claims.get("aud")
            if isinstance(aud, list):
                if audience not in aud:
                    raise InvalidClaimError("aud")
            elif aud != audience:
                raise InvalidClaimError("aud")

        return dict(claims)

    except ExpiredTokenError:
        raise TokenValidationError(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except (BadSignatureError, DecodeError):
        raise TokenValidationError(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except InvalidClaimError:
        raise TokenValidationError(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except Exception as e:
        logger.warning("JWT validation failed")
        raise TokenValidationError(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Token validation failed",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def authenticate_header_values(
    keys: JwksCache,
    *,
    authorization: list[bytes],
) -> dict[str, Any] | None:
    """Authenticate raw ASGI credential headers without route dependencies.

    This is shared by the outer ASGI boundary that protects mounted A2A/ACP/custom applications and WebSockets. Duplicate,
    oversized, malformed, or unsupported credential headers fail closed.
    """
    from arbora.security.settings import security_settings

    config = security_settings()

    token = parse_bearer_authorization(authorization)
    if not config.auth_jwt_jwks_uri:
        return None

    if token is not None:
        jwks = await fetch_jwks(keys, config.auth_jwt_jwks_uri)
        try:
            claims = _decode_jwt(
                token,
                jwks,
                issuer=config.auth_jwt_issuer,
                audience=config.auth_jwt_audience,
            )
        except TokenValidationError as exc:
            # Only a genuine credential rejection (401) collapses to
            # PermissionError here. A verification-path fault that is NOT a
            # credential rejection (currently only the 500 `_decode_jwt` raises
            # when the JWT stack's dependency is missing) must propagate
            # as-is — collapsing it into "invalid bearer token" is exactly how
            # a missing dependency gets misreported as a rejected credential.
            if exc.status_code == HTTP_401_UNAUTHORIZED:
                raise PermissionError("invalid bearer token") from exc
            raise
        return {**claims, "auth_type": "jwt"}

    raise PermissionError("authentication required")
