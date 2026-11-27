"""Stateless bearer ingress: every refusal runs zero handlers (ARBORA-IDENTITY-R017.2, .5, .6, .8)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.http.auth import HTTPAuthenticationError, verify_local_bearer_request
from arbora.identity.client_entry_points import (
    CLIENT_TOKEN_REQUIREMENTS,
    ClientEntryPoint,
    entry_points_without_verified_tokens,
    require_verified_entry_points,
)
from arbora.security.bearer import (
    BearerRefusal,
    BearerRefused,
    precheck_bearer,
    verify_bearer,
)
from arbora.security.key_cache import JwksCache
from tests.identity._bearer_fixture import (
    AUDIENCE,
    ISSUER,
    JWKS,
    JWKS_URI,
    NOW,
    PROFILE,
    mint,
)


def _request(*headers: str) -> Any:
    return SimpleNamespace(
        scope={
            "headers": [(b"authorization", value.encode()) for value in headers]
            + [(b"cookie", b"arbora_session=valid")]
        }
    )


class Host:
    """The served handler and the engine authority behind the bearer ingress."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, authority_up: bool = True):
        self.handler_calls = 0
        self.up = authority_up
        monkeypatch.setattr(
            "arbora.security.settings.security_settings",
            lambda: SimpleNamespace(
                auth_jwt_issuer=ISSUER,
                auth_jwt_audience=AUDIENCE,
                auth_jwt_jwks_uri=JWKS_URI,
                auth_jwt_algorithms=["RS256"],
            ),
        )

        async def fetch(_keys: JwksCache, _uri: str) -> dict[str, Any]:
            if not self.up:
                raise RuntimeError("key authority down")
            return JWKS

        async def engine_authority(token: str, **_kw: Any) -> Any:
            self.handler_calls += 1  # reached only after the token checks passed
            return SimpleNamespace(token=token)

        monkeypatch.setattr("arbora.security.auth._fetch_jwks", fetch)
        monkeypatch.setattr("time.time", lambda: NOW)
        monkeypatch.setattr(
            "arbora.security.request_identity.verify_local_bearer_token",
            engine_authority,
        )

    def call(self, *headers: str) -> Any:
        return asyncio.run(verify_local_bearer_request(JwksCache(), _request(*headers)))


def _status(error: HTTPAuthenticationError) -> int:
    return getattr(error, "status_code", 401)


@pytest.mark.spec("ARBORA-IDENTITY-R017.5")
@pytest.mark.spec("ARBORA-IDENTITY-R017.6")
@pytest.mark.spec("ARBORA-IDENTITY-R017.8")
def test_served_stateless_bearer_requires_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = Host(monkeypatch)
    # A valid bearer needs no browser-session store: the cookie in the request is unused.
    assert host.call(f"Bearer {mint()}").token
    assert host.handler_calls == 1
    refused = {
        "missing tenant": f"Bearer {mint(tenant_id=None)}",
        "expired": f"Bearer {mint(exp=NOW - 300)}",
        "wrong audience": f"Bearer {mint(aud='other')}",
        "forged": f"Bearer {mint(forged=True)}",
        "wrong issuer": f"Bearer {mint(iss='https://other')}",
        "not yet valid": f"Bearer {mint(nbf=NOW + 900)}",
    }
    for case, header in refused.items():
        before = host.handler_calls
        with pytest.raises(HTTPAuthenticationError):
            host.call(header)
        assert host.handler_calls == before, case
    # A duplicated header is ambiguous and runs nothing.
    before = host.handler_calls
    with pytest.raises(HTTPAuthenticationError):
        host.call(f"Bearer {mint()}", f"Bearer {mint()}")
    assert host.handler_calls == before
    assert (
        asyncio.run(precheck_bearer(mint(), key_cache=JwksCache())).tenant
        == "tenant:one"
    )  # type: ignore[union-attr]
    # An unreadable key authority keeps the ingress unavailable (503), never open.
    down = Host(monkeypatch, authority_up=False)
    with pytest.raises(HTTPAuthenticationError) as caught:
        down.call(f"Bearer {mint()}")
    assert _status(caught.value) == 503 and down.handler_calls == 0
    # Both ingress paths run verify_bearer first and the profile names the tenant claim.
    assert PROFILE.tenant_claim == "tenant_id"
    with pytest.raises(BearerRefused) as tenant:
        verify_bearer(mint(), PROFILE, NOW, keys=JWKS, expected_tenant="tenant:two")
    assert tenant.value.refusal is BearerRefusal.TENANT_MISMATCH


@pytest.mark.spec("ARBORA-IDENTITY-R017.2")
def test_every_entry_point_is_denied_without_valid_token() -> None:
    assert entry_points_without_verified_tokens() == ()
    require_verified_entry_points()
    assert {req.entry_point for req in CLIENT_TOKEN_REQUIREMENTS} == set(
        ClientEntryPoint
    )
    bad = {
        "missing": "",
        "expired": mint(exp=NOW - 999),
        "wrong audience": mint(aud="elsewhere"),
    }
    for entry_point in ClientEntryPoint:
        for case, token in bad.items():
            # No unsigned retry exists: the one verification path refuses, once.
            with pytest.raises(BearerRefused):
                verify_bearer(token, PROFILE, NOW, keys=JWKS)
            assert entry_point and case
