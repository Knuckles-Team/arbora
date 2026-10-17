"""Security settings read from the process environment at call time.

The security modules read their settings through :func:`security_settings`, so
a test or an operator change to the environment applies on the next call.
Each field names the environment variable it reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_connector_sdk.config import setting

__all__ = ["SecuritySettings", "security_settings", "setting"]

#: Asymmetric JWT signature algorithms accepted when none are configured.
DEFAULT_JWT_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)


def _text(key: str) -> str | None:
    value = str(setting(key, "") or "").strip()
    return value or None


def _list(key: str) -> list[str]:
    raw = str(setting(key, "") or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        parsed = json.loads(raw)
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _mapping(key: str) -> Any:
    raw = str(setting(key, "") or "").strip()
    return json.loads(raw) if raw else None


@dataclass(frozen=True)
class SecuritySettings:
    """One immutable snapshot of the security settings."""

    auth_jwt_jwks_uri: str | None = None  # AUTH_JWT_JWKS_URI
    auth_jwt_issuer: str | None = None  # AUTH_JWT_ISSUER
    auth_jwt_audience: str | None = None  # AUTH_JWT_AUDIENCE
    auth_jwt_algorithms: list[str] = field(
        default_factory=lambda: list(DEFAULT_JWT_ALGORITHMS)
    )  # AUTH_JWT_ALGORITHMS
    mcp_jwt_audience: str | None = None  # FASTMCP_SERVER_AUTH_JWT_AUDIENCE
    kg_policy_version: str | None = None  # KG_POLICY_VERSION
    kg_auth_token_ref: str | None = None  # KG_AUTH_TOKEN_REF
    kg_identity_oauth2: dict[str, Any] | None = None  # KG_IDENTITY_OAUTH2
    deployment_profile: str = "tiny"  # DEPLOYMENT_PROFILE
    graph_service_endpoints: list[str] | None = None  # GRAPH_SERVICE_ENDPOINTS
    identity_group_capability_map: dict[str, list[str]] | None = None
    engine_breaker_cooldown: float = 15.0  # ENGINE_BREAKER_COOLDOWN
    oidc_tls_profile: str | None = None  # OIDC_TLS_PROFILE
    oidc_tls_profile_ref: str | None = None  # OIDC_TLS_PROFILE_REF
    oidc_http_allowed_private_hosts: list[str] = field(default_factory=list)


def security_settings() -> SecuritySettings:
    """Read the security settings from the environment now."""
    return SecuritySettings(
        auth_jwt_jwks_uri=_text("AUTH_JWT_JWKS_URI"),
        auth_jwt_issuer=_text("AUTH_JWT_ISSUER"),
        auth_jwt_audience=_text("AUTH_JWT_AUDIENCE"),
        auth_jwt_algorithms=_list("AUTH_JWT_ALGORITHMS")
        or list(DEFAULT_JWT_ALGORITHMS),
        mcp_jwt_audience=_text("FASTMCP_SERVER_AUTH_JWT_AUDIENCE"),
        kg_policy_version=_text("KG_POLICY_VERSION"),
        kg_auth_token_ref=_text("KG_AUTH_TOKEN_REF"),
        kg_identity_oauth2=_mapping("KG_IDENTITY_OAUTH2"),
        deployment_profile=_text("DEPLOYMENT_PROFILE") or "tiny",
        graph_service_endpoints=_list("GRAPH_SERVICE_ENDPOINTS") or None,
        identity_group_capability_map=_mapping("IDENTITY_GROUP_CAPABILITY_MAP"),
        engine_breaker_cooldown=float(setting("ENGINE_BREAKER_COOLDOWN", 15.0)),
        oidc_tls_profile=_text("OIDC_TLS_PROFILE"),
        oidc_tls_profile_ref=_text("OIDC_TLS_PROFILE_REF"),
        oidc_http_allowed_private_hosts=_list("OIDC_HTTP_ALLOWED_PRIVATE_HOSTS"),
    )
