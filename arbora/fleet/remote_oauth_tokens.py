"""Remote OAuth tokens: provider and discovery contracts, PKCE transactions and the encrypted token store (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import secrets
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arbora.security.actor_context import ActorContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounds — explicit, numeric, never open-ended (lane contract: "Algorithmic
# and resource budget").
# ---------------------------------------------------------------------------
_MAX_DISCOVERY_RESPONSE_BYTES = 256 * 1024


_MAX_TOKEN_RESPONSE_BYTES = 1024 * 1024


_MAX_STATE_LEN = 256


_CONSUMED_TOMBSTONE = "__consumed__"


# ---------------------------------------------------------------------------
# Errors — one exception type per fail-closed boundary, never a bare bool.
# ---------------------------------------------------------------------------
class OAuthProviderError(RuntimeError):
    """Unknown, disabled, or otherwise rejected provider registration."""


class OAuthDiscoveryError(RuntimeError):
    """RFC 9728 / RFC 8414 discovery failed, was malformed, or was inconsistent."""


class OAuthStateError(RuntimeError):
    """The transaction ``state`` was missing, unknown, replayed, or expired."""


class OAuthBindingError(RuntimeError):
    """The callback did not bind to the transaction's principal/tenant/session."""


class OAuthTokenAbsentError(RuntimeError):
    """No stored token/refresh-token exists for this principal/provider/resource."""


class OAuthRevokedError(RuntimeError):
    """The stored record is revoked (or its revocation status is ambiguous)."""


class OAuthRefreshRaceError(RuntimeError):
    """The stored record changed underneath a refresh; retry."""


# ---------------------------------------------------------------------------
# 1. Provider registration — administrator-approved only.
# ---------------------------------------------------------------------------
class ProviderDescriptor(BaseModel):
    """One administrator-approved remote-MCP OAuth provider.

    Never constructed from caller-supplied request data — a
    :class:`ProviderRegistry` is populated only by deployment/administration code.
    Abstraction seam for "abstraction-first, no opinionation": nothing here is
    specific to one vendor's quirks; a provider is fully described by this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=128)
    resource_url: str = Field(
        min_length=1,
        max_length=2048,
        description="Exact remote MCP resource URL. Never a caller-supplied value.",
    )
    authorization_server_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "Explicit authorization-server issuer to use when the protected-resource "
            "metadata lists more than one. Unset picks the resource metadata's first "
            "entry."
        ),
    )
    client_id: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Pre-registered public PKCE client id for the deployment callback. "
            "Mutually exclusive with dynamic_client_registration=True — exactly "
            "one client-identity source is configured, never both."
        ),
    )
    dynamic_client_registration: bool = Field(
        default=False,
        description=(
            "RFC 7591 opt-in: when true, client_id must be unset and is instead "
            "resolved once via Rfc7591DynamicClientRegistrar (or an injected "
            "DynamicClientRegistrar) and cached idempotently per provider_id."
        ),
    )
    redirect_uri: str = Field(
        min_length=1,
        max_length=2048,
        description="Exact deployment callback route. The sole allowlisted redirect.",
    )
    scopes: tuple[str, ...] = Field(default_factory=tuple)
    enabled: bool = Field(
        default=False,
        description="Staged rollout gate — disabled providers reject begin()/callback().",
    )

    @field_validator("resource_url", "redirect_uri")
    @classmethod
    def _require_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("must be an exact HTTPS URL without embedded credentials")
        return value

    @field_validator("authorization_server_url")
    @classmethod
    def _optional_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._require_https(value)

    @field_validator("client_id")
    @classmethod
    def _client_id_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("client_id must not be blank")
        return value

    @model_validator(mode="after")
    def _require_exactly_one_client_identity_source(self) -> ProviderDescriptor:
        if self.dynamic_client_registration and self.client_id:
            raise ValueError(
                "dynamic_client_registration and a pre-registered client_id are "
                "mutually exclusive"
            )
        if not self.dynamic_client_registration and not self.client_id:
            raise ValueError(
                "client_id is required unless dynamic_client_registration is enabled"
            )
        return self


# ---------------------------------------------------------------------------
# 2. Discovery — RFC 9728 protected-resource, RFC 8414 authorization-server.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProtectedResourceMetadata:
    resource: str
    authorization_servers: tuple[str, ...]


@dataclass(frozen=True)
class AuthorizationServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    code_challenge_methods_supported: tuple[str, ...]
    grant_types_supported: tuple[str, ...]
    registration_endpoint: str | None = None
    """RFC 7591 §2 dynamic-client-registration endpoint, when advertised. HTTPS-
    validated the same as every other discovered endpoint; ``None`` when the
    authorization server does not support DCR (a provider that also has no
    pre-registered ``client_id`` then fails closed at registration time, not
    silently)."""


def _bounded_json_get(client: httpx.Client, url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OAuthDiscoveryError("discovery endpoint must be an exact HTTPS URL")
    try:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            resp.raise_for_status()
            body = bytearray()
            for chunk in resp.iter_bytes(65536):
                body.extend(chunk)
                if len(body) > _MAX_DISCOVERY_RESPONSE_BYTES:
                    raise OAuthDiscoveryError("discovery response exceeded its bound")
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError("discovery request failed") from exc
    try:
        payload = json.loads(bytes(body))
    except (ValueError, UnicodeDecodeError):
        raise OAuthDiscoveryError("discovery response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise OAuthDiscoveryError("discovery response had an unexpected shape")
    return payload


def _protected_resource_metadata_url(resource_url: str) -> str:
    """RFC 9728 §3.1 well-known URI construction."""
    parsed = urlsplit(resource_url)
    path = parsed.path.rstrip("/")
    well_known = f"/.well-known/oauth-protected-resource{path}"
    return f"{parsed.scheme}://{parsed.netloc}{well_known}"


def _authorization_server_metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1 well-known URI construction (insert before the issuer's path)."""
    parsed = urlsplit(issuer)
    path = parsed.path.rstrip("/")
    well_known = f"/.well-known/oauth-authorization-server{path}"
    return f"{parsed.scheme}://{parsed.netloc}{well_known}"


