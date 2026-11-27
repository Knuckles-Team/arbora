"""Durable native sign-in (ARBORA-IDENTITY-R030.4.8 and R030.4.9).

Protected-resource metadata and the 401 challenge carry the native client's
sign-in scopes, so a client requests ``offline_access`` and receives an offline
refresh token. Readiness reports every identity contract state.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from starlette.testclient import TestClient

from arbora.identity import client_contract as cc
from arbora.identity.client_contract import ContractEvent, ReconcileResult
from arbora.mcp_server.backing.server_factory import _configure_jwt_auth
from arbora.mcp_server.host_context import build_host_context
from arbora.observability import runtime_health
from tests.identity.test_client_contract import CONFIG, FakeRealm, factory

ISSUER = "https://idp.example/realms/fixture"
AUDIENCE = "agent-services"


def _provider(keys: RSAKeyPair, required_scopes: str | None = None) -> Any:
    args = argparse.Namespace(
        token_jwks_uri=None,
        token_issuer=ISSUER,
        token_audience=AUDIENCE,
        token_algorithm="RS256",
        token_secret=None,
        token_secret_ref=None,
        token_public_key=keys.public_key,
        required_scopes=required_scopes,
        public_base_url="https://arbora.example",
    )
    return _configure_jwt_auth(args)


def _client(provider: Any) -> TestClient:
    """A client over the served app; use as a context manager to run the lifespan."""
    mcp = FastMCP("fixture", auth=provider)
    return TestClient(mcp.http_app(path="/mcp"), base_url="https://arbora.example")


def _token(keys: RSAKeyPair, scopes: list[str]) -> str:
    return keys.create_token(
        subject="user", issuer=ISSUER, audience=AUDIENCE, scopes=scopes
    )


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.8")
def test_metadata_scopes_supported_equal_the_native_contract_scopes() -> None:
    client = _client(_provider(RSAKeyPair.generate()))
    document = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert document["scopes_supported"] == list(cc.NATIVE_DEFAULT_SCOPES)
    assert "offline_access" in document["scopes_supported"]


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.8")
def test_challenge_carries_the_same_scope_list() -> None:
    client = _client(_provider(RSAKeyPair.generate()))
    missing = client.post("/mcp", json={})
    assert missing.status_code == 401
    challenge = missing.headers["www-authenticate"]
    match = re.search(r'scope="([^"]*)"', challenge)
    assert match is not None
    assert match.group(1).split() == list(cc.NATIVE_DEFAULT_SCOPES)
    assert "resource_metadata=" in challenge
    # An invalid token gets the same scope parameter.
    bad = client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert (
        'scope="' + " ".join(cc.NATIVE_DEFAULT_SCOPES) + '"'
        in (bad.headers["www-authenticate"])
    )


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.8")
def test_advertising_scopes_does_not_require_them() -> None:
    keys = RSAKeyPair.generate()
    # No offline_access and no scope at all: service and client-credential
    # tokens are still verified.
    with _client(_provider(keys)) as client:
        for scopes in ([], ["mcp-fleet"]):
            reply = client.post(
                "/mcp",
                json={},
                headers={"Authorization": f"Bearer {_token(keys, scopes)}"},
            )
            assert reply.status_code not in (401, 403)
    # A configured required scope is still enforced, and is not widened.
    with _client(_provider(keys, required_scopes="mcp-fleet")) as strict:
        refused = strict.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {_token(keys, [])}"}
        )
        assert refused.status_code == 401
        accepted = strict.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Bearer {_token(keys, ['mcp-fleet'])}"},
        )
        assert accepted.status_code not in (401, 403)


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.8")
def test_served_sign_in_that_follows_the_advertised_scopes_gets_an_offline_token() -> (
    None
):
    """L3: the reconciled realm issues an offline refresh token to that sign-in."""
    realm = FakeRealm()
    reconciler, result = cc.reconcile_now(CONFIG, admin_factory=factory(realm))
    assert result.ok, result
    assert reconciler.state is cc.ContractState.CONVERGED

    client = _client(_provider(RSAKeyPair.generate()))
    challenge = client.post("/mcp", json={}).headers["www-authenticate"]
    advertised = re.search(r'scope="([^"]*)"', challenge)
    assert advertised is not None
    native = realm.native()["id"]

    followed = realm.sign_in(native, advertised.group(1))
    assert followed.refresh_type == "Offline"
    assert "offline_access" in followed.scope.split()
    # Without the advertised scopes the provider issues an ordinary session
    # token, which is the defect this row removes.
    ignored = realm.sign_in(native, "")
    assert ignored.refresh_type == "Refresh"
    assert "offline_access" not in ignored.scope.split()


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.9")
def test_readiness_reports_each_identity_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.mcp_server import runtime

    async def health(_health) -> dict[str, Any]:
        return {"checks": [{"name": "engine", "status": "healthy"}]}

    monkeypatch.setattr(runtime_health, "collect_health_async", health)
    monkeypatch.delenv(cc.CONFIG_KEY, raising=False)
    host = build_host_context()
    _args, mcp, _middlewares = runtime._build_server(bootstrap=False, host=host)
    client = TestClient(mcp.http_app())

    def identity() -> dict[str, Any]:
        reply = client.get("/health/ready")
        assert reply.status_code == 200  # never gating
        return reply.json()["identity"]

    assert identity() == {"status": "unconfigured"}

    monkeypatch.setenv(cc.CONFIG_KEY, "{not json")
    assert (
        cc.start_client_contract_supervisor(host.observability.health.identity) is None
    )
    try:
        assert identity() == {
            "status": "degraded",
            "reason": "identity_config_invalid",
        }
    finally:
        host.observability.health.identity.set_config_invalid(False)
    # Set but not yet started: checking.
    monkeypatch.setenv(cc.CONFIG_KEY, "{}")
    assert identity() == {"status": "checking"}
    monkeypatch.delenv(cc.CONFIG_KEY)

    machine = cc.ClientContractReconciler()
    host.observability.health.identity.reconciler.set(machine)
    try:
        assert identity() == {"status": "checking"}
        machine.apply(ContractEvent.RECONCILE_START)
        machine.apply(
            ContractEvent.RECONCILE_ERR,
            result=ReconcileResult(False, "scope_missing", ("scope:x",)),
        )
        assert identity() == {"status": "degraded", "reason": "identity_scope_missing"}
        machine.apply(ContractEvent.RETRY_ELAPSED)
        machine.apply(ContractEvent.RECONCILE_OK, result=ReconcileResult(True))
        assert identity() == {"status": "ready"}
        # Codes only: no client or scope name reaches the route.
        assert "scope:x" not in client.get("/health/ready").text
    finally:
        host.observability.health.identity.reconciler.set(None)
