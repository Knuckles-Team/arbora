"""Exercise the real local TLS issuer and its signed client token."""

import json
import ssl
import threading

import httpx
import jwt
import pytest

from arbora.certification.oidc import CertificationAuthorityError, _LoopbackServer


def _tls(ca_pem: str) -> ssl.SSLContext:
    return ssl.create_default_context(cadata=ca_pem)


@pytest.mark.spec("ARBORA-HOST-R027.1")
def test_discovery_and_jwks_are_loopback_only(certification_authority):
    authority = certification_authority
    with httpx.Client(verify=_tls(authority.ca_pem), trust_env=False) as client:
        discovery = client.get(f"{authority.issuer}/.well-known/openid-configuration")
        discovery.raise_for_status()
        data = discovery.json()
        assert data["issuer"] == authority.issuer
        assert data["jwks_uri"] == f"{authority.issuer}/jwks"
        assert client.get(data["jwks_uri"]).json()["keys"][0]["kty"] == "RSA"
    with pytest.raises(CertificationAuthorityError, match="loopback"):
        _LoopbackServer(
            ("0.0.0.0", 0),
            tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
            stop_event=threading.Event(),
        )


@pytest.mark.spec("ARBORA-HOST-R027.2")
def test_fixture_mints_token_verified_with_served_jwks(certification_authority):
    authority = certification_authority
    environment = authority.child_environment({}, model_private_hosts=[])
    oauth = json.loads(environment["KG_IDENTITY_OAUTH2"])
    secret = environment["ARBORA_SKILL_CERT_OIDC_CLIENT_SECRET"]
    with httpx.Client(verify=_tls(authority.ca_pem), trust_env=False) as client:
        keys = client.get(f"{authority.issuer}/jwks").json()["keys"]
        denied = client.post(
            oauth["token_url"],
            data={"grant_type": "client_credentials", "scope": "kg:admin"},
            auth=(oauth["client_id"], "wrong-secret"),
        )
        assert denied.status_code == 401
        token = client.post(
            oauth["token_url"],
            data={"grant_type": "client_credentials", "scope": "kg:admin"},
            auth=(oauth["client_id"], secret),
        )
        token.raise_for_status()
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(keys[0])
    claims = jwt.decode(
        token.json()["access_token"],
        public_key,
        algorithms=["RS256"],
        audience=oauth["audience"],
        issuer=authority.issuer,
    )
    assert claims["scope"] == "kg:admin"