def discover_protected_resource(
    client: httpx.Client, resource_url: str
) -> ProtectedResourceMetadata:
    """RFC 9728: fetch the protected-resource metadata for ``resource_url``."""
    metadata_url = _protected_resource_metadata_url(resource_url)
    payload = _bounded_json_get(client, metadata_url)
    resource = payload.get("resource")
    servers = payload.get("authorization_servers")
    if (
        not isinstance(resource, str)
        or not resource
        or not isinstance(servers, list)
        or not servers
        or not all(isinstance(s, str) and s for s in servers)
    ):
        raise OAuthDiscoveryError(
            "protected-resource metadata is missing resource/authorization_servers"
        )
    return ProtectedResourceMetadata(
        resource=resource, authorization_servers=tuple(servers)
    )


def _require_https_endpoint(endpoint: str, message: str) -> None:
    """Fail closed with ``message`` unless ``endpoint`` is an HTTPS URL with a host."""
    parsed = urlsplit(endpoint)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OAuthDiscoveryError(message)


def _check_registration_endpoint(registration_endpoint: object) -> None:
    """An advertised registration endpoint must be an HTTPS URL string."""
    if registration_endpoint is None:
        return
    if not isinstance(registration_endpoint, str):
        raise OAuthDiscoveryError(
            "authorization-server registration_endpoint is invalid"
        )
    _require_https_endpoint(
        registration_endpoint,
        "authorization-server registration_endpoint must be HTTPS",
    )


def _check_pkce_and_grant(challenge_methods: object, grant_types: object) -> None:
    """The server must support PKCE S256 and the authorization_code grant."""
    if not isinstance(challenge_methods, list) or "S256" not in challenge_methods:
        raise OAuthDiscoveryError("authorization server does not support PKCE S256")
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise OAuthDiscoveryError(
            "authorization server does not support the authorization_code grant"
        )


