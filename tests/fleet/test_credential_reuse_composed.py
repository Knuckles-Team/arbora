"""Scoped credential reuse is composed into the fleet and called by the connect path.

ARBORA-IDENTITY-R023.2 and R023.3: landed means reachable, so the tests bind the ports with
the host's composition function and reach them through the multiplexer's remote-child
connect, with a recorded engine verdict, a fake vault, and the broker boundary doubled only
at its network edge (the authorization URL and the token endpoint).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.access.credential_use import CredentialRefused
from arbora.fleet import credential_reuse
from arbora.fleet.credential_bridge import BrokerPkceBridge, CallbackWaiters
from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.remote_oauth_broker import (
    OAuthTransaction,
    ProviderDescriptor,
    ProviderRegistry,
    RemoteOAuthBroker,
    StoredToken,
)
from arbora.fleet.state import FleetState
from arbora.security.actor_context import ActorContext, ActorType, use_actor
from arbora.security.secrets import SecretsClient
from tests._support import FakeSecretsBackend
from tests.fleet.catalog_fixture import _NeverRead

URL = "https://protected-mcp.example.com/mcp"
PROVIDER = {
    "provider_id": "acme",
    "resource_url": URL,
    "client_id": "mux-public-client",
    "redirect_uri": "https://gateway.internal.example/oauth/callback",
    "scopes": ("mcp:read",),
    "enabled": True,
}
CFG = {
    "name": "acme-agent",
    "tool": "read",
    "oauth_provider": PROVIDER,
    "credential_owner": "person:owner",
}
VAULT_REF = "credentials/tenant:one/person:owner/acme"


class _Vault:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class _Immediate(CallbackWaiters):
    """The gateway callback lands as soon as the waiter is registered."""

    def __init__(self, token: Any) -> None:
        super().__init__()
        self.token = token

    def expect(self, state: str) -> Any:
        future = super().expect(state)
        self.deliver(state, self.token)
        return future


def _actor() -> ActorContext:
    return ActorContext(
        actor_id="person:agent",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        roles=("kg:read",),
        credential_expires_at=int(time.time()) + 3600,
    )


def _broker(refreshed: list[str]) -> RemoteOAuthBroker:
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor(**PROVIDER))
    broker = RemoteOAuthBroker(
        registry=registry, secrets_client=SecretsClient(backend=FakeSecretsBackend())
    )

    def begin(**kwargs: Any) -> str:
        now = time.time()
        broker.transactions.put(
            OAuthTransaction(
                state="state-1",
                nonce="nonce-1",
                verifier="v" * 64,
                provider_id="acme",
                tenant_id="tenant:one",
                principal_id="person:agent",
                browser_session_id=kwargs["browser_session_id"],
                redirect_uri=PROVIDER["redirect_uri"],
                scope=" ".join(kwargs["requested_scopes"]),
                created_at=now,
                expires_at=now + 120,
                client_id="mux-public-client",
            )
        )
        return (
            "https://idp.example.com/authorize?client_id=mux-public-client"
            "&state=state-1&code_challenge=chal&code_challenge_method=S256"
        )

    broker.begin = begin  # type: ignore[method-assign]

    def refresh(**kwargs: Any) -> StoredToken:
        refreshed.append(kwargs["provider"].provider_id)
        return _token("refreshed-access")

    broker.tokens.refresh = refresh  # type: ignore[method-assign]
    return broker


def _token(access: str, *, expires_in: float = 3600.0) -> StoredToken:
    return StoredToken(
        access_token=access,
        refresh_token="r",
        token_type="Bearer",
        expires_at=time.time() + expires_in,
        granted_scope="mcp:read",
        key_version=1,
        audience=URL,
        grant_revision="rev",
    )


def _admit(verdict: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    import medulla.generated.models as models

    class _Answer:
        def __init__(self, value: Any) -> None:
            self.value = value

        @classmethod
        def model_validate(cls, raw: Any) -> Any:
            return cls(
                SimpleNamespace(verdict=raw["verdict"], reason="r", decision_ref="d1")
            )

    monkeypatch.setattr(models, "FamilyAnswerCredentialReuse", _Answer, raising=False)
    asked: list[tuple] = []

    async def ask(kind: str, params: Any, tenant: str) -> dict:
        asked.append((kind, dict(params), tenant))
        return {"id": "record:1", "answer": {"verdict": verdict}}

    return ask, asked


def _compose(
    ask: Any, vault: _Vault, broker: RemoteOAuthBroker, waiters: CallbackWaiters
) -> tuple[FleetState, MCPMultiplexer]:
    fleet = FleetState()
    fleet.oauth_brokers._brokers["acme"] = broker
    assert credential_reuse.bind_credential_reuse(
        fleet,
        SimpleNamespace(decide_ask=ask),
        vault=vault,
        providers=lambda _id: PROVIDER,
    )
    bridge = BrokerPkceBridge(
        fleet.oauth_brokers, lambda _id: PROVIDER, waiters=waiters, actor=_actor
    )
    from arbora.access.host_ports import credential_ports_for_host

    fleet.credentials.bind(credential_ports_for_host(ask, vault, broker=bridge))
    return fleet, MCPMultiplexer(_NeverRead(), fleet=fleet)


def _connect(mux: MCPMultiplexer) -> tuple[Any, Any]:
    with use_actor(_actor()):
        return asyncio.run(mux._apply_remote_child_oauth_grant(dict(CFG), URL, None))


@pytest.mark.spec("ARBORA-IDENTITY-R023.2")
def test_connect_reuses_the_owners_credential_under_an_engine_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask, asked = _admit("admit", monkeypatch)
    vault = _Vault()
    vault.data[VAULT_REF] = json.dumps(
        {"access_token": "owner-access", "expires_at": time.time() + 600}
    )
    _fleet, mux = _compose(ask, vault, _broker([]), CallbackWaiters())
    headers, bearer = _connect(mux)
    assert headers == bearer == {"Authorization": "Bearer owner-access"}
    assert asked[0][0] == "credential_reuse"
    assert (
        asked[0][1]["owner"] == "person:owner"
        and asked[0][1]["connector"] == "acme-agent"
    )


@pytest.mark.spec("ARBORA-IDENTITY-R023.3")
def test_a_missing_credential_runs_one_inline_pkce_challenge_and_persists_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask, asked = _admit("admit", monkeypatch)
    vault = _Vault()
    waiters = _Immediate(_token("fresh-access"))
    _fleet, mux = _compose(ask, vault, _broker([]), waiters)
    headers, _ = _connect(mux)
    assert headers == {"Authorization": "Bearer fresh-access"}
    stored = json.loads(vault.data[VAULT_REF])
    assert stored["access_token"] == "fresh-access"
    assert len(asked) == 2  # one decision before the challenge, one fresh one after it


@pytest.mark.spec("ARBORA-IDENTITY-R023.3")
def test_an_expired_credential_is_refreshed_through_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask, _asked = _admit("admit", monkeypatch)
    vault = _Vault()
    vault.data[VAULT_REF] = json.dumps(
        {"access_token": "stale", "expires_at": time.time() - 5}
    )
    refreshed: list[str] = []
    _fleet, mux = _compose(ask, vault, _broker(refreshed), CallbackWaiters())
    headers, _ = _connect(mux)
    assert headers == {"Authorization": "Bearer refreshed-access"}
    assert refreshed == ["acme"]


@pytest.mark.spec("ARBORA-IDENTITY-R023.4")
def test_deny_abstain_and_an_unbound_fleet_open_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for verdict in ("deny", "abstain"):
        ask, _asked = _admit(verdict, monkeypatch)
        vault = _Vault()
        vault.data[VAULT_REF] = json.dumps({"access_token": "owner-access"})
        _fleet, mux = _compose(ask, vault, _broker([]), CallbackWaiters())
        with pytest.raises(CredentialRefused) as refused:
            _connect(mux)
        assert refused.value.refusal.value == "REUSE_DENIED"
    unbound = MCPMultiplexer(_NeverRead(), fleet=FleetState())
    with pytest.raises(CredentialRefused) as refused:
        _connect(unbound)
    assert refused.value.refusal.value == "PROVIDER_UNAVAILABLE"
