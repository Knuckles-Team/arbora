"""Federated sign-in: the OIDC code flow and the SAML ACS (ARBORA-IDENTITY-R009.2.3, R012.2.3.2).

Arbora verifies the external identity and hands it to the engine's ``external_login``; the
engine's identity-provider mapping and JIT policy decide. Arbora evaluates no mapping rule.

Arbora holds nothing between the redirect and the callback. The state, nonce and PKCE
verifier are sealed in the engine by ``register_login_state`` and released exactly once by
``consume_login_state``; a SAML assertion id is recorded by ``register_assertion``, which
refuses a second use. Every refusal reaches the browser as the one 401; the precise code
goes to the audit event only, so no refusal tells an attacker which check failed.

PIN-LAG: the three ``token`` operations are engine ``main`` shapes the pinned release
lacks; until it carries them, the routes answer ``UNAVAILABLE`` naming the missing shape.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from arbora.pin_lag.medulla import require_models

from .engine import IdentityUnavailable
from .mfa_ceremonies import Audit, log_audit
from .oidc import OidcRefusalReason, OidcTokenRefused, check_id_token_claims
from .saml import (
    ParsedSamlAssertion,
    SamlAssertionRefused,
    SamlRefusalReason,
    SamlServiceProvider,
    check_assertion_replay,
    verify_assertion,
)

OIDC_LOGIN, OIDC_CALLBACK, SAML_ACS = (
    "/auth/oidc/login",
    "/auth/oidc/callback",
    "/auth/saml/acs",
)
LOGIN_STATE_MODELS = (
    "IdentityOpTokenRegisterLoginState",
    "IdentityOpTokenConsumeLoginState",
)
ASSERTION_MODELS = ("IdentityOpTokenRegisterAssertion",)
#: The engine refuses a longer life; the state and verifier live ten minutes at most.
LOGIN_STATE_TTL_MS = 600_000
HINT_COOKIE = "__Host-arbora_oidc_hint"
_SPENT = "IDENTITY_TOKEN_SPENT"
_REPLAY = "IDENTITY_REPLAY"
_SAML_CODES = {
    SamlRefusalReason.WRONG_AUDIENCE: "SAML_AUDIENCE_MISMATCH",
    SamlRefusalReason.WRONG_RECIPIENT: "SAML_AUDIENCE_MISMATCH",
    SamlRefusalReason.NOT_YET_VALID: "SAML_EXPIRED",
    SamlRefusalReason.EXPIRED: "SAML_EXPIRED",
    SamlRefusalReason.INVALID_SIGNATURE: "SAML_SIGNATURE_INVALID",
    SamlRefusalReason.REPLAYED: "SAML_REPLAYED",
}


class FederatedRefused(PermissionError):
    """A federated sign-in step refused; ``code`` is for the audit event, never the body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class FederatedIdentity:
    """A verified external identity, ready for the engine's ``external_login``."""

    protocol: str
    idp_id: str
    subject: str
    claims: dict[str, list[str]]
    username_hint: str | None = None
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class OidcProvider:
    """One ``identity.oidc.providers`` entry; the secret is a reference, never a value."""

    id: str
    issuer: str
    client_id: str
    secret_ref: str


@dataclass(frozen=True, slots=True)
class SamlProvider:
    """One ``identity.saml.providers`` entry; the certificate is a reference."""

    id: str
    entity_id: str
    acs_url: str
    idp_certificate_ref: str


@dataclass(frozen=True, slots=True)
class ReadSamlResponse:
    """What the injected reader found in a response, with no verification done."""

    assertion: ParsedSamlAssertion
    attributes: Mapping[str, tuple[str, ...]]
    signature_count: int
    signed_is_read_assertion: bool


class SamlResponseReader(Protocol):
    """XML and signature implementation (``arbora.identity.saml_reader``, R012.2.5)."""

    def read(self, response: str) -> ReadSamlResponse:
        """Parse the response; raise ``ValueError`` when it is not a SAML response."""
        ...

    def verify(self, assertion: ParsedSamlAssertion, idp_certificate_pem: str) -> bool:
        """True only when the read assertion is validly signed by the certificate."""
        ...


