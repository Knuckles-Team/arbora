"""The MCP server's authentication configuration: JWT, static, OAuth proxy, OIDC proxy and remote OAuth (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, cast
from urllib.parse import urlsplit

from arbora.core.environment import read_env, read_reference
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.backing.server_filters import (
    UnsupportedAuthTypeError,
    _split_csv,
)
from arbora.security.delegation_config import McpAuthConfig
from arbora.security.key_cache import JwksCache

logger = logging.getLogger(__name__)


def _is_malformed_numeric_claim(value: Any) -> bool:
    """True if `value` cannot be trusted as a finite numeric timestamp claim
    (bool masquerading as int, wrong type, or non-finite)."""
    import math

    return (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
    )


def _exp_claim_invalid(claims: dict, now: float) -> bool:
    # `Any`, not the inferred `Any | None`: `_is_malformed_numeric_claim`
    # short-circuits every non-numeric value (None included) before the
    # `float(...)`, but that narrowing lives in the callee, so the annotation
    # has to say what a JWT claim actually is — untyped.
    exp: Any = claims.get("exp")
    return _is_malformed_numeric_claim(exp) or float(exp) < now


def _nbf_iat_claims_invalid(claims: dict, now: float) -> bool:
    for field in ("nbf", "iat"):
        value = claims.get(field)
        if value is None:
            continue
        if _is_malformed_numeric_claim(value) or float(value) > now + 30.0:
            return True
    return False


def _hardened_jwt_verifier(config: RuntimeConfig, **kwargs: Any) -> Any:
    """Build FastMCP's verifier with pinned JWKS I/O and privacy-safe logging."""
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    # The verifier owns the key sets it fetches; no other component shares them.
    owned_keys = JwksCache(config)

    class _Verifier(JWTVerifier):
        async def _fetch_jwks(self) -> dict[str, Any]:
            if not self.jwks_uri:
                raise ValueError("JWKS URI is not configured")
            from arbora.security.auth import fetch_jwks

            document = await fetch_jwks(owned_keys, str(self.jwks_uri))
            return dict(document)

        async def verify_token(self, token: str) -> Any:
            if not isinstance(token, str) or not token or len(token.encode()) > 16_384:
                return None
            result = await super().verify_token(token)
            if result is None:
                return None
            claims = getattr(result, "claims", None)
            if not isinstance(claims, dict):
                return None
            import time

            now = time.time()
            if _exp_claim_invalid(claims, now) or _nbf_iat_claims_invalid(claims, now):
                return None
            return result

    class _PrivacyLogger:
        def debug(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            logger.debug("JWT verifier diagnostic")

        def info(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            logger.info("JWT verifier event")

        def warning(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            logger.warning("JWT verifier rejected a credential")

        def error(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            logger.error("JWT verifier error")

        def exception(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            logger.error("JWT verifier error")

        def isEnabledFor(self, level: int) -> bool:  # noqa: N802
            return logger.isEnabledFor(level)

    verifier = _Verifier(**kwargs)
    # _PrivacyLogger is a deliberate duck-typed shim (redacts JWT-adjacent log
    # content) satisfying the same debug/info/warning/error/isEnabledFor surface
    # JWTVerifier calls -- not a real logging.Logger subclass, so it is cast
    # rather than nominally typed.
    verifier.logger = cast("logging.Logger", _PrivacyLogger())
    return verifier


def _secure_auth_url(value: Any, *, field: str) -> str:
    rendered = str(value or "").strip()
    if len(rendered) > 8_192:
        raise ValueError(f"{field} is too large")
    parsed = urlsplit(rendered)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field} is invalid")
    if parsed.scheme.lower() == "http" and parsed.hostname.lower() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError(f"{field} requires HTTPS outside loopback")
    return rendered


def _validated_redirect_uris(value: str | None) -> list[str] | None:
    if not value:
        return None
    uris = [part.strip() for part in value.split(",") if part.strip()]
    if not uris or len(uris) > 32:
        raise ValueError("redirect URI allowlist is invalid")
    return [_secure_auth_url(uri, field="redirect URI") for uri in uris]


def _signin_scopes() -> list[str]:
    """Scopes that a client should request at sign-in (ARBORA-IDENTITY-R030.4.8).

    The list is the native client's sign-in scopes from the one declared client
    contract. It reaches the protected-resource metadata (``scopes_supported``)
    and the ``scope`` parameter of the 401 challenge, so an MCP client requests
    ``offline_access`` and receives a durable refresh token. It is advertising
    only: the verifier keeps its own ``required_scopes``, so a token without
    these scopes still passes.
    """
    from arbora.identity.ports import native_signin_scopes

    return list(native_signin_scopes())


def _wrap_with_resource_metadata(
    verifier: Any, args: argparse.Namespace, issuer_values: list[str]
) -> Any:
    """Compose *verifier* with RFC 9728 protected-resource metadata when a public
    base URL is configured (CONCEPT:MERISTEM-OS.identity.protected-resource-metadata).

    A bare ``TokenVerifier`` (what plain ``jwt`` auth builds) publishes zero OAuth
    routes and never appends ``resource_metadata`` to its 401 challenge, so an
    RFC 9728-aware MCP client (e.g. Claude Code) has no way to discover the
    authorization server and refresh its own token — it is handed a static JWT
    that silently expires. Wrapping the same verifier in ``RemoteAuthProvider``
    adds the ``/.well-known/oauth-protected-resource`` route and the
    ``resource_metadata`` challenge param with no change to token verification
    itself (``RemoteAuthProvider.verify_token`` just delegates to *verifier*).

    With no public base URL configured, *verifier* is returned unwrapped — today's
    exact behavior, so this is additive and fully backward compatible.
    """
    import sys as _sys

    base_url = str(getattr(args, "public_base_url", "") or "").strip()
    if not base_url:
        return verifier
    try:
        base_url = _secure_auth_url(base_url, field="MCP public base URL")
    except ValueError:
        logger.error("Error: MCP public base URL policy is invalid")
        _sys.exit(1)

    from fastmcp.server.auth import RemoteAuthProvider
    from pydantic import AnyHttpUrl

    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(u) for u in issuer_values],
        base_url=base_url,
        scopes_supported=_signin_scopes(),
        challenge_scopes=_signin_scopes(),
    )


def _apply_mcp_auth_config_overrides(
    args: argparse.Namespace, delegation: McpAuthConfig
) -> None:
    """Layer parsed CLI auth/delegation flags onto the host's ``delegation`` settings."""
    delegation.set("enable_delegation", args.enable_delegation)
    delegation.set("audience", args.audience or delegation["audience"])
    delegation.set(
        "delegated_scopes", args.delegated_scopes or delegation["delegated_scopes"]
    )
    for key in ("oidc_config_url", "oidc_client_id", "oidc_client_secret"):
        if hasattr(args, key):
            delegation.set(key, getattr(args, key) or delegation[key])


def _discover_delegation_token_endpoint(config_url: str, config: RuntimeConfig) -> str:
    """OIDC-discover the token endpoint for token delegation. Raises
    ValueError on any failure (unavailable issuer, no token_endpoint)."""
    if not isinstance(config_url, str):
        raise ValueError("oidc_config_url must be a string")
    suffix = "/.well-known/openid-configuration"
    issuer = config_url[: -len(suffix)] if config_url.endswith(suffix) else config_url

    from arbora.security.oidc_discovery import discover

    oidc_config = discover(issuer, config)
    token_endpoint = oidc_config.get("token_endpoint")
    if not token_endpoint:
        raise ValueError("No token_endpoint found in OIDC configuration")
    return token_endpoint


def _enforce_delegation_requirements(
    args: argparse.Namespace, delegation: McpAuthConfig, config: RuntimeConfig
) -> None:
    """When token delegation is enabled, require oidc-proxy + a complete
    OIDC configuration, and resolve the delegation token endpoint."""
    if not delegation["enable_delegation"]:
        return
    if args.auth_type != "oidc-proxy":
        logger.error("Error: Token delegation requires auth-type=oidc-proxy")
        sys.exit(1)
    if not delegation["audience"]:
        logger.error("Error: audience is required for delegation")
        sys.exit(1)
    if not all(
        [
            delegation["oidc_config_url"],
            delegation["oidc_client_id"],
            delegation["oidc_client_secret"],
        ]
    ):
        logger.error("Error: Delegation requires complete OIDC configuration")
        sys.exit(1)
    try:
        delegation.set(
            "token_endpoint",
            _discover_delegation_token_endpoint(delegation["oidc_config_url"], config),
        )
    except Exception as exc:
        logger.error(
            "Failed to fetch OIDC configuration (exception_type=%s)",
            type(exc).__name__,
            exc_info=True,
        )
        sys.exit(1)


def _validate_allowed_redirect_uris(args: argparse.Namespace) -> list[str] | None:
    try:
        return _validated_redirect_uris(args.allowed_client_redirect_uris)
    except ValueError:
        logger.error("Error: allowed client redirect URI policy is invalid")
        sys.exit(1)


def _resolve_static_tokens_ref(reference: str) -> Any:
    if reference.startswith("env://"):
        return read_reference(reference[len("env://") :])
    from arbora.security.secrets import create_secrets_client

    return create_secrets_client().resolve_ref(reference)


def _validate_static_token_string(token: Any) -> None:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 4096:
        raise ValueError("invalid token")


def _validate_static_claims_shape(claims: Any) -> tuple[str, list]:
    if not isinstance(claims, dict):
        raise ValueError("invalid claims")
    client_id = claims.get("client_id")
    scopes = claims.get("scopes", [])
    if not isinstance(client_id, str) or not 1 <= len(client_id) <= 256:
        raise ValueError("invalid client id")
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) and 1 <= len(scope) <= 256 for scope in scopes
    ):
        raise ValueError("invalid scopes")
    return client_id, scopes


