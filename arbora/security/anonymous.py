"""Explicit tokenless loopback admission (ARBORA-IDENTITY-R004.3).

This is a deployment mode, never an authentication fallback. Anonymous
provenance is kept distinct from verified credentials throughout dispatch.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

PRINCIPAL = "anonymous"
TENANT = "local"
POLICY = "no-auth"
AUDIENCE = "epistemic-graph"
_ACTIVE_MODE: str | None = None


def enabled() -> bool:
    return _ACTIVE_MODE == "none"


def loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_startup(args: Any) -> None:
    """Reject unsafe or ambiguous anonymous configuration before minting state."""
    global _ACTIVE_MODE
    from arbora.core.settings import runtime_config

    mode = runtime_config.arbora_auth_mode
    if _ACTIVE_MODE is not None and _ACTIVE_MODE != mode:
        raise ValueError("Authentication mode changes require a new serving process")
    if mode != "none":
        _ACTIVE_MODE = mode
        return
    from arbora.security.settings import security_settings

    config = security_settings()
    _validate_transport(args)
    _validate_credentials(config)
    _validate_engine_endpoints(runtime_config.graph_service_endpoints or ())

    _ACTIVE_MODE = mode
    import logging

    logging.getLogger(__name__).warning(
        "UNSECURED local anonymous mode: no credentials, loopback access only"
    )


def _validate_transport(args: Any) -> None:
    """Require an unproxied loopback listener without transport credentials."""
    if not loopback(str(getattr(args, "host", ""))):
        raise ValueError("Anonymous mode requires a literal loopback bind")
    if getattr(args, "auth_type", "none") != "none":
        raise ValueError("Anonymous mode conflicts with transport authentication")
    credential_options = (
        "static_tokens_ref",
        "token_jwks_uri",
        "token_issuer",
        "token_audience",
        "token_secret_ref",
        "token_public_key",
        "oauth_upstream_client_id",
        "oauth_upstream_client_secret",
        "oidc_client_id",
        "oidc_client_secret",
        "remote_auth_servers",
    )
    if any(getattr(args, field, None) for field in credential_options):
        raise ValueError("Anonymous mode conflicts with transport credentials")
    if getattr(args, "tls_terminated", False) or getattr(
        args, "trusted_proxy_cidrs", None
    ):
        raise ValueError("Anonymous mode does not admit reverse-proxy exposure")


def _validate_credentials(config: Any) -> None:
    """Keep configured identity and engine secrets out of tokenless mode."""
    from arbora.core.settings import setting

    fields = (
        "auth_jwt_jwks_uri",
        "auth_jwt_issuer",
        "auth_jwt_audience",
        "kg_auth_token_ref",
        "kg_identity_oauth2",
    )
    if any(getattr(config, field, None) for field in fields):
        raise ValueError("Anonymous mode conflicts with credential configuration")
    if setting("GRAPH_SERVICE_AUTH_SECRET", "") or setting(
        "MEDULLA_SIGNER_KEYS_JSON", ""
    ):
        raise ValueError("Anonymous mode must not configure engine secrets")


def _validate_engine_endpoints(endpoints: Any) -> None:
    """Allow only local socket paths and literal loopback engine addresses."""
    for endpoint in endpoints:
        if str(endpoint).startswith(("unix://", "/")):
            continue
        parsed = urlsplit(
            str(endpoint) if "://" in str(endpoint) else "tcp://" + str(endpoint)
        )
        if not loopback(parsed.hostname or ""):
            raise ValueError("Anonymous mode requires a local engine endpoint")


def validate_request(scope: Any) -> None:
    if not enabled():
        raise PermissionError("Anonymous mode is not configured")
    peer = scope.get("client") or ()
    if not peer or not loopback(str(peer[0])):
        raise PermissionError("Anonymous requests require a loopback peer")
    headers = tuple(scope.get("headers", ()))
    if any(k.lower() in {b"authorization", b"cookie", b"origin"} for k, _ in headers):
        raise PermissionError(
            "Anonymous requests must not contain credentials or Origin"
        )
    hosts = [
        v.decode("ascii", errors="strict") for k, v in headers if k.lower() == b"host"
    ]
    if len(hosts) != 1:
        raise PermissionError("One loopback Host is required")
    _validate_host(hosts[0])


def _validate_host(authority: str) -> None:
    """Validate Host syntax before accepting its loopback destination."""
    if any(c in authority for c in "/@?#\\") or any(c.isspace() for c in authority):
        raise PermissionError("Invalid anonymous Host authority")
    try:
        parsed = urlsplit("http://" + authority)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise PermissionError("Invalid anonymous Host") from exc
    if host != "localhost" and not loopback(host or ""):
        raise PermissionError("Anonymous Host must be loopback")


def admitted_actor(actor: Any) -> bool:
    from arbora.security.actor_context import ActorType

    return (
        actor is not None
        and enabled()
        and getattr(actor, "authority_mode", "verified") == "none"
        and actor.authenticated is False
        and actor.actor_type == ActorType.ANONYMOUS
        and _local_actor_authority(actor)
    )


def _local_actor_authority(actor: Any) -> bool:
    """Anonymous authority has fixed identity and no roles or credential lease."""
    return (
        not actor.roles
        and not actor.groups
        and actor.actor_id == PRINCIPAL
        and actor.tenant_id == TENANT
        and not actor.credential_expires_at
        and actor.credential_lease is None
    )


def validate_session(value: Any) -> None:
    from arbora.security.scope_registry import SCOPE_CLASSES

    if (
        not admitted_actor(value.actor)
        or value.tenant != TENANT
        or value.policy_version != POLICY
        or value.audience != AUDIENCE
        or any(SCOPE_CLASSES.get(scope) in {None, "approver"} for scope in value.scopes)
    ):
        raise PermissionError("Anonymous session authority is invalid")


def session() -> Any:
    if not enabled():
        raise PermissionError("Anonymous mode is not configured")
    from arbora.api.contracts.session import GraphSession
    from arbora.security.actor_context import ActorContext, ActorType
    from arbora.security.scope_registry import SCOPE_CLASSES

    scopes = frozenset(
        name for name, kind in SCOPE_CLASSES.items() if kind != "approver"
    )
    actor = ActorContext(
        actor_id=PRINCIPAL,
        actor_type=ActorType.ANONYMOUS,
        tenant_id=TENANT,
        authenticated=False,
        authority_mode="none",
    )
    return GraphSession(
        actor=actor,
        tenant=TENANT,
        graph="tenant__local",
        scopes=scopes,
        policy_version=POLICY,
        audience=AUDIENCE,
    )


def session_context(value: Any) -> dict[str, Any]:
    """Anonymous descriptive routing metadata, or verified wire claims.

    Anonymous metadata is never forwarded as an EG verified context.
    """
    if not admitted_actor(getattr(value, "actor", None)):
        return value.engine_verified_context()
    validate_session(value)
    value.ensure_authority_current()
    return {
        "principal": PRINCIPAL,
        "tenant": TENANT,
        "agent_id": PRINCIPAL,
        "audience": AUDIENCE,
        "policy_version": POLICY,
        "roles": [],
        "scopes": sorted(value.scopes),
        "delegation": [],
        "auth_mode": "none",
    }


def client_scope(client: Any, context: Any) -> Any:
    """Never pass anonymous metadata to the verified-context carrier."""
    import contextlib

    if context.get("auth_mode") == "none":
        if (
            not enabled()
            or getattr(client, "authenticated", None) is not False
            or context.get("principal") != PRINCIPAL
            or context.get("tenant") != TENANT
        ):
            raise PermissionError("Anonymous client mode mismatch")
        return contextlib.nullcontext()
    if getattr(client, "authenticated", None) is False:
        raise PermissionError("Verified authority cannot use anonymous transport")
    return client.use_verified_context(context)


def network_run_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Compose Arbora admission around the SDK-owned network boundary."""
    if not enabled():
        return kwargs
    from starlette.middleware import Middleware

    from arbora.security.request_identity import ActorIdentityMiddleware

    return {
        **kwargs,
        "middleware": [
            Middleware(ActorIdentityMiddleware),
            *kwargs.get("middleware", ()),
        ],
    }
