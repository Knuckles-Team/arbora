"""The delegated identity model and the token exchange (ARBORA-IDENTITY-R028.1, R028.2)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from arbora.access.delegation import (
    DelegatedIdentity,
    DelegationRefused,
    exchange_for_actor,
    requires_actor,
)
from arbora.identity.issuer import IssuerSettings, LocalIssuer

NOW = 1_800_000_000.0


class _Store:
    def get(self, key: str) -> str:
        from joserfc.jwk import RSAKey

        key_ = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
        return json.dumps({"active": key_.as_dict(private=True)})


def _issuer() -> LocalIssuer:
    settings = IssuerSettings(
        issuer="https://i", audience="arbora", tenant="tenant:one"
    )
    authority = SimpleNamespace(resolve_credential=lambda c: None)
    return LocalIssuer(_Store(), settings, authority, clock=lambda: NOW)


@pytest.mark.spec("ARBORA-IDENTITY-R028.1")
def test_model_round_trip_and_actor_flag_from_tools_list_meta() -> None:
    identity = DelegatedIdentity(
        subject="person:ada",
        tenant="tenant:one",
        audience="vector-mcp",
        scopes=("b", "a"),
        expires_at=1,
        exchange_id="x",
    )
    assert identity.scopes == ("a", "b")
    with pytest.raises(DelegationRefused) as invalid:
        DelegatedIdentity("", "t", "a", (), 1, "x")
    assert invalid.value.code == "DELEGATION_FIELD_INVALID"
    assert requires_actor({"team.knuckles/requires_actor": True}) is True
    assert requires_actor({"requires_actor": True}) is False
    assert requires_actor(None) is False


@pytest.mark.spec("ARBORA-IDENTITY-R028.2")
def test_exchange_lifetime_audience_and_expired_caller() -> None:
    caller = SimpleNamespace(
        principal="person:ada", tenant="tenant:one", effective_scopes=frozenset({"s:r"})
    )
    claims = {"sub": "person:ada", "tenant_id": "tenant:one", "exp": int(NOW) + 100}
    identity, token = asyncio.run(
        exchange_for_actor(caller, "vector-mcp", issuer=_issuer(), claims=claims)
    )
    assert identity.audience == "vector-mcp"
    assert identity.expires_at == int(NOW) + 100  # the remaining caller lifetime
    assert len(identity.exchange_id) == 32 and token.count(".") == 2
    long = {**claims, "exp": int(NOW) + 9_000}
    identity, _ = asyncio.run(
        exchange_for_actor(caller, "vector-mcp", issuer=_issuer(), claims=long)
    )
    assert identity.expires_at == int(NOW) + 300
    with pytest.raises(PermissionError):
        asyncio.run(
            exchange_for_actor(
                caller,
                "vector-mcp",
                issuer=_issuer(),
                claims={**claims, "exp": int(NOW) - 1},
            )
        )
