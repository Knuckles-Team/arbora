#!/usr/bin/python
from __future__ import annotations

"""Remote browser-OAuth MCP broker — provider-agnostic per-principal token custody.

CONCEPT:MERISTEM-ECO.mcp.remote-oauth-broker

GOC-85 (U-11 / U-41 / U-43 / U-44 / U-45). Builds the credential class this repo has
never had: a per-user, per-provider, per-remote-resource OAuth token, minted through a
real browser-mediated authorization flow and owned by a server-side broker rather than
the single-process local browser helper (U-43, not used here) or a shared/service credential (U-44/U-45).

Confirmed by source inspection before this module was first written (matches the
fail-closed regression pins in ``tests/unit/mcp/test_remote_oauth_fail_closed.py``,
landed in ``6c1606cbb``): no ``TokenStorage``/``OAuthClientProvider`` consumer, no
server callback route, and no per-principal token store existed anywhere in this repo.
Those pins are left completely untouched by NE-008 (this track) — it does not touch
``client_credentials.py``'s ``MCP_CLIENT_AUTH`` selector, and it does not reference
``delegated_auth``/``get_user_token``/``get_user_claims``/``get_delegated_token`` (the
exact names the canary greps for) anywhere in ``multiplexer.py``. NE-008 instead wires
this broker's own, already-reserved seam
(:meth:`RemoteOAuthBroker.bearer_headers_for`, see point 9 below) using the ONE
verified-identity primitive this repo already had before this track started
(:func:`arbora.security.actor_context.current_actor`) — a deliberate design
decision consistent with W07/GOC-15, not an incidental import.

Ten-part architecture (lane doc ``plans/arbora-completion-program/lanes/
GOC-85-remote-browser-oauth-mcp-broker.md``), what is implemented here and what is
deliberately deferred:

1. Provider registration — :class:`ProviderDescriptor` / :class:`ProviderRegistry`.
   Administrator-populated only; never caller-supplied at request time.               DONE
2. RFC 9728 + RFC 8414 discovery — :func:`discover_protected_resource` /
   :func:`discover_authorization_server`. HTTPS-only, bounded, issuer-consistent.     DONE
3. Client registration — pre-registered public-PKCE metadata on the descriptor, OR
   (NE-008) RFC 7591 dynamic registration via :class:`Rfc7591DynamicClientRegistrar`
   behind the :class:`DynamicClientRegistrar` protocol, idempotent per provider via
   :class:`_DynamicClientRegistrationCache`.                                          DONE
4. Authorization transaction — :class:`OAuthTransaction` / :class:`TransactionStore`,
   encrypted, session-bound, short TTL.                                               DONE
5. Callback validation — exact redirect, single-use state, same-principal/session
   binding, scope-widening rejection — :meth:`RemoteOAuthBroker.callback`.            DONE
6. Token store — :class:`OAuthTokenStore`, versioned-key encrypted, keyed by
   tenant/principal/provider/resource/audience.                                       DONE
7. Refresh/revocation — per-token lock, atomic rotation, fail-closed revoke. (NE-008
   fixed a latent defect here — see "Bug found and fixed" below.)                     DONE
8. Gateway callback + authorize routes (NE-008) —
   :mod:`arbora.gateway.remote_oauth_api`, mountable by one call.            DONE
9. Per-principal remote MCP path (U-44) + per-call authorization (U-44/U-45 wiring,
   NE-008) — :meth:`arbora.fleet.oauth_brokers.RemoteOAuthBrokers.bearer`,
   invoked from ``_open_one_session`` ONLY for a catalog entry explicitly opted in via
   an admin-configured ``oauth_provider`` block, and ONLY ever over an ephemeral,
   per-request session — such a server is never pool-mounted (``_start_child`` skips
   it) and never cached in the shared probe cache, so no fleet-global, principal-agnostic
   session or catalog snapshot is ever created for it. Fleet-global discovery
   (``find_tools``/``load_tools``/``call_proxied_tool``'s prefixed-name aggregation)
   remains principal-agnostic by construction and is NOT extended to auto-surface these
   tools there — that generalization is GOC-15/catalog-layer scope, not this track's.  DONE (seam), CATALOG AGGREGATION DEFERRED
10. Sanitized audit — :func:`_audit`, reusing this repo's "never log the secret" norm
    (the U-54 hygiene filter in :mod:`arbora.mcp_server.backing.oauth_log_hygiene` covers the
    *vendored SDK's* loggers; this module's own audit calls never pass code/state/
    verifier/token values to a log call in the first place, so there is nothing for a
    filter to redact).                                                                DONE

Why the multiplexer wiring (point 9) is safe, unlike an incidental one
------------------------------------------------------------------------
The U-44/U-45 canary (``test_remote_oauth_fail_closed.py``) fences literal names
(``delegated_auth``, ``get_user_token``, ``get_user_claims``, ``get_delegated_token``)
because *those* names would mean a per-user token reached the ONE shared,
principal-agnostic ``ChildRuntime``/session-per-server pool every other caller also
uses — exactly the unsafe shortcut GOC-85's original author correctly refused to build
without GOC-15's carrier contract. NE-008 does not do that: a catalog entry carrying
``oauth_provider`` is structurally excluded from that shared pool (never appears in
``self.children``, ``tool_to_server``, or the aggregated fleet catalog) and is instead
served through a dedicated, ephemeral, per-request session opened and torn down for
that one caller — the SAME pattern ``probe_server`` already used for un-pooled catalog
probing before this track. Two different credentials, two different lifetimes, two
different code paths: Arbora's own service-to-child authorization
(``MCP_CLIENT_AUTH``/``child_auth``) and this broker's per-user delegated grant are
never merged, and an oauth-gated child never carries both.

Bug found and fixed (ledger note, not a rewrite)
---------------------------------------------------
While wiring point 3 (DCR) required threading an explicit ``client_id`` through the
refresh grant, source inspection found ``OAuthTokenStore.refresh()`` was POSTing the
refresh grant to ``provider.resource_url`` (the protected MCP resource) instead of the
authorization server's discovered ``token_endpoint`` — refresh would never have worked
against a real provider (item 7 was marked DONE but was not, in fact, correct against a
real token endpoint; it was only ever exercised against a mock transport that ignores
the request URL). Fixed by re-running discovery inside ``refresh()`` (mirroring
``begin()``/``callback()``) and posting to ``as_meta.token_endpoint``.

Real-provider validation stops at the authorization URL. ``begin()`` is the only method
exercised in this lane against anything that could be a real provider, and even that is
only ever exercised in tests against a mock authorization server — no real provider is
registered, enabled, or contacted by this change. ``callback()``/``refresh()``/
``revoke()`` are fully implemented and tested exclusively against mock HTTP transports;
enabling any of them against a live third-party IdP is explicitly out of scope here
("stop at a user-mediated consent URL for real-provider validation").
"""