def discover_authorization_server(
    client: httpx.Client,
    resource_metadata: ProtectedResourceMetadata,
    *,
    authorization_server_url: str | None = None,
) -> AuthorizationServerMetadata:
    """RFC 8414: fetch + validate the authorization-server metadata.

    Requires HTTPS, a matching issuer (issuer consistency — the returned ``issuer``
    field must equal the URL that was queried), the authorization_code grant, and
    PKCE S256 support. Any violation fails closed with :class:`OAuthDiscoveryError`.
    """
    issuer_url = authorization_server_url or resource_metadata.authorization_servers[0]
    if issuer_url not in resource_metadata.authorization_servers:
        raise OAuthDiscoveryError(
            "configured authorization server is not among the resource's declared servers"
        )
    metadata_url = _authorization_server_metadata_url(issuer_url)
    payload = _bounded_json_get(client, metadata_url)

    issuer = payload.get("issuer")
    authorization_endpoint = payload.get("authorization_endpoint")
    token_endpoint = payload.get("token_endpoint")
    challenge_methods = payload.get("code_challenge_methods_supported") or []
    grant_types = payload.get("grant_types_supported") or [
        "authorization_code",
        "implicit",
    ]
    registration_endpoint = payload.get("registration_endpoint")

    if not isinstance(issuer, str) or issuer.rstrip("/") != issuer_url.rstrip("/"):
        raise OAuthDiscoveryError("authorization-server issuer is inconsistent")
    if not isinstance(authorization_endpoint, str) or not isinstance(
        token_endpoint, str
    ):
        raise OAuthDiscoveryError("authorization-server metadata is incomplete")
    for endpoint in (authorization_endpoint, token_endpoint):
        _require_https_endpoint(
            endpoint, "authorization-server endpoints must be HTTPS"
        )
    _check_registration_endpoint(registration_endpoint)
    _check_pkce_and_grant(challenge_methods, grant_types)
    return AuthorizationServerMetadata(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        code_challenge_methods_supported=tuple(challenge_methods),
        grant_types_supported=tuple(grant_types),
        registration_endpoint=registration_endpoint,
    )


