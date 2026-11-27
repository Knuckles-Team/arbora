"""Delegated identity is composed from the deployment into the fleet (ARBORA-IDENTITY-R028.3).

Landed means reachable: the test binds the exchange through the composition function the
host runs, then reaches the multiplexer's one exchange call site as an acting user.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from arbora.access.delegation import DelegationRefused
from arbora.fleet.multiplexer import _ENGINE_CONFIG_FIELDS, MCPMultiplexer
from arbora.fleet.state import FleetState
from arbora.identity import actor_exchange_composition as composition
from arbora.identity.deployment_settings import IdentityDeployment
from arbora.security.actor_context import ActorContext, ActorType, use_actor
from arbora.security.error_surface import public_error_payload
from arbora.security.session import GraphSession, use_session
from tests.fleet.catalog_fixture import _NeverRead

CFG = {
    "name": "keycloak-agent",
    "tool": "get_user",
    "requires_actor": True,
    "client_id": "keycloak-agent",
}


class _Store:
    def __init__(self) -> None:
        from joserfc.jwk import RSAKey

        key = {
            **RSAKey.generate_key(2048, private=True).as_dict(private=True),
            "kid": "fixture-key",
            "use": "sig",
            "alg": "RS256",
        }
        self.value = json.dumps({"active": key})

    def get(self, name: str) -> str:
        return self.value


def _actor(kind: ActorType = ActorType.HUMAN) -> ActorContext:
    return ActorContext(
        actor_id="person:ada",
        actor_type=kind,
        tenant_id="tenant:one",
        authenticated=True,
        roles=("kg:read",),
        credential_expires_at=int(time.time()) + 3600,
    )


def _resolve(mux: MCPMultiplexer, actor: ActorContext) -> dict | None:
    session = GraphSession(
        actor=actor,
        tenant=actor.tenant_id,
        scopes=frozenset({"kg:read"}),
        graph=actor.tenant_id,
        policy_version="test",
        audience="test",
    )
    with use_actor(actor), use_session(session):
        return asyncio.run(mux._resolve_actor_exchange_bearer(dict(CFG)))


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    import arbora.identity.deployment_settings as deployment
    import arbora.security.settings as settings

    monkeypatch.setattr(
        deployment,
        "identity_deployment",
        lambda: IdentityDeployment("https://issuer.test", "tenant:one", False),
    )
    monkeypatch.setattr(
        settings,
        "security_settings",
        lambda _config: SimpleNamespace(
            auth_jwt_issuer=None, auth_jwt_audience="arbora"
        ),
    )


@pytest.mark.spec("ARBORA-IDENTITY-R028.3")
def test_composed_exchange_serves_actor_required_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    fleet = FleetState()
    mux = MCPMultiplexer(_NeverRead(), fleet=fleet)
    events: list = []
    assert composition.bind_actor_exchange(
        fleet, store_factory=_Store, audit=events.append
    )
    headers = _resolve(mux, _actor())
    assert headers is not None and headers["Authorization"].startswith("Bearer ")
    import base64

    token = headers["Authorization"].removeprefix("Bearer ")
    segment = token.split(".")[1]
    body = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    assert body["aud"] == "keycloak-agent" and body["sub"] == "person:ada"
    assert body["tenant_id"] == "tenant:one"
    assert (
        "requires_actor" in _ENGINE_CONFIG_FIELDS
        and "client_id" in _ENGINE_CONFIG_FIELDS
    )
    assert (
        events[0]["result_status"] == "ok" and events[0]["audience"] == "keycloak-agent"
    )
    assert "Bearer" not in repr(events)


@pytest.mark.spec("ARBORA-IDENTITY-R028.4")
def test_unbound_or_service_callers_refuse_with_mapped_public_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unbound = MCPMultiplexer(_NeverRead(), fleet=FleetState())
    with pytest.raises(DelegationRefused) as refused:
        _resolve(unbound, _actor())
    assert refused.value.code == "IDENTITY_DELEGATION_REQUIRED"
    _configure(monkeypatch)
    fleet = FleetState()
    assert composition.bind_actor_exchange(fleet, store_factory=_Store)
    mux = MCPMultiplexer(_NeverRead(), fleet=fleet)
    with pytest.raises(DelegationRefused):
        _resolve(mux, _actor(ActorType.AUTOMATED_SERVICE))
    payload = public_error_payload(refused.value)
    assert payload["error"]["code"] == "permission_denied"
    assert payload["refusal_code"] == "IDENTITY_DELEGATION_REQUIRED"
    failed = public_error_payload(DelegationRefused("IDENTITY_EXCHANGE_FAILED"))
    assert failed["error"]["code"] == "dependency_unavailable"
    assert failed["error"]["retryable"] is True
    assert "refusal_code" not in public_error_payload(RuntimeError("x"))


@pytest.mark.spec("ARBORA-IDENTITY-R028.3")
def test_an_unconfigured_deployment_leaves_the_exchange_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbora.identity.deployment_settings as deployment

    monkeypatch.setattr(
        deployment, "identity_deployment", lambda: IdentityDeployment(None, None, False)
    )
    assert composition.bind_actor_exchange(FleetState(), store_factory=_Store) is False