import hashlib
import json
import logging
import secrets
import time
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from arbora.fleet.discovery_authority import OAuthGrantBinding
from arbora.fleet.remote_oauth_tokens import (
    AuthorizationServerMetadata,
    OAuthBindingError,
    OAuthDiscoveryError,
    OAuthProviderError,
    OAuthTokenAbsentError,
    OAuthTokenStore,
    OAuthTransaction,
    ProviderDescriptor,
    StoredToken,
    TransactionStore,
    _bounded_token_post,
    _generate_pkce,
    _lock_for,
    _new_grant_revision,
    _normalize_granted_scopes,
    discover_authorization_server,
    discover_protected_resource,
)
from arbora.security.actor_context import ActorContext

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("arbora.fleet.remote_oauth_broker.audit")

__all__ = [
    "DynamicClientRegistrar",
    "OAuthRedirectNotAllowlistedError",
    "OAuthScopeError",
    "ProviderRegistry",
    "RemoteOAuthBroker",
    "Rfc7591DynamicClientRegistrar",
]

_DISCOVERY_TIMEOUT_S = 10.0
_TOKEN_EXCHANGE_TIMEOUT_S = 20.0
_TRANSACTION_TTL_S = 300.0


class OAuthRedirectNotAllowlistedError(RuntimeError):
    """The requested redirect URI is not the provider's exact registered value."""


