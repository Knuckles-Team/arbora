"""Tests for provider-neutral OIDC discovery.

Fleet auth is configured by issuer URL only; jwks_uri + token_endpoint are resolved
from the issuer's OIDC discovery document, so Keycloak/Okta/Auth0/Entra all work with
the same plumbing (no vendor-specific paths in config).
"""

from __future__ import annotations

import argparse
import time

import pytest

from graph_os.security import oidc_discovery

ISS = "https://idp.test/realms/homelab"  # non-loopback issuer requires HTTPS (AU-OS.identity)
DOC = {
    "issuer": ISS,
    "jwks_uri": f"{ISS}/protocol/openid-connect/certs",
    "token_endpoint": f"{ISS}/protocol/openid-connect/token",
}
# An Okta-shaped issuer to prove vendor-neutrality (different paths).
OKTA = "https://org.okta.test/oauth2/default"
OKTA_DOC = {
    "issuer": OKTA,
    "jwks_uri": f"{OKTA}/v1/keys",
    "token_endpoint": f"{OKTA}/v1/token",
}


@pytest.fixture(autouse=True)
def _seed_cache():
    oidc_discovery._cache.clear()
    oidc_discovery._cache[ISS] = (time.monotonic() + 1000, DOC)
    oidc_discovery._cache[OKTA] = (time.monotonic() + 1000, OKTA_DOC)
    yield
    oidc_discovery._cache.clear()


def test_jwks_and_token_resolve_per_provider() -> None:
    assert oidc_discovery.jwks_uri_for(ISS) == DOC["jwks_uri"]
    assert oidc_discovery.token_endpoint_for(ISS) == DOC["token_endpoint"]
    # Okta has different paths — discovery abstracts that away.
    assert oidc_discovery.jwks_uri_for(OKTA) == OKTA_DOC["jwks_uri"]
    assert oidc_discovery.token_endpoint_for(OKTA) == OKTA_DOC["token_endpoint"]


def test_trailing_slash_normalized() -> None:
    assert oidc_discovery.jwks_uri_for(ISS + "/") == DOC["jwks_uri"]


def test_discovery_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*, timeout: float = 15.0) -> object:
        raise RuntimeError("network down")

    monkeypatch.setattr(oidc_discovery, "oidc_http_client", _boom)
    assert oidc_discovery.jwks_uri_for("http://unreachable.test/iss") is None
    assert oidc_discovery.token_endpoint_for("http://unreachable.test/iss") is None


def test_oidc_client_options_pin_egress_and_admit_loopback() -> None:
    """The transport agrees with the endpoint validator's loopback policy."""
    options = oidc_discovery._options(10.0)

    assert options.pin_egress is True
    assert options.allow_loopback is True
    assert options.timeout == 10.0


def test_minter_token_url_from_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_os.mcp_server.backing import client_credentials

    monkeypatch.delenv("OIDC_TOKEN_URL", raising=False)
    monkeypatch.setenv("OIDC_ISSUER", ISS)
    assert client_credentials._derive_token_url() == DOC["token_endpoint"]


def test_minter_explicit_token_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_os.mcp_server.backing import client_credentials

    monkeypatch.setenv("OIDC_TOKEN_URL", "http://explicit.test/token")
    monkeypatch.setenv("OIDC_ISSUER", ISS)
    assert client_credentials._derive_token_url() == "http://explicit.test/token"


def test_minter_multi_issuer_uses_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_os.mcp_server.backing import client_credentials

    monkeypatch.delenv("OIDC_TOKEN_URL", raising=False)
    monkeypatch.setenv("OIDC_ISSUER", f"{ISS},{OKTA}")  # primary = first
    assert client_credentials._derive_token_url() == DOC["token_endpoint"]


def test_server_factory_discovers_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    from graph_os.mcp_server.backing.server_factory import _configure_jwt_auth

    monkeypatch.delenv("FASTMCP_SERVER_AUTH_JWT_JWKS_URI", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    args = argparse.Namespace(
        token_jwks_uri=None,
        token_issuer=ISS,
        token_audience="agent-services",
        token_algorithm=None,
        token_secret=None,
        token_secret_ref=None,
        token_public_key=None,
        required_scopes=None,
    )
    verifier = _configure_jwt_auth(args)
    assert isinstance(verifier, JWTVerifier)