def _ident(value: Any) -> str:
    if not isinstance(value, str) or not value or "." in value:
        raise IdentityUnavailable("identity provider id is invalid")
    return value


def oidc_providers(text: str) -> dict[str, OidcProvider]:
    """Parse ``identity.oidc.providers`` (JSON text); refuse a malformed entry."""
    try:
        return {
            _ident(item["id"]): OidcProvider(
                item["id"],
                item["issuer"],
                item["client_id"],
                item["secret_ref"],
            )
            for item in json.loads(text or "[]")
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        raise IdentityUnavailable("identity.oidc.providers is malformed") from None


def saml_providers(text: str) -> dict[str, SamlProvider]:
    """Parse ``identity.saml.providers`` (JSON text); refuse a malformed entry."""
    try:
        return {
            _ident(item["id"]): SamlProvider(
                item["id"],
                item["entity_id"],
                item["acs_url"],
                item["idp_certificate_ref"],
            )
            for item in json.loads(text or "[]")
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        raise IdentityUnavailable("identity.saml.providers is malformed") from None


async def token_op(
    client: Any, graph: str, op: str, request: dict[str, Any], kind: str
) -> Any:
    """One engine ``token`` operation; an engine refusal is a ``FederatedRefused``."""
    from medulla import EngineResponseError
    from medulla.generated.security import decode_identity, send_identity

    try:
        async with asyncio.timeout(5):
            response = await send_identity(
                client,
                {"op": {"family": "token", "op": op, "request": request}},
                graph,
            )
            decode_identity(response)
    except EngineResponseError as refused:
        if refused.code in (_SPENT, _REPLAY):
            raise FederatedRefused(refused.code) from None
        raise IdentityUnavailable("qualified identity operation unavailable") from None
    except Exception:  # noqa: BLE001 - an unanswered question refuses nothing and admits nothing
        raise IdentityUnavailable("qualified identity operation unavailable") from None
    payload = response.payload
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise IdentityUnavailable("identity response kind disagrees")
    return payload.get("value")


class EngineSamlReplayStore:
    """The ``SamlReplayStore`` port over the engine's ``register_assertion``."""

    def __init__(self, client: Any, graph: str, idp_id: str) -> None:
        self._client, self._graph, self._idp = client, graph, idp_id

    async def register(self, assertion_id: str, expires_at: datetime) -> bool:
        """True when the id was new; False when the engine already held it."""
        require_models(ASSERTION_MODELS)
        try:
            await token_op(
                self._client,
                self._graph,
                "register_assertion",
                {
                    "idp_id": self._idp,
                    "assertion_id": assertion_id,
                    "not_on_or_after_ms": int(expires_at.timestamp() * 1000),
                },
                "done",
            )
        except FederatedRefused as refused:
            if refused.code == _REPLAY:
                return False
            raise
        return True


def _flatten(claims: Mapping[str, Any]) -> dict[str, list[str]]:
    """The scalar and list-of-text claims the engine's mapping rules read."""
    flat: dict[str, list[str]] = {}
    for name, value in claims.items():
        values = [value] if isinstance(value, (str, bool, int)) else value
        if isinstance(values, (list, tuple)) and all(
            isinstance(v, (str, bool, int)) for v in values
        ):
            flat[str(name)] = [
                str(v).lower() if isinstance(v, bool) else str(v) for v in values
            ]
    return flat


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class Federation:
    """The OIDC and SAML sign-in steps over one engine client and one public origin."""

    def __init__(
        self,
        client: Any,
        graph: str,
        origin: str,
        *,
        oidc: Mapping[str, OidcProvider] | None = None,
        saml: Mapping[str, SamlProvider] | None = None,
        transport: Any = None,
        saml_reader: SamlResponseReader | None = None,
        resolve: Callable[[str], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        audit: Audit = log_audit,
    ) -> None:
        self._client, self._graph, self._origin = client, graph, origin
        self._oidc, self._saml = dict(oidc or {}), dict(saml or {})
        self._transport, self._reader = transport, saml_reader
        self._resolve = resolve or _resolve_secret
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audit = audit

    @property
    def redirect_uri(self) -> str:
        return self._origin + OIDC_CALLBACK

    def audit(self, protocol: str, provider: str, outcome: str) -> None:
        """One audit event: protocol, provider and outcome; never a secret."""
        self._audit({"protocol": protocol, "provider": provider, "outcome": outcome})

    def _refuse(self, protocol: str, provider: str, code: str) -> FederatedRefused:
        self.audit(protocol, provider, code)
        return FederatedRefused(code)

    # -- OIDC ---------------------------------------------------------------

    async def oidc_start(self, provider_id: str | None) -> str:
        """Seal state, nonce and verifier in the engine; the URL to send the browser to."""
        provider = self._oidc.get(provider_id or "")
        if provider is None:
            raise self._refuse("oidc", str(provider_id), "OIDC_PROVIDER_UNKNOWN")
        require_models(LOGIN_STATE_MODELS)
        state = f"{provider.id}.{secrets.token_urlsafe(24)}"
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        await token_op(
            self._client,
            self._graph,
            "register_login_state",
            {
                "state": state,
                "nonce": nonce,
                "pkce_verifier": verifier,
                "ttl_ms": LOGIN_STATE_TTL_MS,
            },
            "login_state_sealed",
        )
        endpoint = await self._transport.authorization_endpoint(provider.issuer)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": provider.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": _s256(verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def oidc_complete(self, query: Mapping[str, str]) -> FederatedIdentity:
        """Consume the state, exchange the code, check the token; refuse uniformly."""
        state, code = query.get("state", ""), query.get("code", "")
        provider = self._oidc.get(state.partition(".")[0])
        if provider is None or not code or "error" in query:
            raise self._refuse("oidc", state.partition(".")[0], "OIDC_STATE_MISMATCH")
        require_models(LOGIN_STATE_MODELS)
        try:
            released = await token_op(
                self._client,
                self._graph,
                "consume_login_state",
                {"state": state},
                "login_state",
            )
        except FederatedRefused:
            raise self._refuse("oidc", provider.id, "OIDC_STATE_MISMATCH") from None
        exchanged = await self._exchange(provider, code, released["pkce_verifier"])
        self._check_token(provider, exchanged.claims, released["nonce"])
        claims = _flatten(exchanged.claims)
        subject = exchanged.claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise self._refuse("oidc", provider.id, "OIDC_TOKEN_INVALID")
        hint = exchanged.claims.get("preferred_username")
        return FederatedIdentity(
            "oidc",
            provider.id,
            subject,
            claims,
            hint if isinstance(hint, str) else None,
            f"{provider.id}.{exchanged.id_token}",
        )

    async def _exchange(self, provider: OidcProvider, code: str, verifier: str) -> Any:
        secret = self._resolve(provider.secret_ref)
        if not secret:
            raise IdentityUnavailable("OIDC client secret is not available")
        try:
            return await self._transport.exchange(
                provider.issuer,
                client_id=provider.client_id,
                client_secret=secret,
                code=code,
                verifier=verifier,
                redirect_uri=self.redirect_uri,
            )
        except PermissionError:
            raise self._refuse("oidc", provider.id, "OIDC_TOKEN_INVALID") from None

    def _check_token(
        self, provider: OidcProvider, claims: Mapping[str, Any], nonce: str
    ) -> None:
        try:
            check_id_token_claims(
                claims,
                issuer=provider.issuer,
                audience=provider.client_id,
                nonce=nonce,
                now=self._clock(),
            )
        except OidcTokenRefused as refused:
            code = (
                "OIDC_NONCE_MISMATCH"
                if refused.reason is OidcRefusalReason.NONCE_MISMATCH
                else "OIDC_TOKEN_INVALID"
            )
            raise self._refuse("oidc", provider.id, code) from None

    async def end_session_url(self, hint: str | None) -> str | None:
        """The provider's logout URL carrying ``id_token_hint``, or ``None``."""
        provider = self._oidc.get((hint or "").partition(".")[0])
        if provider is None:
            return None
        try:
            endpoint = await self._transport.end_session_endpoint(provider.issuer)
        except Exception:  # noqa: BLE001 - logout never fails on a provider outage
            return None
        if endpoint is None:
            return None
        query = urlencode(
            {
                "id_token_hint": (hint or "").partition(".")[2],
                "post_logout_redirect_uri": self._origin + "/",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    # -- SAML ---------------------------------------------------------------

    async def saml_complete(self, form: Mapping[str, str]) -> FederatedIdentity:
        """Signature, conditions, replay, mapping, in that order (R012.2.3.2)."""
        if self._reader is None:
            raise IdentityUnavailable("no SAML response reader is configured")
        try:
            read = self._reader.read(form.get("SAMLResponse", ""))
        except ValueError:
            raise self._refuse("saml", "", "SAML_UNSIGNED") from None
        assertion = read.assertion
        provider = next(
            (p for p in self._saml.values() if p.acs_url == assertion.recipient), None
        )
        if provider is None:
            raise self._refuse("saml", "", "SAML_AUDIENCE_MISMATCH")
        self._check_signatures(provider, read)
        await self._verify_saml(provider, assertion)
        claims = {name: list(values) for name, values in read.attributes.items()}
        return FederatedIdentity("saml", provider.id, assertion.subject, claims)

    def _check_signatures(self, provider: SamlProvider, read: ReadSamlResponse) -> None:
        if read.signature_count == 0:
            raise self._refuse("saml", provider.id, "SAML_UNSIGNED")
        if read.signature_count > 1 or not read.signed_is_read_assertion:
            raise self._refuse("saml", provider.id, "SAML_SIGNATURE_WRAPPING")

    async def _verify_saml(
        self, provider: SamlProvider, assertion: ParsedSamlAssertion
    ) -> None:
        pem = self._resolve(provider.idp_certificate_ref)
        if not pem:
            raise IdentityUnavailable("SAML IdP certificate is not available")
        service = SamlServiceProvider(provider.entity_id, provider.acs_url, pem)
        try:
            verify_assertion(service, assertion, self._reader, self._clock())
            await check_assertion_replay(
                assertion,
                EngineSamlReplayStore(self._client, self._graph, provider.id),
            )
        except SamlAssertionRefused as refused:
            code = _SAML_CODES.get(refused.reason, "SAML_SIGNATURE_INVALID")
            raise self._refuse("saml", provider.id, code) from None


def _resolve_secret(ref: str) -> str | None:
    from arbora.security.secrets import create_secrets_client

    return create_secrets_client().resolve_ref(ref)


def federation_from_settings(
    client: Any, graph: str, origin: str, *, saml_reader: Any = None
) -> Federation | None:
    """A ``Federation`` when providers are configured, else ``None`` (the routes are absent)."""
    from arbora.core.environment import read_env

    oidc = oidc_providers(str(read_env("ARBORA_IDENTITY_OIDC_PROVIDERS") or ""))
    saml = saml_providers(str(read_env("ARBORA_IDENTITY_SAML_PROVIDERS") or ""))
    if not oidc and not saml:
        return None
    from .oidc_transport import HttpOidcTransport

    return Federation(
        client,
        graph,
        origin,
        oidc=oidc,
        saml=saml,
        transport=HttpOidcTransport(),
        saml_reader=saml_reader or _entry_point_reader(),
    )


def _entry_point_reader() -> Any:
    """The SAML reader: an installed entry point of ``arbora.identity.saml_reader``, else the built-in."""
    from importlib.metadata import entry_points

    for point in entry_points(group="arbora.identity.saml_reader"):
        return point.load()()
    from .saml_reader import SamlReader

    return SamlReader()