class OAuthScopeError(RuntimeError):
    """The authorization server granted a scope outside what was requested."""


class ProviderRegistry:
    """Administrator-populated provider set. No request-time registration exists."""

    def __init__(self) -> None:
        self._providers: dict[str, ProviderDescriptor] = {}

    def register(self, descriptor: ProviderDescriptor) -> None:
        """Administrator-only call — never reachable from an MCP tool argument."""
        self._providers[descriptor.provider_id] = descriptor

    def get(self, provider_id: str) -> ProviderDescriptor | None:
        return self._providers.get(provider_id)

    def require_enabled(self, provider_id: str) -> ProviderDescriptor:
        provider = self._providers.get(str(provider_id or ""))
        if provider is None or not provider.enabled:
            raise OAuthProviderError("unknown or disabled provider")
        return provider

    def enabled_providers(self) -> tuple[ProviderDescriptor, ...]:
        """Return the administrator-populated enabled set for broker reads."""

        return tuple(
            sorted(
                (provider for provider in self._providers.values() if provider.enabled),
                key=lambda provider: provider.provider_id,
            )
        )


class DynamicClientRegistrar(Protocol):
    """Pluggable RFC 7591 dynamic-client-registration hook.

    The unopinionated default remains pre-registered ``client_id``/``redirect_uri``
    on :class:`ProviderDescriptor` (``dynamic_client_registration=False``, the
    model default). :class:`Rfc7591DynamicClientRegistrar` is the concrete
    implementation a provider opts into with ``dynamic_client_registration=True``;
    a deployment that needs different registration semantics (e.g. an
    out-of-band-approved client) supplies its own implementation of this
    protocol instead. The broker never invents or silently switches a client
    id/issuer on its own — see :class:`RemoteOAuthBroker`'s ``dcr_registrar``
    constructor argument.
    """

    def register(
        self, provider: ProviderDescriptor, as_metadata: AuthorizationServerMetadata
    ) -> str:
        """Return the registered ``client_id`` for ``provider`` at this issuer."""
        ...


_MAX_DCR_RESPONSE_BYTES = 64 * 1024
_MAX_CLIENT_NAME_LEN = 128


def _bounded_registration_post(
    client: httpx.Client, url: str, body: dict[str, Any]
) -> dict[str, Any]:
    """RFC 7591 §3.1 registration request: bounded JSON POST, HTTPS-only."""
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OAuthDiscoveryError("registration endpoint must be an exact HTTPS URL")
    try:
        with client.stream(
            "POST",
            url,
            json=body,
            headers={"Accept": "application/json"},
        ) as resp:
            resp.raise_for_status()
            response_body = bytearray()
            for chunk in resp.iter_bytes(65536):
                response_body.extend(chunk)
                if len(response_body) > _MAX_DCR_RESPONSE_BYTES:
                    raise OAuthDiscoveryError(
                        "registration response exceeded its bound"
                    )
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError("dynamic client registration request failed") from exc
    try:
        payload = json.loads(bytes(response_body))
    except (ValueError, UnicodeDecodeError):
        raise OAuthDiscoveryError(
            "dynamic client registration response was not valid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise OAuthDiscoveryError(
            "dynamic client registration response had an unexpected shape"
        )
    return payload


