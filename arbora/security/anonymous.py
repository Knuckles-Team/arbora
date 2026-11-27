"""Explicit tokenless loopback admission (ARBORA-IDENTITY-R004.3).

This is a deployment mode, never an authentication fallback. Anonymous
provenance is kept distinct from verified credentials throughout dispatch.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from arbora.core.settings import RuntimeConfig
from arbora.security.key_cache import JwksCache
from arbora.security.signed_ingress import SignedIngressSlot

PRINCIPAL = "anonymous"
TENANT = "local"
POLICY = "no-auth"
AUDIENCE = "medulla"
_ACTIVE_MODE: str | None = None


def enabled() -> bool:
    return _ACTIVE_MODE == "none"


BANNER = "Arbora is running in unsecured loopback mode."


def indicator(runtime: RuntimeConfig) -> dict[str, str]:
    """The unsecured-mode indicator read by MCP ``initialize`` and ``ops.doctor``.

    Empty outside none mode. ``doctor_check`` is ``fail`` when the exposure
    acknowledgement is set, because anyone who can reach the port is then an
    administrator, and ``warn`` otherwise (ARBORA-IDENTITY-R004.2).
    """
    from arbora.core.environment import read_env

    if runtime.arbora_auth_mode != "none":
        return {}
    exposed = bool(read_env("ARBORA_AUTH_NONE_EXPOSE"))
    return {
        "mode": "anonymous_local",
        "principal": PRINCIPAL,
        "banner": BANNER,
        "doctor_check": "fail" if exposed else "warn",
    }


def loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_startup(args: Any, runtime: RuntimeConfig) -> None:
    """Reject unsafe or ambiguous anonymous configuration before minting state."""
    global _ACTIVE_MODE
    mode = runtime.arbora_auth_mode
    if _ACTIVE_MODE is not None and _ACTIVE_MODE != mode:
        raise ValueError("Authentication mode changes require a new serving process")
    if mode != "none":
        _ACTIVE_MODE = mode
        return
    from arbora.security.settings import security_settings

    config = security_settings(runtime)
    _validate_transport(args)
    _validate_credentials(config)
    _validate_engine_endpoints(runtime.graph_service_endpoints or ())

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
    from arbora.core.environment import read_env

    fields = (
        "auth_jwt_jwks_uri",
        "auth_jwt_issuer",
        "auth_jwt_audience",
        "kg_auth_token_ref",
        "kg_identity_oauth2",
    )
    if any(getattr(config, field, None) for field in fields):
        raise ValueError("Anonymous mode conflicts with credential configuration")
    if read_env("GRAPH_SERVICE_AUTH_SECRET") or read_env("MEDULLA_SIGNER_KEYS_JSON"):
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
    from arbora.security.scope_registry import scope_classes

    classes = scope_classes()
    if (
        not admitted_actor(value.actor)
        or value.tenant != TENANT
        or value.policy_version != POLICY
        or value.audience != AUDIENCE
        or any(classes.get(scope) in {None, "approver"} for scope in value.scopes)
    ):
        raise PermissionError("Anonymous session authority is invalid")


def session() -> Any:
    if not enabled():
        raise PermissionError("Anonymous mode is not configured")
    from arbora.security.actor_context import ActorContext, ActorType
    from arbora.security.scope_registry import scope_classes
    from arbora.security.session import GraphSession

    scopes = frozenset(
        name for name, kind in scope_classes().items() if kind != "approver"
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


def network_run_kwargs(
    kwargs: dict[str, Any], keys: JwksCache, ingress: SignedIngressSlot
) -> dict[str, Any]:
    """Compose Arbora admission around the SDK-owned network boundary."""
    if not enabled():
        return kwargs
    from starlette.middleware import Middleware

    from arbora.security.request_identity import ActorIdentityMiddleware

    return {
        **kwargs,
        "middleware": [
            Middleware(ActorIdentityMiddleware, keys=keys, ingress=ingress),
            *kwargs.get("middleware", ()),
        ],
    }


#: The operator list of admitted anonymous effects (ARBORA-IDENTITY-R004.6).
EFFECT_POLICY_KEY = "anonymous_effect_policy"
NOT_ADMITTED = "ANONYMOUS_EFFECT_NOT_ADMITTED"
DIRECT_EFFECT_DENIED = "ANONYMOUS_DIRECT_EFFECT_DENIED"
_DIGEST = __import__("re").compile(r"[0-9a-f]{64}")


def _policy_entry(entry: Any) -> tuple[str, str, str] | None:
    """One well-formed ``(op_id, target, action_digest)`` entry, else ``None``."""
    if not isinstance(entry, dict):
        return None
    op_id, target = entry.get("op_id"), entry.get("target")
    digest = entry.get("action_digest")
    if not (
        isinstance(op_id, str) and isinstance(target, str) and isinstance(digest, str)
    ):
        return None
    return (op_id, target, digest) if _DIGEST.fullmatch(digest) else None


def effect_policy(document: Any = None) -> tuple[tuple[str, str, str], ...]:
    """The ``identity.anonymous_effect_policy`` entries; the default (empty) denies all.

    An entry holds ``op_id``, ``target`` and ``action_digest`` (SHA-256, 64 lowercase
    hex). A malformed entry is dropped, so it can never admit anything.
    """
    if document is None:
        from arbora.core.settings import platform_config_file, read_config_document

        path = platform_config_file("config.json")
        document = read_config_document(path) if path.is_file() else {}
    entries = ((document or {}).get("identity") or {}).get(EFFECT_POLICY_KEY) or []
    parsed = (_policy_entry(entry) for entry in entries)
    return tuple(entry for entry in parsed if entry is not None)


def admit_effect(
    op_id: str,
    target: str,
    action_digest: str,
    *,
    policy: tuple[tuple[str, str, str], ...] | None = None,
) -> str | None:
    """``None`` when an operator entry matches the operation, target and digest.

    Otherwise ``ANONYMOUS_EFFECT_NOT_ADMITTED``. An anonymous actor never gains grants
    through this check: the shared dispatcher still applies containment and confirmation.
    """
    entries = effect_policy() if policy is None else policy
    return None if (op_id, target, action_digest) in entries else NOT_ADMITTED
