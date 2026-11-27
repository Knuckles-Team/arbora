"""Opt-in EG browser composition over the existing served invocation owners."""

from typing import Any
from urllib.parse import urlsplit

from arbora.identity.engine import IdentityUnavailable


class _IdentityClient:
    """Borrow the composition-owned pool client; never create another pool."""

    def __init__(self, client_factory: Any, claims: Any):
        self._client_factory = client_factory
        self._claims = claims

    def _check(self, claims: Any) -> None:
        _require_broker_claims(claims)

    async def _send(self, *args: Any, **kwargs: Any) -> Any:
        client = await self._client_factory()
        # Read after connection awaits; neither the first connection snapshot
        # nor an ambient browser caller supplies broker identity authority.
        import inspect

        from arbora.security.anonymous import client_scope

        supplied = self._claims()
        claims = await supplied if inspect.isawaitable(supplied) else supplied
        self._check(claims)
        with client_scope(client, claims):
            return await client._send(*args, **kwargs)

    def acting_as(self, session: Any) -> "_IdentityClient":
        """A client whose engine calls carry this verified session's own claims."""
        return _ActingClient(
            self._client_factory, lambda: session.engine_verified_context()
        )


class _ActingClient(_IdentityClient):
    """Sends as one verified session; the claims are the person's, not the broker's."""

    def _check(self, claims: Any) -> None:
        return None


def configured_browser_boundary(
    invocation: Any,
    client_factory: Any,
    *,
    graph: str,
    tenant: str,
    broker_claims: Any,
    request_dispatch: Any,
) -> Any:
    """Require explicit existing deployment inputs, never provision defaults."""
    from arbora.core.settings import runtime_config, setting
    from arbora.identity.deployment_settings import identity_deployment
    from arbora.security.settings import security_settings

    if not runtime_config.enable_web_ui or runtime_config.webui_auth_mode != "eg":
        return None
    config = security_settings()
    origin = runtime_config.webui_origin
    parsed = urlsplit(origin or "")
    if (
        not origin
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not origin.isascii()
        or not config.auth_jwt_jwks_uri
        or not config.auth_jwt_issuer
        or not config.auth_jwt_audience
        or not setting("SECRETS_VAULT_URL", None)
        or setting("SECRETS_BACKEND", "vault") != "vault"
    ):
        raise IdentityUnavailable(
            "EG browser requires configured HTTPS origin, JWT verifier and signing store"
        )
    from .browser_broker import ConfiguredBrowserBroker

    broker = ConfiguredBrowserBroker(
        runtime_config.webui_broker_token_ref or "",
        tenant=tenant,
        audience=config.auth_jwt_audience,
        process_claims=broker_claims,
    )
    from arbora.security.secrets import create_secrets_client

    deployed = identity_deployment()
    return build_browser_boundary(
        invocation,
        _IdentityClient(client_factory, broker),
        graph=graph,
        tenant=deployed.tenant or tenant,
        origin=origin,
        issuer=deployed.issuer or config.auth_jwt_issuer,
        audience=config.auth_jwt_audience,
        store=create_secrets_client().backend,
        request_dispatch=request_dispatch,
    )


def build_browser_boundary(
    invocation: Any,
    client: Any,
    *,
    graph: str,
    tenant: str,
    origin: str,
    issuer: str,
    audience: str,
    store: Any,
    request_dispatch: Any,
) -> Any:
    """One runtime, issuer and exact caller fence around the shared pipeline."""
    from arbora.api.http.browser_app import (
        create_browser_api_application,
        create_webui_session_boundary,
    )
    from arbora.api.invoke.pipeline import invoke
    from arbora.identity.admission import BrowserAdmission
    from arbora.identity.broker import ArboraBrowserAuthority
    from arbora.identity.browser_runtime import (
        BrowserAuthApplication,
        BrowserCallerResolver,
    )
    from arbora.identity.composition import build_engine_identity_ports
    from arbora.identity.federated import federation_from_settings
    from arbora.identity.issuer import IssuerSettings, LocalIssuer
    from arbora.identity.issuer_discovery import IssuerDiscovery
    from arbora.security.request_identity import verify_local_bearer_token

    ports = build_engine_identity_ports(client, graph=graph, refresh=False)
    settings = IssuerSettings(issuer, audience, tenant)
    # ISSUER_KEY_UNAVAILABLE: an unreadable key ring refuses to start the issuer.
    discovery = IssuerDiscovery(store, settings)
    signer = LocalIssuer(store, settings, ports.credentials)
    owner = ArboraBrowserAuthority(
        ports.sessions, verify_local_bearer_token, trusted_origin=origin
    )
    caller = BrowserCallerResolver(
        ports.sessions, verify_local_bearer_token, graph=graph
    )
    admission = BrowserAdmission(
        signer,
        owner,
        caller,
        trusted_origin=origin,
    )
    operations = create_browser_api_application(
        admission=admission,
        services=invocation.services,
        visibility=invocation.services.policy_gate.check_op,
        invoke=invoke,
    )
    return create_webui_session_boundary(
        admission=admission,
        operations=operations,
        request_dispatch=request_dispatch,
        auth_app=BrowserAuthApplication(
            client,
            ports.sessions,
            graph=graph,
            origin=origin,
            issuer=signer,
            caller=caller,
            discovery=discovery,
            acting=getattr(client, "acting_as", None),
            federation=federation_from_settings(client, graph, origin),
        ),
    )


def _require_broker_claims(claims: Any) -> None:
    """Check existing exact broker scope; admin and graph roles never imply it."""
    from collections.abc import Mapping

    if not isinstance(claims, Mapping):
        raise IdentityUnavailable("qualified browser broker authority required")
    scopes = claims.get("scopes")
    if (
        not isinstance(scopes, (tuple, list, set, frozenset))
        or "identity:authenticate" not in scopes
        or claims.get("delegation") not in ([], ())
    ):
        raise PermissionError("direct identity:authenticate browser broker required")