class Rfc7591DynamicClientRegistrar:
    """RFC 7591 public-client dynamic registration — the concrete DCR implementation.

    Registers exactly one public PKCE client per provider
    (``token_endpoint_auth_method="none"``, no client authentication secret
    requested or accepted — this broker only ever runs the public-client PKCE
    flow, U-43's model). Requires the authorization server to both advertise a
    ``registration_endpoint`` (RFC 8414 §2's optional DCR extension) and support
    PKCE ``S256`` (already enforced fail-closed by
    :func:`discover_authorization_server` before an :class:`AuthorizationServerMetadata`
    can even exist — re-checked here so this class is safe even if constructed
    and called directly, outside :class:`RemoteOAuthBroker`).

    Idempotency is NOT this class's job — it registers unconditionally every
    time :meth:`register` is called. :class:`RemoteOAuthBroker` (via
    :class:`_DynamicClientRegistrationCache`) is what makes registration
    idempotent-per-provider: it persists the winning ``client_id`` and never
    calls :meth:`register` again once a value is cached.
    """

    def __init__(self, http_client_factory: Any | None = None) -> None:
        self._http_client_factory = http_client_factory or _default_broker_http_client

    def register(
        self, provider: ProviderDescriptor, as_metadata: AuthorizationServerMetadata
    ) -> str:
        if "S256" not in as_metadata.code_challenge_methods_supported:
            raise OAuthDiscoveryError("authorization server does not support PKCE S256")
        if not as_metadata.registration_endpoint:
            raise OAuthProviderError(
                "authorization server does not advertise a dynamic client "
                "registration endpoint"
            )
        request_body = {
            "client_name": (f"meristem-remote-oauth-broker:{provider.provider_id}")[
                :_MAX_CLIENT_NAME_LEN
            ],
            "redirect_uris": [provider.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",  # nosec B105 - RFC 7591 public-client method name
            "application_type": "web",
        }
        client = self._http_client_factory()
        try:
            payload = _bounded_registration_post(
                client, as_metadata.registration_endpoint, request_body
            )
        finally:
            client.close()
        client_id = payload.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise OAuthProviderError(
                "dynamic client registration response is missing client_id"
            )
        # A returned ``client_secret`` (some servers issue one even for a
        # public-client request) is deliberately never read or stored: this
        # broker only ever authenticates as a public PKCE client, and storing
        # an unused secret would be one more thing that could leak.
        return client_id


class _DynamicClientRegistrationCache:
    """Idempotent-per-provider persisted ``client_id`` cache for RFC 7591 DCR.

    "Register once, persist the registration, never re-register on every flow"
    (the DCR deliverable's own idempotency requirement): a cached ``client_id``
    is reused for every subsequent :meth:`RemoteOAuthBroker.begin`. Concurrent
    first-callers in ONE process serialize on a per-provider lock
    (:func:`_lock_for`, the same primitive token refresh uses); across
    processes, :meth:`~arbora.security.secrets.SecretsClient.set_if_absent`
    is the durable engine backend's atomic create-if-absent, so exactly one
    racer's registration wins and every other racer reads back the winner's
    ``client_id`` rather than persisting its own (wasted, but harmless) extra
    registration.
    """

    def __init__(self, secrets_client: Any) -> None:
        self._secrets = secrets_client

    @staticmethod
    def _key(provider_id: str) -> str:
        return f"oauth-dcr-client:{provider_id}"

    def get(self, provider_id: str) -> str | None:
        return self._secrets.get(self._key(provider_id))

    def get_or_register(
        self,
        provider: ProviderDescriptor,
        as_metadata: AuthorizationServerMetadata,
        registrar: DynamicClientRegistrar,
    ) -> str:
        key = self._key(provider.provider_id)
        cached = self._secrets.get(key)
        if cached:
            return cached
        with _lock_for(key):
            cached = self._secrets.get(key)
            if cached:
                return cached
            client_id = registrar.register(provider, as_metadata)
            if not isinstance(client_id, str) or not client_id:
                raise OAuthProviderError(
                    "dynamic client registration returned an invalid client_id"
                )
            if self._secrets.set_if_absent(key, client_id):
                return client_id
            # A concurrent registration in another process won the race; use
            # ITS client_id (this process's own registration, if the AS
            # actually created a distinct client for it, is simply unused).
            winner = self._secrets.get(key)
            return winner or client_id


def _exchange_authorization_code(
    client: httpx.Client,
    as_metadata: AuthorizationServerMetadata,
    *,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    return _bounded_token_post(client, as_metadata.token_endpoint, data)


# ---------------------------------------------------------------------------
# Sanitized audit — never a code/token/state/verifier value.
# ---------------------------------------------------------------------------
def _pseudonymize(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _audit(
    event: str,
    *,
    provider: str,
    tenant: str,
    principal: str,
    outcome: str,
    latency_ms: float | None = None,
) -> None:
    _audit_logger.info(
        "oauth_broker event=%s provider=%s tenant=%s principal=%s outcome=%s latency_ms=%s",
        event,
        provider,
        _pseudonymize(tenant),
        _pseudonymize(principal),
        outcome,
        latency_ms,
    )


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------
def _default_broker_http_client() -> httpx.Client:
    from arbora.core.transport import create_http_client

    return create_http_client(
        timeout=_DISCOVERY_TIMEOUT_S,
        verify=True,
        follow_redirects=False,
        trust_env=False,
    )


class RemoteOAuthBroker:
    """Provider-agnostic remote browser-OAuth broker.

    Construction takes a :class:`ProviderRegistry` (administrator-populated) and an
    optional :class:`~arbora.security.secrets.SecretsClient` (defaults
    to :func:`arbora.security.secrets.create_secrets_client` — the
    same OpenBao/engine-backed encrypted store every other secret in this repo uses).
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        secrets_client: Any | None = None,
        http_client_factory: Any | None = None,
        dcr_registrar: DynamicClientRegistrar | None = None,
    ) -> None:
        if secrets_client is None:
            from arbora.security.secrets import create_secrets_client

            secrets_client = create_secrets_client()
        self.registry = registry
        self.transactions = TransactionStore(secrets_client)
        self.tokens = OAuthTokenStore(secrets_client)
        self._http_client_factory = http_client_factory or _default_broker_http_client
        # Deliverable 1 (RFC 7591): the concrete registrar a
        # ``dynamic_client_registration=True`` provider uses, plus the
        # idempotent-per-provider cache of what it returned. Never constructed
        # eagerly against a real network — only invoked from begin() when a
        # provider actually opts in and no cached client_id exists yet.
        self._dcr_registrar: DynamicClientRegistrar = (
            dcr_registrar or Rfc7591DynamicClientRegistrar(self._http_client_factory)
        )
        self._dcr_cache = _DynamicClientRegistrationCache(secrets_client)

    def _resolve_client_id(
        self, provider: ProviderDescriptor, as_meta: AuthorizationServerMetadata
    ) -> str:
        """Effective client_id for one flow: pre-registered, or DCR (Deliverable 1).

        A DCR-enabled provider registers AT MOST ONCE, ever — subsequent calls
        (this method, called again on the next begin()) read the persisted
        cache instead of re-registering, which is what "idempotent per
        provider" means here.
        """
        if provider.client_id:
            return provider.client_id
        return self._dcr_cache.get_or_register(provider, as_meta, self._dcr_registrar)

    def begin(
        self,
        *,
        provider_id: str,
        actor: ActorContext,
        browser_session_id: str,
        requested_scopes: tuple[str, ...] | None = None,
    ) -> str:
        """Stage 1-4: discover, mint a transaction, return the authorization URL.

        This is the only method meant to be exercised against a REAL provider in
        this lane (the explicit "stop at the consent URL" gate); it is still only
        ever tested here against a mock authorization server.

        ``requested_scopes``, when given, must be a subset of the provider's
        administrator-configured ``scopes`` (a policy ceiling this broker never
        exceeds) — the gateway's ``POST /providers/{id}/authorize`` route uses
        this to apply a caller/policy-specific scope filter without the broker
        ever trusting a caller-supplied scope it wasn't already willing to grant.
        """
        if not isinstance(browser_session_id, str) or not browser_session_id:
            raise OAuthBindingError("a browser session id is required")
        tenant, principal = OAuthTokenStore._require_verified(actor)
        provider = self.registry.require_enabled(provider_id)
        if requested_scopes is not None:
            if not set(requested_scopes) <= set(provider.scopes):
                raise OAuthScopeError(
                    "requested scope exceeds the provider's configured scopes"
                )
            effective_scopes: tuple[str, ...] = tuple(requested_scopes)
        else:
            effective_scopes = provider.scopes
        client = self._http_client_factory()
        try:
            resource_meta = discover_protected_resource(client, provider.resource_url)
            as_meta = discover_authorization_server(
                client,
                resource_meta,
                authorization_server_url=provider.authorization_server_url,
            )
            client_id = self._resolve_client_id(provider, as_meta)
        finally:
            client.close()

        verifier, challenge = _generate_pkce()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        now = time.time()
        scope = " ".join(effective_scopes)
        tx = OAuthTransaction(
            state=state,
            nonce=nonce,
            verifier=verifier,
            provider_id=provider.provider_id,
            tenant_id=tenant,
            principal_id=principal,
            browser_session_id=browser_session_id,
            redirect_uri=provider.redirect_uri,
            scope=scope,
            created_at=now,
            expires_at=now + _TRANSACTION_TTL_S,
            client_id=client_id,
        )
        self.transactions.put(tx)
        _audit(
            "begin",
            provider=provider.provider_id,
            tenant=tenant,
            principal=principal,
            outcome="authorization_url_issued",
        )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": provider.redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{as_meta.authorization_endpoint}?{urlencode(params)}"

    def callback(
        self,
        *,
        code: str,
        state: str,
        actor: ActorContext,
        browser_session_id: str,
    ) -> StoredToken:
        """Stage 5-6: validate the callback, exchange the code, store the token.

        Mock-server tested only in this lane (see module docstring). Rejects
        missing/replayed state, principal/tenant/session mismatch, and scope
        widening, all before any network call is made against the token endpoint
        except the discovery + exchange calls themselves.
        """
        tx = self.transactions.consume_once(state)
        tenant, principal = OAuthTokenStore._require_verified(actor)
        if principal != tx.principal_id or tenant != tx.tenant_id:
            _audit(
                "callback",
                provider=tx.provider_id,
                tenant=tenant,
                principal=principal,
                outcome="rejected_principal_mismatch",
            )
            raise OAuthBindingError(
                "callback principal/tenant does not match the initiating transaction"
            )
        if browser_session_id != tx.browser_session_id:
            _audit(
                "callback",
                provider=tx.provider_id,
                tenant=tenant,
                principal=principal,
                outcome="rejected_session_mismatch",
            )
            raise OAuthBindingError(
                "callback session does not match the initiating browser session"
            )
        provider = self.registry.require_enabled(tx.provider_id)
        client = self._http_client_factory()
        try:
            resource_meta = discover_protected_resource(client, provider.resource_url)
            as_meta = discover_authorization_server(
                client,
                resource_meta,
                authorization_server_url=provider.authorization_server_url,
            )
            payload = _exchange_authorization_code(
                client,
                as_meta,
                client_id=tx.client_id or provider.client_id or "",
                code=code,
                verifier=tx.verifier,
                redirect_uri=tx.redirect_uri,
            )
        finally:
            client.close()

        granted_scope = str(payload.get("scope", tx.scope))
        requested = set(tx.scope.split())
        granted = set(granted_scope.split())
        if not granted <= requested:
            _audit(
                "callback",
                provider=provider.provider_id,
                tenant=tenant,
                principal=principal,
                outcome="rejected_scope_widened",
            )
            raise OAuthScopeError("granted scope exceeds what was requested")

        access_token = payload["access_token"]
        if not isinstance(access_token, str) or not access_token:
            raise OAuthDiscoveryError(
                "token response contained an invalid access token"
            )
        token = StoredToken(
            access_token=access_token,
            refresh_token=payload.get("refresh_token"),
            token_type=str(payload.get("token_type", "Bearer")),
            expires_at=time.time() + float(payload.get("expires_in", 3600)),
            granted_scope=granted_scope,
            key_version=OAuthTokenStore.CURRENT_KEY_VERSION,
            audience=provider.resource_url,
            grant_revision=_new_grant_revision(),
        )
        self.tokens.put(
            actor=actor,
            provider_id=provider.provider_id,
            resource_url=provider.resource_url,
            audience=provider.resource_url,
            token=token,
            authorization_started_at=tx.created_at,
        )
        _audit(
            "callback",
            provider=provider.provider_id,
            tenant=tenant,
            principal=principal,
            outcome="token_stored",
        )
        return token

    def revoke(self, *, actor: ActorContext, provider_id: str) -> None:
        """Revoke the verified caller's grant for an enabled provider.

        Provider identity and resource/audience binding come from the
        administrator-owned registry, never from the request.  The token store
        writes its fail-closed tombstone even when no live token remains.
        """
        provider = self.registry.require_enabled(provider_id)
        tenant, principal = OAuthTokenStore._require_verified(actor)
        self.tokens.revoke(
            actor=actor,
            provider_id=provider.provider_id,
            resource_url=provider.resource_url,
            audience=provider.resource_url,
        )
        _audit(
            "revoke",
            provider=provider.provider_id,
            tenant=tenant,
            principal=principal,
            outcome="token_revoked",
        )

    def _resolved_grant(
        self, *, actor: ActorContext, provider_id: str, resource_url: str
    ) -> tuple[ProviderDescriptor, StoredToken, OAuthGrantBinding]:
        """Resolve one live token and mint its non-secret broker binding."""

        provider = self.registry.require_enabled(provider_id)
        if resource_url != provider.resource_url:
            raise OAuthProviderError(
                "token forwarding is bound to the provider's exact registered resource"
            )
        tenant, principal = OAuthTokenStore._require_verified(actor)
        token = self.tokens.get(
            actor=actor,
            provider_id=provider.provider_id,
            resource_url=provider.resource_url,
            audience=provider.resource_url,
        )
        if token is None:
            raise OAuthTokenAbsentError("no stored token for this principal/provider")
        if token.expires_at <= time.time():
            raise OAuthTokenAbsentError(
                "stored token is expired; re-authorization is required"
            )
        if token.audience != provider.resource_url:
            raise OAuthProviderError("stored token audience is not provider-bound")
        granted_scopes = _normalize_granted_scopes(token.granted_scope)
        if not set(granted_scopes) <= set(provider.scopes):
            raise OAuthScopeError("stored grant exceeds the provider scope ceiling")
        if token.key_version <= 0 or not token.grant_revision.strip():
            raise OAuthTokenAbsentError("stored grant identity is unavailable")
        binding = OAuthGrantBinding(
            tenant_id=tenant,
            principal_id=principal,
            provider_id=provider.provider_id,
            resource_url=provider.resource_url,
            audience=token.audience,
            granted_scopes=granted_scopes,
            key_version=token.key_version,
            grant_revision=token.grant_revision,
        )
        return provider, token, binding

    def grant_binding_for(
        self, *, actor: ActorContext, provider_id: str, resource_url: str
    ) -> OAuthGrantBinding:
        """Return the current exact grant identity without exposing credentials."""

        _provider, _token, binding = self._resolved_grant(
            actor=actor, provider_id=provider_id, resource_url=resource_url
        )
        return binding

    def bearer_headers_and_grant_binding(
        self, *, actor: ActorContext, provider_id: str, resource_url: str
    ) -> tuple[dict[str, str], OAuthGrantBinding]:
        """Resolve the bearer and its binding atomically from one stored grant."""

        _provider, token, binding = self._resolved_grant(
            actor=actor, provider_id=provider_id, resource_url=resource_url
        )
        return {"Authorization": f"{token.token_type} {token.access_token}"}, binding

    def bearer_headers_for(
        self, *, actor: ActorContext, provider_id: str, resource_url: str
    ) -> dict[str, str]:
        """Endpoint-bound bearer lookup: forwards ONLY to the exact registered resource.

        This is the seam a per-principal multiplexer child (Deliverable 3, GOC-85
        W06/W08) calls per outbound request, via
        :meth:`arbora.fleet.oauth_brokers.RemoteOAuthBrokers.bearer`.
        ``resource_url`` must equal the provider's registered exact resource URL,
        or this raises rather than forwarding a token to a different endpoint
        (rejects cross-provider forwarding, and — because the caller passes the
        URL it is ACTUALLY about to connect to, never one echoed back from a
        redirect — rejects forwarding to a rebound/redirected endpoint too).

        Fails closed on an EXPIRED token exactly like a missing one: this method
        never transparently refreshes (the per-call forwarding hot path stays
        synchronous and side-effect-free; refresh is a separate, deliberately
        out-of-band operation via :meth:`OAuthTokenStore.refresh`). An expired
        grant reads as "no valid grant" — the caller must re-run the browser
        flow, not silently receive a stale credential.
        """
        headers, _binding = self.bearer_headers_and_grant_binding(
            actor=actor, provider_id=provider_id, resource_url=resource_url
        )
        return headers
