"""The served process repairs its own OAuth clients (ARBORA-IDENTITY-R030.4, .4)."""

from __future__ import annotations

import argparse
import queue
import threading
from typing import Any

import pytest

from arbora.identity import client_contract as cc
from arbora.identity.client_contract import (
    ContractEvent,
    ContractState,
    ReconcileResult,
)
from arbora.observability import runtime_health
from tests.identity.test_client_contract import CONFIG, FakeRealm, factory


class Lockstep(threading.Event):
    """A stop event that parks the supervisor at every wait until the test resumes it."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: queue.Queue[float] = queue.Queue()
        self.resume: queue.Queue[None] = queue.Queue()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        self.asked.put(float(timeout or 0.0))
        self.resume.get(timeout=30)
        return self.is_set()


@pytest.mark.spec("ARBORA-IDENTITY-R030.4")
def test_deleted_native_client_is_recreated_and_readiness_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    from arbora.mcp_server import runtime

    async def health() -> dict[str, Any]:
        return {"checks": [{"name": "engine", "status": "healthy"}]}

    monkeypatch.setattr(runtime_health, "collect_health_async", health)
    _args, mcp, _middlewares = runtime._build_server(bootstrap=False)
    client = TestClient(mcp.http_app())

    realm = FakeRealm()
    supervisor = cc.ClientContractSupervisor(CONFIG, admin_factory=factory(realm))
    stop = Lockstep()
    supervisor.stop_event = stop
    supervisor.start()
    observed: list[dict[str, Any]] = []
    try:
        stop.asked.get(timeout=30)  # first pass done: converged, waiting the interval
        observed.append(client.get("/health/ready").json())
        assert supervisor.reconciler.state is ContractState.CONVERGED
        # An out-of-band cleanup deletes the client while the provider is down.
        realm.delete_native()
        realm.down = True
        stop.resume.put(None)
        stop.asked.get(timeout=30)  # interval pass failed: degraded, backing off
        observed.append(client.get("/health/ready").json())
        realm.down = False
        stop.resume.put(None)
        stop.asked.get(timeout=30)  # retry pass recreated the client
        observed.append(client.get("/health/ready").json())
    finally:
        stop.set()
        stop.resume.put(None)
        supervisor.stop()

    assert [body.get("identity") for body in observed] == [
        {"status": "ready"},
        {"status": "degraded", "reason": "identity_admin_unreachable"},
        {"status": "ready"},
    ]
    assert all(body["status"] == "ready" for body in observed)  # never gating
    native = realm.native()
    assert native["redirectUris"] == list(cc.DEFAULT_NATIVE_REDIRECT_URIS)
    assert realm.defaults[native["id"]] == set(cc.NATIVE_DEFAULT_SCOPES)
    assert not any(method == "DELETE" for method, _ in realm.calls)
    assert supervisor.reconciler.state is ContractState.STOPPED
    assert runtime_health.identity_readiness() is None


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.4")
def test_degraded_contract_keeps_registration_in_metadata() -> None:
    """While degraded, the served metadata still points clients at the provider."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from arbora.mcp_server.backing.server_factory import _configure_jwt_auth

    machine = cc.ClientContractReconciler()
    machine.apply(ContractEvent.RECONCILE_START)
    machine.apply(
        ContractEvent.RECONCILE_ERR,
        result=ReconcileResult(False, "client_missing", ("client:arbora-native-mcp",)),
    )
    runtime_health.register_identity_contract(machine)
    try:
        args = argparse.Namespace(
            token_jwks_uri="https://idp.example/realms/fixture/protocol/openid-connect/certs",
            token_issuer="https://idp.example/realms/fixture",
            token_audience="agent-services",
            token_algorithm=None,
            token_secret=None,
            token_public_key=None,
            required_scopes=None,
            public_base_url="https://arbora.example",
        )
        provider = _configure_jwt_auth(args)
        app = Starlette(routes=provider.get_routes(mcp_path="/mcp"))
        document = (
            TestClient(app).get("/.well-known/oauth-protected-resource/mcp").json()
        )
        assert runtime_health.identity_readiness() == {
            "status": "degraded",
            "reason": "identity_client_missing",
        }
    finally:
        runtime_health.register_identity_contract(None)
    # The authorization server stays advertised, so a native client discovers
    # the provider's own registration endpoint and registers dynamically.
    assert document["authorization_servers"] == ["https://idp.example/realms/fixture"]
