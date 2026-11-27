"""The HTTP side of the OIDC code flow against a mock issuer (ARBORA-IDENTITY-R009.2.3)."""

from __future__ import annotations

import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from arbora.identity.engine import IdentityUnavailable
from arbora.identity.oidc_transport import HttpOidcTransport

ISSUER = "https://idp.invalid/realm"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(kid: str = "k1") -> dict[str, object]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _issuer(token_status: int = 200, signing_key: object = KEY, kid: str = "k1"):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/jwks",
                    "end_session_endpoint": ISSUER + "/logout",
                },
            )
        if path.endswith("/token"):
            token = jwt.encode(
                {"iss": ISSUER, "sub": "alice", "nonce": "n"},
                signing_key,
                algorithm="RS256",
                headers={"kid": kid},
            )
            return httpx.Response(token_status, json={"id_token": token})
        return httpx.Response(200, json=_jwks())

    transport = HttpOidcTransport(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    return transport, seen


def _exchange(transport: HttpOidcTransport):
    return asyncio.run(
        transport.exchange(
            ISSUER,
            client_id="c",
            client_secret="s",
            code="code-1",
            verifier="v" * 64,
            redirect_uri="https://console.invalid/auth/oidc/callback",
        )
    )


@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_exchange_sends_the_verifier_and_returns_signature_verified_claims() -> None:
    transport, seen = _issuer()
    exchanged = _exchange(transport)
    assert exchanged.claims["sub"] == "alice" and exchanged.id_token.count(".") == 2
    form = next(r for r in seen if r.url.path.endswith("/token")).content.decode()
    assert (
        "code_verifier=" + "v" * 64 in form and "grant_type=authorization_code" in form
    )
    assert (
        asyncio.run(transport.authorization_endpoint(ISSUER)) == ISSUER + "/authorize"
    )
    assert asyncio.run(transport.end_session_endpoint(ISSUER)) == ISSUER + "/logout"


@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_a_forged_unknown_key_or_refused_code_is_a_permission_refusal() -> None:
    forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(PermissionError):
        _exchange(_issuer(signing_key=forger)[0])
    with pytest.raises(PermissionError):
        _exchange(_issuer(kid="other")[0])
    with pytest.raises(PermissionError):
        _exchange(_issuer(token_status=400)[0])


@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_a_discovery_document_for_another_issuer_is_unavailable() -> None:
    transport, _ = _issuer()
    with pytest.raises(IdentityUnavailable):
        asyncio.run(transport.authorization_endpoint("https://other.invalid/realm"))