def _validate_static_expiry(claims: dict) -> Any:
    expires_at = claims.get("expires_at")
    if expires_at is None:
        return None
    import math

    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int | float)
        or not math.isfinite(float(expires_at))
    ):
        raise ValueError("invalid expiry")
    return expires_at


def _validate_one_static_token_entry(token: Any, claims: Any) -> tuple[str, dict]:
    _validate_static_token_string(token)
    client_id, scopes = _validate_static_claims_shape(claims)
    expires_at = _validate_static_expiry(claims)
    entry: dict[str, Any] = {"client_id": client_id, "scopes": scopes}
    if expires_at is not None:
        entry["expires_at"] = expires_at
    return token, entry


def _validate_static_token_map(token_map: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(token_map, dict) or not token_map:
        raise ValueError("empty token map")
    validated: dict[str, dict[str, Any]] = {}
    for token, claims in token_map.items():
        key, entry = _validate_one_static_token_entry(token, claims)
        validated[key] = entry
    return validated


def _configure_static_auth(args: argparse.Namespace) -> Any:
    reference = str(getattr(args, "static_tokens_ref", "") or "").strip()
    if not reference:
        logger.error(
            "Error: static auth requires --static-tokens-ref; inline and "
            "built-in tokens are not permitted"
        )
        sys.exit(1)
    try:
        import json as _json

        raw_tokens = _resolve_static_tokens_ref(reference)
        token_map = _json.loads(str(raw_tokens or ""))
        validated = _validate_static_token_map(token_map)
    except Exception as exc:
        logger.error(
            "Error: static authentication token reference is unavailable or invalid (%s)",
            type(exc).__name__,
            exc_info=True,
        )
        sys.exit(1)

    import hashlib as _hashlib
    import hmac as _hmac
    import secrets as _secrets
    import time as _time

    from fastmcp.server.auth import AccessToken, TokenVerifier

    class _ConstantTimeStaticVerifier(TokenVerifier):
        def __init__(self, tokens: dict[str, dict[str, Any]]) -> None:
            super().__init__()
            self._key = _secrets.token_bytes(32)
            self._entries = [
                (
                    _hmac.new(
                        self._key,
                        token.encode("utf-8"),
                        _hashlib.sha256,
                    ).digest(),
                    dict(claims),
                )
                for token, claims in tokens.items()
            ]

        async def verify_token(self, token: str) -> Any:
            if not isinstance(token, str) or len(token) > 4_096:
                return None
            candidate = _hmac.new(
                self._key,
                token.encode("utf-8"),
                _hashlib.sha256,
            ).digest()
            matched: dict[str, Any] | None = None
            for expected, claims in self._entries:
                if _secrets.compare_digest(candidate, expected):
                    matched = claims
            if matched is None:
                return None
            expires_at = matched.get("expires_at")
            if expires_at is not None and float(expires_at) < _time.time():
                return None
            return AccessToken(
                token=token,
                client_id=matched["client_id"],
                scopes=list(matched.get("scopes", [])),
                expires_at=int(expires_at) if expires_at is not None else None,
                claims=matched,
            )

    return _ConstantTimeStaticVerifier(validated)


def _configure_oauth_proxy_auth(
    args: argparse.Namespace, allowed_uris: list[str] | None, config: RuntimeConfig
) -> Any:
    from fastmcp.server.auth import OAuthProxy

    if not all(
        [
            args.oauth_upstream_auth_endpoint,
            args.oauth_upstream_token_endpoint,
            args.oauth_upstream_client_id,
            args.oauth_upstream_client_secret,
            args.oauth_base_url,
            args.token_jwks_uri,
            args.token_issuer,
            args.token_audience,
        ]
    ):
        logger.error(
            "Error: oauth-proxy requires all upstream endpoints and JWT params"
        )
        sys.exit(1)
    try:
        upstream_auth = _secure_auth_url(
            args.oauth_upstream_auth_endpoint,
            field="OAuth authorization endpoint",
        )
        upstream_token = _secure_auth_url(
            args.oauth_upstream_token_endpoint,
            field="OAuth token endpoint",
        )
        base_url = _secure_auth_url(args.oauth_base_url, field="OAuth base URL")
    except ValueError:
        logger.error("Error: OAuth proxy URL policy is invalid")
        sys.exit(1)
    token_verifier = _hardened_jwt_verifier(
        config,
        jwks_uri=args.token_jwks_uri,
        issuer=args.token_issuer,
        audience=args.token_audience,
    )
    return OAuthProxy(
        upstream_authorization_endpoint=upstream_auth,
        upstream_token_endpoint=upstream_token,
        upstream_client_id=args.oauth_upstream_client_id,
        upstream_client_secret=args.oauth_upstream_client_secret,
        token_verifier=token_verifier,
        base_url=base_url,
        allowed_client_redirect_uris=allowed_uris,
    )


def _configure_oidc_proxy_auth(
    args: argparse.Namespace, allowed_uris: list[str] | None
) -> Any:
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    if not all(
        [
            args.oidc_config_url,
            args.oidc_client_id,
            args.oidc_client_secret,
            args.oidc_base_url,
        ]
    ):
        logger.error(
            "Error: oidc-proxy requires OIDC metadata, client identity, "
            "a resolved client-secret reference, and base URL"
        )
        sys.exit(1)
    inbound_audience = args.token_audience or args.audience
    if not inbound_audience:
        logger.error("Error: oidc-proxy requires an explicit token audience")
        sys.exit(1)
    try:
        config_url = _secure_auth_url(
            args.oidc_config_url, field="OIDC configuration URL"
        )
        base_url = _secure_auth_url(args.oidc_base_url, field="OIDC base URL")
    except ValueError:
        logger.error("Error: OIDC proxy URL policy is invalid")
        sys.exit(1)
    return OIDCProxy(
        config_url=config_url,
        client_id=args.oidc_client_id,
        client_secret=args.oidc_client_secret,
        audience=inbound_audience,
        base_url=base_url,
        allowed_client_redirect_uris=allowed_uris,
    )


def _configure_remote_oauth_auth(
    args: argparse.Namespace, config: RuntimeConfig
) -> Any:
    from fastmcp.server.auth import RemoteAuthProvider
    from pydantic import AnyHttpUrl

    if not all(
        [
            args.remote_auth_servers,
            args.remote_base_url,
            args.token_jwks_uri,
            args.token_issuer,
            args.token_audience,
        ]
    ):
        logger.error(
            "Error: remote-oauth requires remote-auth-servers, remote-base-url, and JWT params"
        )
        sys.exit(1)
    try:
        auth_servers = [
            _secure_auth_url(url.strip(), field="authorization server")
            for url in args.remote_auth_servers.split(",")
            if url.strip()
        ]
        remote_base_url = _secure_auth_url(
            args.remote_base_url, field="remote OAuth base URL"
        )
    except ValueError:
        logger.error("Error: remote OAuth URL policy is invalid")
        sys.exit(1)
    if not auth_servers or len(auth_servers) > 16:
        logger.error("Error: remote authorization-server policy is invalid")
        sys.exit(1)
    token_verifier = _hardened_jwt_verifier(
        config,
        jwks_uri=args.token_jwks_uri,
        issuer=args.token_issuer,
        audience=args.token_audience,
    )
    return RemoteAuthProvider(
        token_verifier=token_verifier,
        authorization_servers=[AnyHttpUrl(u) for u in auth_servers],
        base_url=remote_base_url,
        scopes_supported=_signin_scopes(),
        challenge_scopes=_signin_scopes(),
    )


def _configure_auth(
    args: argparse.Namespace, delegation: McpAuthConfig, config: RuntimeConfig
) -> Any:
    """Configure authentication provider based on parsed CLI args.

    Returns the auth provider instance, or ``None`` only for the explicitly
    recognised "no authentication" values (``"none"`` / empty). Any other
    unrecognised ``auth_type`` raises :class:`UnsupportedAuthTypeError` rather
    than degrading to an unauthenticated server (BUG-CX-023).
    """
    if args.auth_type == "none" or not args.auth_type:
        return None

    _apply_mcp_auth_config_overrides(args, delegation)
    _enforce_delegation_requirements(args, delegation, config)
    allowed_uris = _validate_allowed_redirect_uris(args)

    if args.auth_type == "none":
        return None
    if args.auth_type == "static":
        return _configure_static_auth(args)
    if args.auth_type == "jwt":
        return _configure_jwt_auth(args, config)
    if args.auth_type == "oauth-proxy":
        return _configure_oauth_proxy_auth(args, allowed_uris, config)
    if args.auth_type == "oidc-proxy":
        return _configure_oidc_proxy_auth(args, allowed_uris)
    if args.auth_type == "remote-oauth":
        return _configure_remote_oauth_auth(args, config)
    logger.error(
        "Refusing to build an MCP server for unsupported auth type %r", args.auth_type
    )
    raise UnsupportedAuthTypeError(
        f"unsupported auth type {args.auth_type!r}; expected one of "
        "none, static, jwt, oauth-proxy, oidc-proxy, remote-oauth"
    )


def _resolve_jwt_basic_params(
    args: argparse.Namespace,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """(jwks_uri, issuer, audience, algorithm, secret_or_key) from CLI args
    with their environment-variable fallbacks. OIDC_ISSUER is the
    canonical, provider-agnostic var; FASTMCP_SERVER_AUTH_JWT_ISSUER is the
    per-server alias. Either may be a comma-separated multi-issuer list
    (OS-5.45)."""
    jwks_uri = args.token_jwks_uri or read_env("FASTMCP_SERVER_AUTH_JWT_JWKS_URI")
    issuer = (
        read_env("OIDC_ISSUER")
        or args.token_issuer
        or read_env("FASTMCP_SERVER_AUTH_JWT_ISSUER")
    )
    audience = args.token_audience or read_env("FASTMCP_SERVER_AUTH_JWT_AUDIENCE")
    algorithm = args.token_algorithm
    secret_or_key = args.token_public_key
    return jwks_uri, issuer, audience, algorithm, secret_or_key


def _oidc_discover_jwks_uris(issuer: str, config: RuntimeConfig) -> list[str | None]:
    from arbora.security.oidc_discovery import jwks_uri_for

    return [
        jwks_uri_for(i.strip(), config) for i in str(issuer).split(",") if i.strip()
    ]


def _maybe_discover_jwks(
    jwks_uri: str | None,
    secret_or_key: str | None,
    args: argparse.Namespace,
    issuer: str | None,
    config: RuntimeConfig,
) -> str | None:
    """CONCEPT:MERISTEM-OS.identity.resolve-token-endpoint-from -- IdP-agnostic:
    with no explicit JWKS URI (and not a static key), resolve it from each
    issuer's OIDC discovery document, so config carries only the issuer
    (Keycloak, Okta, Auth0, Entra, ...) -- never a vendor-specific path.

    ``args.token_secret_ref`` is read lazily, INSIDE this same short-
    circuited condition (not pre-extracted at the call site), matching the
    original inline expression's evaluation order exactly: a real
    argparse.Namespace always defines the attribute, but a caller with a
    truthy ``jwks_uri``/``secret_or_key`` (as in this repo's own JWT test
    fixtures) never needed to.
    """
    if jwks_uri or secret_or_key or args.token_secret_ref or not issuer:
        return jwks_uri
    resolved = _oidc_discover_jwks_uris(issuer, config)
    if resolved and all(resolved):
        return ",".join(u for u in resolved if u)
    if any(resolved):
        logger.warning(
            "OIDC discovery resolved JWKS for only part of the issuer policy; "
            "configure the JWKS policy explicitly"
        )
    return jwks_uri


def _validate_jwt_key_material_present(
    jwks_uri: str | None, secret_or_key: str | None, args: argparse.Namespace
) -> None:
    """``args.token_secret_ref`` is read lazily -- see :func:`_maybe_discover_jwks`."""
    if not (jwks_uri or secret_or_key or args.token_secret_ref):
        logger.error(
            "Error: JWT auth requires --token-jwks-uri, --token-public-key, "
            "or --token-secret-ref"
        )
        sys.exit(1)


def _validate_jwt_issuer_audience_present(
    issuer: str | None, audience: str | None
) -> None:
    if not (issuer and audience):
        logger.error("Error: JWT requires --token-issuer and --token-audience")
        sys.exit(1)


def _validate_jwt_audience_format(audience: Any) -> None:
    if (
        not isinstance(audience, str)
        or not 1 <= len(audience) <= 512
        or any(character in audience for character in "\r\n\x00")
    ):
        logger.error("Error: JWT audience is invalid")
        sys.exit(1)


def _normalize_jwt_issuer_and_jwks(
    issuer: str | None, jwks_uri: str | None
) -> tuple[str, str | None, list[str]]:
    try:
        issuer_values = [
            _secure_auth_url(value.strip(), field="JWT issuer")
            for value in str(issuer).split(",")
            if value.strip()
        ]
        if not issuer_values:
            raise ValueError("missing issuer")
        issuer = ",".join(issuer_values)
        if jwks_uri:
            jwks_values = [
                _secure_auth_url(value.strip(), field="JWKS URI")
                for value in str(jwks_uri).split(",")
                if value.strip()
            ]
            if not jwks_values:
                raise ValueError("missing JWKS URI")
            jwks_uri = ",".join(jwks_values)
    except ValueError:
        logger.error("Error: JWT issuer/JWKS URL policy is invalid")
        sys.exit(1)
    return issuer, jwks_uri, issuer_values


def _resolve_jwt_public_key_pem(args: argparse.Namespace) -> str | None:
    if args.token_public_key and os.path.isfile(args.token_public_key):
        try:
            with open(args.token_public_key) as f:
                return f.read()
        except (OSError, ValueError) as exc:
            logger.error(
                "Failed to read public key file (exception_type=%s)",
                type(exc).__name__,
                exc_info=True,
            )
            sys.exit(1)
    elif args.token_public_key:
        return args.token_public_key
    return None


def _resolve_jwt_secret_or_key(
    args: argparse.Namespace, algorithm: str | None, public_key_pem: str | None
) -> Any:
    if not (algorithm and algorithm.startswith("HS")):
        return public_key_pem
    secret_ref = str(getattr(args, "token_secret_ref", "") or "").strip()
    if not secret_ref:
        logger.error("Error: HMAC JWT verification requires --token-secret-ref")
        sys.exit(1)
    try:
        from arbora.security.secrets import create_secrets_client

        public_key = create_secrets_client().resolve_ref(secret_ref)
        if not isinstance(public_key, str) or len(public_key.encode()) < 32:
            raise ValueError("weak secret")
    except Exception:
        logger.error("Error: HMAC JWT secret reference is unavailable", exc_info=True)
        sys.exit(1)
    return public_key


def _parse_required_scopes(args: argparse.Namespace) -> list[str] | None:
    if not args.required_scopes:
        return None
    return _split_csv(args.required_scopes)


def _build_multi_realm_verifiers(
    jwks_uris: list[str],
    issuers: list[str],
    audience: str | None,
    algorithm: str | None,
    required_scopes: list[str] | None,
    config: RuntimeConfig,
) -> list[Any]:
    return [
        _hardened_jwt_verifier(
            config,
            jwks_uri=u,
            issuer=i,
            audience=audience,
            algorithm=algorithm or "RS256",
            required_scopes=required_scopes,
        )
        for u, i in zip(jwks_uris, issuers, strict=False)
    ]


def _configure_multi_realm_jwt(
    jwks_uris: list[str],
    issuers: list[str],
    audience: str | None,
    algorithm: str | None,
    required_scopes: list[str] | None,
    args: argparse.Namespace,
    config: RuntimeConfig,
) -> Any:
    """CONCEPT:MERISTEM-OS.identity.native-multi-realm-jwt -- native multi-realm
    JWT trust. FASTMCP_SERVER_AUTH_JWT_ISSUER and _JWKS_URI may each be a
    comma-separated, aligned-by-index list (one Keycloak realm per entry).
    FastMCP's JWTVerifier accepts an issuer list but only a SINGLE
    jwks_uri (one realm's signing keys), so during a realm migration the
    fleet must trust two realms' signing keys at once. Build one
    JWTVerifier per realm and accept a token if ANY verifies it --
    enabling a zero-downtime issuer cutover (add new realm -> flip the
    minter -> drop the old realm) with no signature gap and no auth
    lock-out window."""
    if len(jwks_uris) != len(issuers):
        logger.error(
            "Multi-realm JWT auth requires FASTMCP_SERVER_AUTH_JWT_ISSUER and "
            "FASTMCP_SERVER_AUTH_JWT_JWKS_URI to be comma-separated lists of EQUAL length "
            "(one entry per realm, aligned by index)."
        )
        sys.exit(1)

    from fastmcp.server.auth import TokenVerifier

    class _MultiIssuerVerifier(TokenVerifier):
        """Accept a JWT validated by ANY wrapped per-realm JWTVerifier (CONCEPT:MERISTEM-OS.identity.native-multi-realm-jwt)."""

        def __init__(self, verifiers: list, *, required_scopes: Any = None) -> None:
            super().__init__(required_scopes=required_scopes)
            self._verifiers = list(verifiers)

        async def verify_token(self, token: str) -> Any:
            for _v in self._verifiers:
                try:
                    _result = await _v.verify_token(token)
                except Exception:
                    logging.getLogger(__name__).debug(
                        "try the next realm", exc_info=True
                    )
                    _result = None
                if _result is not None:
                    return _result
            return None

    try:
        verifiers = _build_multi_realm_verifiers(
            jwks_uris, issuers, audience, algorithm, required_scopes, config
        )
        logger.info("JWT auth: native multi-issuer trust configured")
        return _wrap_with_resource_metadata(
            _MultiIssuerVerifier(verifiers, required_scopes=required_scopes),
            args,
            issuers,
        )
    except Exception as exc:
        logger.error(
            "Failed to initialize multi-realm JWTVerifier (exception_type=%s)",
            type(exc).__name__,
            exc_info=True,
        )
        sys.exit(1)


def _configure_single_realm_jwt(
    jwks_uri: str | None,
    public_key: Any,
    issuer: str,
    audience: str | None,
    algorithm: str | None,
    required_scopes: list[str] | None,
    args: argparse.Namespace,
    issuer_values: list[str],
    config: RuntimeConfig,
) -> Any:
    try:
        verifier = _hardened_jwt_verifier(
            config,
            jwks_uri=jwks_uri,
            public_key=public_key,
            issuer=issuer,
            audience=audience,
            algorithm=algorithm or "RS256",
            required_scopes=required_scopes,
        )
        return _wrap_with_resource_metadata(verifier, args, issuer_values)
    except Exception as exc:
        logger.error(
            "Failed to initialize JWTVerifier (exception_type=%s)",
            type(exc).__name__,
            exc_info=True,
        )
        sys.exit(1)


def _configure_jwt_auth(args: argparse.Namespace, config: RuntimeConfig) -> Any:
    """Configure JWT authentication from CLI args."""
    jwks_uri, issuer, audience, algorithm, secret_or_key = _resolve_jwt_basic_params(
        args
    )
    jwks_uri = _maybe_discover_jwks(jwks_uri, secret_or_key, args, issuer, config)
    _validate_jwt_key_material_present(jwks_uri, secret_or_key, args)
    _validate_jwt_issuer_audience_present(issuer, audience)
    _validate_jwt_audience_format(audience)
    issuer, jwks_uri, issuer_values = _normalize_jwt_issuer_and_jwks(issuer, jwks_uri)

    public_key_pem = _resolve_jwt_public_key_pem(args)
    public_key = _resolve_jwt_secret_or_key(args, algorithm, public_key_pem)
    required_scopes = _parse_required_scopes(args)

    issuers = _split_csv(str(issuer)) if issuer else []
    jwks_uris = _split_csv(str(jwks_uri)) if jwks_uri else []
    if len(jwks_uris) > 1 or len(issuers) > 1:
        return _configure_multi_realm_jwt(
            jwks_uris, issuers, audience, algorithm, required_scopes, args, config
        )
    return _configure_single_realm_jwt(
        jwks_uri,
        public_key,
        issuer,
        audience,
        algorithm,
        required_scopes,
        args,
        issuer_values,
        config,
    )