# ---------------------------------------------------------------------------
# 4. Authorization transaction — encrypted, session-bound, short TTL.
# ---------------------------------------------------------------------------
def _generate_pkce() -> tuple[str, str]:
    """PKCE S256 verifier/challenge. Deliberately not imported from
    ``security.browser_auth`` — see the module docstring's U-43 rationale."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )
    return verifier, challenge


@dataclass(frozen=True)
class OAuthTransaction:
    state: str
    nonce: str
    verifier: str
    provider_id: str
    tenant_id: str
    principal_id: str
    browser_session_id: str
    redirect_uri: str
    scope: str
    created_at: float
    expires_at: float
    client_id: str = ""
    """The client_id actually used to build the authorization URL — resolved once
    at begin() time (pre-registered or DCR-cached) and pinned into the
    transaction so callback()'s token exchange uses the SAME client_id even if
    a concurrent DCR registration elsewhere changed the cache in between."""

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "state": self.state,
                "nonce": self.nonce,
                "verifier": self.verifier,
                "provider_id": self.provider_id,
                "tenant_id": self.tenant_id,
                "principal_id": self.principal_id,
                "browser_session_id": self.browser_session_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "created_at": self.created_at,
                "expires_at": self.expires_at,
                "client_id": self.client_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> OAuthTransaction:
        data = json.loads(raw)
        return cls(**data)


def _state_is_well_formed(state: object) -> bool:
    """True for a non-empty, bounded state string with no control characters."""
    return (
        isinstance(state, str)
        and bool(state)
        and len(state) <= _MAX_STATE_LEN
        and not any(ord(character) < 32 or ord(character) == 127 for character in state)
    )


class TransactionStore:
    """Session-bound encrypted transaction storage, single-use by atomic claim.

    Backed by the same :class:`~arbora.security.secrets.SecretsClient`
    every other secret in this repo uses (engine-backed encrypted store, or
    OpenBao/Vault when ``SECRETS_BACKEND=vault`` — never a new store). Single-use is
    enforced with :meth:`SecretsClient.compare_and_set` (already atomic on the durable
    engine backend, the same primitive signing-key rotation uses) rather than a
    read-then-delete race: exactly one caller's compare-and-set from the exact bytes it
    read to a tombstone can win, so a concurrent replay of the same ``state`` fails
    closed even under real concurrency, not just under one process's lock.
    """

    def __init__(self, secrets_client: Any) -> None:
        self._secrets = secrets_client

    @staticmethod
    def _key(state: str) -> str:
        return f"oauth-tx:{state}"

    def put(self, tx: OAuthTransaction) -> None:
        self._secrets.set(self._key(tx.state), tx.to_json(), kind="oauth-transaction")

    def peek(self, state: str) -> OAuthTransaction | None:
        """Read one live transaction without consuming it; absent or malformed is ``None``."""
        raw = self._secrets.get(self._key(state))
        if raw is None or raw == _CONSUMED_TOMBSTONE:
            return None
        try:
            return OAuthTransaction.from_json(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def consume_once(self, state: str, *, now: float | None = None) -> OAuthTransaction:
        if not _state_is_well_formed(state):
            raise OAuthStateError("malformed state")
        key = self._key(state)
        raw = self._secrets.get(key)
        if raw is None or raw == _CONSUMED_TOMBSTONE:
            raise OAuthStateError("unknown, expired, or already-consumed state")
        claimed = self._secrets.compare_and_set(key, raw, _CONSUMED_TOMBSTONE)
        if not claimed:
            raise OAuthStateError("state was concurrently consumed (replay)")
        self._secrets.delete(key)
        try:
            tx = OAuthTransaction.from_json(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise OAuthStateError("stored transaction was malformed") from None
        if tx.state != state or tx.is_expired(now if now is not None else time.time()):
            raise OAuthStateError("authorization transaction expired or mismatched")
        return tx


# ---------------------------------------------------------------------------
# 6/7. Token store — encrypted, versioned-key, per-principal; refresh/revoke.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StoredToken:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: float
    granted_scope: str
    key_version: int
    audience: str
    # Process-owned identity for the exact grant.  It is deliberately separate
    # from bearer/refresh material and is rotated on callback/refresh.
    grant_revision: str = ""


def _normalize_granted_scopes(value: str) -> tuple[str, ...]:
    """Return the canonical, non-secret representation of granted scopes."""

    return tuple(sorted({part for part in str(value or "").split() if part}))


_TOKEN_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)


_TOKEN_LOCKS_GUARD = threading.Lock()


def _lock_for(storage_key: str) -> threading.Lock:
    """Per-token lock, never a global lock — concurrent refreshes for different
    tokens must not serialize against each other (lane resource budget)."""
    with _TOKEN_LOCKS_GUARD:
        lock = _TOKEN_LOCKS.get(storage_key)
        if lock is None:
            lock = threading.Lock()
            _TOKEN_LOCKS[storage_key] = lock
        return lock


def _new_grant_revision() -> str:
    """Mint a process-owned opaque identity for one grant revision."""

    return secrets.token_hex(24)


def _refuse_revoked_overwrite(
    raw: str | None, authorization_started_at: float | None
) -> None:
    """A revoked stored token is replaced only by a flow that began after the revocation."""
    if raw is None:
        return
    try:
        current = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        current = None
    if not (isinstance(current, dict) and current.get("revoked")):
        return
    try:
        revoked_at = float(current["revoked_at"])
        if authorization_started_at is None:
            raise TypeError("authorization start time is required")
        started_at = float(authorization_started_at)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise OAuthRevokedError(
            "revoked token requires a newer authorization flow"
        ) from None
    if (
        not math.isfinite(revoked_at)
        or not math.isfinite(started_at)
        or started_at <= revoked_at
    ):
        raise OAuthRevokedError("authorization flow predates the token revocation")


class OAuthTokenStore:
    """Encrypted token custody keyed by tenant/principal/provider/resource/audience.

    The storage key is DERIVED ONLY from a verified, authenticated
    :class:`ActorContext` — never from a caller-supplied tenant/principal string.
    This is the structural mechanism, not a policy statement, behind "one user must
    never be able to reach another's token": there is no code path in this class that
    accepts an arbitrary principal identifier, so a caller can only ever address ITS
    OWN storage key.
    """

    CURRENT_KEY_VERSION = 1

    def __init__(self, secrets_client: Any) -> None:
        self._secrets = secrets_client

    @staticmethod
    def _require_verified(actor: ActorContext) -> tuple[str, str]:
        tenant = str(actor.tenant_id or "").strip()
        principal = str(actor.actor_id or "").strip()
        if not actor.authenticated or not tenant or not principal:
            raise PermissionError(
                "OAuth token custody requires a verified, authenticated principal "
                "with a tenant claim"
            )
        return tenant, principal

    @staticmethod
    def _storage_key(
        tenant: str, principal: str, provider_id: str, resource_url: str, audience: str
    ) -> str:
        # Key NAMES stay queryable plaintext metadata in the secrets backend (see
        # SecretsClient's own docstring); hash the principal-identifying components so
        # the key namespace itself never carries a readable actor id / tenant name.
        digest = hashlib.sha256(
            "\n".join((tenant, principal, provider_id, resource_url, audience)).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"oauth-token:{digest}"

    def put(
        self,
        *,
        actor: ActorContext,
        provider_id: str,
        resource_url: str,
        audience: str,
        token: StoredToken,
        authorization_started_at: float | None = None,
    ) -> None:
        tenant, principal = self._require_verified(actor)
        if token.audience != audience:
            raise OAuthBindingError(
                "stored token audience must equal the protected resource audience"
            )
        grant_revision = (
            str(token.grant_revision or "").strip() or _new_grant_revision()
        )
        key = self._storage_key(tenant, principal, provider_id, resource_url, audience)
        record = {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_type": token.token_type,
            "expires_at": token.expires_at,
            "granted_scope": token.granted_scope,
            "key_version": token.key_version,
            "audience": token.audience,
            "grant_revision": grant_revision,
            "revoked": False,
        }
        lock = _lock_for(key)
        with lock:
            _refuse_revoked_overwrite(self._secrets.get(key), authorization_started_at)
            self._secrets.set(key, json.dumps(record, separators=(",", ":")))

    def get(
        self,
        *,
        actor: ActorContext,
        provider_id: str,
        resource_url: str,
        audience: str,
    ) -> StoredToken | None:
        """Return the caller's OWN token, or ``None``. Never another principal's."""
        tenant, principal = self._require_verified(actor)
        key = self._storage_key(tenant, principal, provider_id, resource_url, audience)
        raw = self._secrets.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("revoked"):
            # Fail closed: a revoked or malformed record reads as absent, never as a
            # usable token.
            return None
        try:
            stored_audience = str(data["audience"])
            grant_revision = str(data["grant_revision"]).strip()
            if stored_audience != audience or not grant_revision:
                return None
            return StoredToken(
                access_token=str(data["access_token"]),
                refresh_token=data.get("refresh_token"),
                token_type=str(data.get("token_type", "Bearer")),
                expires_at=float(data["expires_at"]),
                granted_scope=str(data.get("granted_scope", "")),
                key_version=int(data.get("key_version", self.CURRENT_KEY_VERSION)),
                audience=stored_audience,
                grant_revision=grant_revision,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def refresh(
        self,
        *,
        actor: ActorContext,
        provider: ProviderDescriptor,
        resource_url: str,
        audience: str,
        http_client: httpx.Client,
        client_id: str | None = None,
    ) -> StoredToken:
        """Rotate the refresh token atomically under a per-token lock.

        Fails closed: an absent record, a revoked record, or a missing refresh
        token all raise rather than degrading to "nothing to do" — the caller must
        treat any of these as "re-authenticate", never as "proceed unauthenticated".

        ``client_id`` is the client identity to present in the refresh grant.
        Defaults to ``provider.client_id`` (the pre-registered case); a
        DCR-enabled provider has no ``provider.client_id`` and the caller must
        pass the DCR-cached ``client_id`` explicitly (see
        :class:`RemoteOAuthBroker`, which resolves it via
        ``_DynamicClientRegistrationCache`` before calling this method).
        """
        tenant, principal = self._require_verified(actor)
        key = self._storage_key(
            tenant, principal, provider.provider_id, resource_url, audience
        )
        effective_client_id = client_id or provider.client_id
        if not effective_client_id:
            raise OAuthProviderError(
                "no client_id available for token refresh — DCR has not "
                "completed for this provider"
            )
        lock = _lock_for(key)
        with lock:
            raw = self._secrets.get(key)
            if raw is None:
                raise OAuthTokenAbsentError(
                    "no stored token for this principal/provider"
                )
            try:
                current = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise OAuthRevokedError(
                    "stored record is unreadable; ambiguous -> revoked"
                ) from None
            if not isinstance(current, dict) or current.get("revoked"):
                raise OAuthRevokedError("token is revoked")
            refresh_token = current.get("refresh_token")
            if not refresh_token:
                raise OAuthTokenAbsentError("no refresh token on this record")
            # BUG (found in this change, GOC-85 lane, fixed here): the refresh
            # grant was previously POSTed to ``provider.resource_url`` (the
            # protected MCP resource) instead of the authorization server's
            # token endpoint — refresh would never have worked against a real
            # provider. Re-discover here (not cached) so a rotated token
            # endpoint is always honored, mirroring begin()/callback().
            resource_meta = discover_protected_resource(
                http_client, provider.resource_url
            )
            as_meta = discover_authorization_server(
                http_client,
                resource_meta,
                authorization_server_url=provider.authorization_server_url,
            )
            payload = _exchange_refresh_token(
                http_client, as_meta.token_endpoint, effective_client_id, refresh_token
            )
            new_record = {
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token", refresh_token),
                "token_type": payload.get("token_type", "Bearer"),
                "expires_at": time.time() + float(payload.get("expires_in", 3600)),
                "granted_scope": payload.get("scope", current.get("granted_scope", "")),
                "key_version": self.CURRENT_KEY_VERSION,
                "audience": audience,
                "grant_revision": _new_grant_revision(),
                "revoked": False,
            }
            rotated = self._secrets.compare_and_set(
                key, raw, json.dumps(new_record, separators=(",", ":"))
            )
            if not rotated:
                raise OAuthRefreshRaceError(
                    "token record changed during refresh; retry"
                )
            return StoredToken(
                access_token=new_record["access_token"],
                refresh_token=new_record["refresh_token"],
                token_type=new_record["token_type"],
                expires_at=new_record["expires_at"],
                granted_scope=new_record["granted_scope"],
                key_version=new_record["key_version"],
                audience=new_record["audience"],
                grant_revision=new_record["grant_revision"],
            )

    def revoke(
        self,
        *,
        actor: ActorContext,
        provider_id: str,
        resource_url: str,
        audience: str,
    ) -> None:
        """Fail-closed revoke: always writes a tombstone record.

        An ambiguous outcome (record already gone, backend write partially failed,
        concurrent mutation) must never be readable as "still valid" — so this writes
        an explicit ``revoked: true`` record rather than merely attempting a delete.
        :meth:`get` treats ``revoked`` the same as absent.
        """
        tenant, principal = self._require_verified(actor)
        key = self._storage_key(tenant, principal, provider_id, resource_url, audience)
        lock = _lock_for(key)
        with lock:
            self._secrets.set(
                key,
                json.dumps(
                    {"revoked": True, "revoked_at": time.time()},
                    separators=(",", ":"),
                ),
            )


# ---------------------------------------------------------------------------
# Token exchange (mock-server tested only in this lane; see module docstring)
# ---------------------------------------------------------------------------
def _bounded_token_post(
    client: httpx.Client, url: str, data: dict[str, str]
) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OAuthDiscoveryError("token endpoint must be an exact HTTPS URL")
    try:
        with client.stream("POST", url, data=data) as resp:
            resp.raise_for_status()
            body = bytearray()
            for chunk in resp.iter_bytes(65536):
                body.extend(chunk)
                if len(body) > _MAX_TOKEN_RESPONSE_BYTES:
                    raise OAuthDiscoveryError("token response exceeded its bound")
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError("token exchange request failed") from exc
    try:
        payload = json.loads(bytes(body))
    except (ValueError, UnicodeDecodeError):
        raise OAuthDiscoveryError("token response was not valid JSON") from None
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise OAuthDiscoveryError("token response is missing access_token")
    return payload


def _exchange_refresh_token(
    client: httpx.Client, token_endpoint: str, client_id: str, refresh_token: str
) -> dict[str, Any]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    return _bounded_token_post(client, token_endpoint, data)


__all__ = [
    "AuthorizationServerMetadata",
    "OAuthBindingError",
    "OAuthDiscoveryError",
    "OAuthProviderError",
    "OAuthRefreshRaceError",
    "OAuthRevokedError",
    "OAuthStateError",
    "OAuthTokenAbsentError",
    "OAuthTokenStore",
    "OAuthTransaction",
    "ProtectedResourceMetadata",
    "ProviderDescriptor",
    "StoredToken",
    "TransactionStore",
    "discover_authorization_server",
    "discover_protected_resource",
]
