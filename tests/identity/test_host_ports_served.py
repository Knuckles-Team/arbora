"""The resource gate and credential reuse are composed into the host behind pin-lag.

ARBORA-IDENTITY-R019.3 and ARBORA-IDENTITY-R023.3: landed means reachable, so the tests
build the host's gate through the one composition function and run it as the invoke pipeline
does, with a structural double for the engine and a refusal test for the pinned release.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.access import host_ports
from arbora.access.credential_use import (
    CredentialRefusal,
    CredentialRefused,
    CredentialUseRequest,
    resolve_credential,
)
from arbora.pin_lag.unavailable import PinLagUnavailable
from arbora.security.operation_result import OpError

NOW = 1_800_000_000.0
CALLER = SimpleNamespace(
    tenant="t1", principal="ada", client="cli", request_id="r", surface=None
)
OP = SimpleNamespace(id="fleet.call", executor=SimpleNamespace(value="caller"))
DECISION = SimpleNamespace(resource_protocol="database", resource_param="target")
PARAMS = {"server": "sql-mcp", "tool": "run", "arguments": {"target": "db:one"}}


def _gate(send, events):
    return host_ports.resource_gate_for_host(
        send, clock=lambda: NOW, emit=events.append
    )


def _call(gate) -> Any:
    return asyncio.run(gate(OP, PARAMS, CALLER, DECISION))


@pytest.mark.spec("ARBORA-IDENTITY-R019.3")
def test_host_gate_asks_the_engine_decision_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_ports, "require_methods", lambda *names: None)
    monkeypatch.setattr(host_ports, "require_models", lambda names: None)
    monkeypatch.setattr(host_ports, "require_enum_member", lambda model, value: None)
    sent: list[tuple[str, dict, str]] = []

    async def send(method: str, params: dict, tenant: str) -> dict:
        sent.append((method, params, tenant))
        return {
            "allow": True,
            "grant_id": "g1",
            "expires_at_ms": int(NOW * 1000) + 60_000,
            "revocation_revision": 1,
            "scope_ceiling": [],
        }

    events: list = []
    assert _call(_gate(send, events)) is None
    assert sent == [
        (
            "CheckResourceAccess",
            {
                "agent_id": "cli",
                "operation": "run",
                "protocol": "database",
                "resource_ref": "db:one",
            },
            "t1",
        )
    ]
    assert events and events[0]["engine_decision_ref"] == "g1"


@pytest.mark.spec("ARBORA-IDENTITY-R019.3")
def test_pinned_release_without_the_method_refuses_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lacking(*names: str) -> None:
        raise PinLagUnavailable(names[0])

    monkeypatch.setattr(host_ports, "require_methods", lacking)
    sent: list = []

    async def send(*args: Any) -> dict:
        sent.append(args)
        return {"allow": True}

    refused = _call(_gate(send, []))
    assert isinstance(refused, OpError) and refused.code == "AUTHORITY_UNAVAILABLE"
    assert sent == []  # no engine call and no local grant


@pytest.mark.spec("ARBORA-IDENTITY-R019.3")
def test_the_real_protocol_goes_on_the_wire_behind_pin_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_ports, "require_methods", lambda *names: None)
    monkeypatch.setattr(host_ports, "require_models", lambda names: None)
    sent: list[dict] = []

    async def send(method: str, params: dict, tenant: str) -> dict:
        sent.append(params)
        return {"allow": True, "grant_id": "g1", "expires_at_ms": int(NOW * 1000) + 1}

    # A pinned engine whose ResourceProtocol lacks `database` refuses; nothing says `mcp`.
    assert isinstance(_call(_gate(send, [])), OpError)
    assert sent == []
    monkeypatch.setattr(host_ports, "require_enum_member", lambda model, value: None)
    _call(_gate(send, []))
    assert sent[0]["protocol"] == "database"


@pytest.mark.spec("ARBORA-IDENTITY-R023.3")
def test_credential_ports_refuse_unavailable_until_the_engine_decides() -> None:
    class Vault:
        def get(self, key: str) -> None:
            return None

    async def ask(*args: Any) -> dict:
        raise AssertionError("never reached on a pinned release without the model")

    ports = host_ports.credential_ports_for_host(ask, Vault())
    request = CredentialUseRequest(
        tenant="t1",
        owner_principal="ada",
        requesting_principal="ada",
        client="cli",
        connector="github-agent",
        provider="github",
        audience="api",
        scopes=("repo",),
        resource="r",
        operation="read",
    )
    ports.emit = lambda event: None
    with pytest.raises(CredentialRefused) as refused:
        asyncio.run(resolve_credential(request, ports))  # type: ignore[arg-type]
    assert refused.value.refusal is CredentialRefusal.PROVIDER_UNAVAILABLE
    assert (
        asyncio.run(ports.challenge(request, 1)) is None
    )  # no broker: interaction required


@pytest.mark.spec("ARBORA-IDENTITY-R019.3")
def test_served_invocation_composes_the_host_gate() -> None:
    import inspect

    from arbora.mcp_server import served_invocation

    assert "resource_gate_for_host" in inspect.getsource(served_invocation)
