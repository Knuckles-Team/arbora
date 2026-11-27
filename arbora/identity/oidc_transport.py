"""The HTTP side of the OIDC code flow: discovery, code exchange, ID-token signature.

Only the network and the signature live here. Issuer, audience, expiry and nonce are
checked by ``arbora.identity.oidc.check_id_token_claims`` over the claims this returns,
so the one check owner stays one (ARBORA-IDENTITY-R009.2.2, R009.2.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .engine import IdentityUnavailable

_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "EdDSA"]
_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Exchanged:
    """A signature-verified ID token: its raw form (the logout hint) and its claims."""

    id_token: str
    claims: Mapping[str, Any]


class HttpOidcTransport:
    """Discovery, token endpoint and JWKS over one injected ``httpx`` client factory."""

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._factory = client_factory or _default_client

    async def _discovery(self, issuer: str) -> dict[str, Any]:
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with self._factory() as http:
            reply = await http.get(url)
        document = _json(reply)
        if document.get("issuer") != issuer:
            raise IdentityUnavailable("OIDC discovery issuer disagrees")
        return document

    async def authorization_endpoint(self, issuer: str) -> str:
        return _endpoint(await self._discovery(issuer), "authorization_endpoint")

    async def end_session_endpoint(self, issuer: str) -> str | None:
        value = (await self._discovery(issuer)).get("end_session_endpoint")
        return (
            value if isinstance(value, str) and value.startswith("https://") else None
        )

    async def exchange(
        self,
        issuer: str,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> Exchanged:
        """Swap the code (with the PKCE verifier) for a signature-verified ID token."""
        document = await self._discovery(issuer)
        async with self._factory() as http:
            reply = await http.post(
                _endpoint(document, "token_endpoint"),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": verifier,
                },
            )
            if 400 <= reply.status_code < 500:
                raise PermissionError("the provider refused the code")
            token = _json(reply).get("id_token")
            if not isinstance(token, str) or not token:
                raise PermissionError("the provider returned no ID token")
            jwks = _json(await http.get(_endpoint(document, "jwks_uri")))
        return Exchanged(token, _verified_claims(token, jwks))


def _default_client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False)


def _endpoint(document: Mapping[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.startswith("https://"):
        raise IdentityUnavailable(f"OIDC discovery lacks an https {name}")
    return value


def _json(reply: Any) -> dict[str, Any]:
    if reply.status_code != 200:
        raise IdentityUnavailable("OIDC provider answered unavailable")
    try:
        body = reply.json()
    except ValueError:
        raise IdentityUnavailable("OIDC provider answered no JSON") from None
    if not isinstance(body, dict):
        raise IdentityUnavailable("OIDC provider answered an unexpected shape")
    return body


def _verified_claims(token: str, jwks: Mapping[str, Any]) -> Mapping[str, Any]:
    import jwt

    try:
        header = jwt.get_unverified_header(token)
        keys = [k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")]
        if len(keys) != 1 or header.get("alg") not in _ALGORITHMS:
            raise PermissionError("ID token key is unknown")
        return jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0]).key,
            algorithms=[header["alg"]],
            options={
                "verify_aud": False,
                "verify_iss": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError:
        raise PermissionError("ID token signature is invalid") from None
