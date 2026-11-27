"""The composed wallet authority and change door (ARBORA-DATA-MARKET-R007.3)."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.finance.wallet_port import (
    EngineWalletOperations,
    FleetFinanceChangePort,
    reservation_id,
)
from arbora.mcp_server.served_invocation import _with_finance_change_ports

PROJECTION = {
    "tenant_id": "t1",
    "actor": "actor-1",
    "account_id": "acct-1",
    "network_id": "xrpl",
    "wallet_id": "w1",
    "operation_id": "pay",
    "plan_digest": "d" * 64,
}


@pytest.fixture(autouse=True)
def _engine_declares_the_grant_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arbora.pin_lag.medulla.require_methods", lambda *_n: None)


class Engine:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.nodes: set[str] = set()

    async def wire(self, method: str, params: dict[str, Any]) -> Any:
        self.sent.append((method, params))
        if method == "CheckResourceAccess":
            return {"allow": self.allow}
        if method == "CreateNodeIfAbsent":
            fresh = params["node_id"] not in self.nodes
            self.nodes.add(params["node_id"])
            return fresh
        return {}

    async def client(self) -> Any:
        return object()


def port(engine: Engine, provider: Any = None) -> EngineWalletOperations:
    return EngineWalletOperations(
        engine.wire, engine.client, provider=provider, clock_ms=lambda: 1_000_000
    )


@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
def test_composition_binds_the_wallet_authority_and_the_change_door() -> None:
    session = lambda: SimpleNamespace(tenant="t1")  # noqa: E731
    bound = _with_finance_change_ports({}, object(), "g", lambda t: t, session)
    assert isinstance(bound["wallet_operations"], EngineWalletOperations)
    assert isinstance(bound["finance_change_port"], FleetFinanceChangePort)
    mine = object()
    assert (
        _with_finance_change_ports(
            {"wallet_operations": mine}, object(), "g", lambda t: t, session
        )["wallet_operations"]
        is mine
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
async def test_verify_refuses_unavailable_on_a_pin_without_the_sdk_trust_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "scion_sdk.writeback.wallet_trust", None)
    monkeypatch.setitem(
        sys.modules, "scion_sdk.writeback.wallet_intent", SimpleNamespace(
            WalletBusinessIntent=1, WalletConfirmation=1, WalletExecutionEnvelope=1,
            wallet_plan_digest=1,
        ),
    )  # fmt: skip
    with pytest.raises(OperationRefused) as refused:
        await port(Engine()).verify(PROJECTION, {"signature": "x"})
    assert refused.value.code == "UNAVAILABLE"
    assert "wallet_trust" in str(refused.value.details)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
async def test_authorization_is_the_engine_grant_ledger_and_apply_needs_a_provider() -> (
    None
):
    granted, denied = Engine(True), Engine(False)
    assert (await port(granted).authorize(PROJECTION))["allowed"] is True
    assert (await port(denied).authorize(PROJECTION))["allowed"] is False
    method, asked = granted.sent[0]
    assert method == "CheckResourceAccess"
    assert asked["agent_id"] == "actor-1" and asked["resource_ref"] == "wallet:xrpl:w1"
    # No provider door is published: refuse before any reservation exists.
    with pytest.raises(OperationRefused) as refused:
        await port(granted).apply(PROJECTION, "k1")
    assert refused.value.code == "UNAVAILABLE"
    assert not granted.nodes


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
async def test_a_reserved_key_applies_once_and_a_second_attempt_is_uncertain() -> None:
    engine = Engine()
    calls: list[str] = []

    async def provider(projection: Any, key: str) -> Any:
        calls.append(key)
        return {"provider_ref": "tx-1"}

    wallet = port(engine, provider)
    first = await wallet.apply(PROJECTION, "k1")
    again = await wallet.apply(PROJECTION, "k1")
    assert first["state"] == "applied" and first["provider_ref"] == "tx-1"
    assert again["state"] == "uncertain" and calls == ["k1"]
    assert first["effect_receipt"] == reservation_id("t1", "k1")
    await wallet.reconcile(PROJECTION, "k1")
    method, params = engine.sent[-1]
    assert method == "CompareAndSetNodeFields"
    assert msgpack.unpackb(params["updates_msgpack"], raw=False) == {
        "state": "reconciling"
    }


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
async def test_the_change_door_cancels_an_owned_order_and_refuses_what_the_venue_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops import finance

    venue: list[Any] = []

    async def fleet(_c: Any, _op: str, arguments: Any, **_k: Any) -> Any:
        venue.append(arguments["arguments"])
        return {"value": {"status": "CANCELLED"}}

    monkeypatch.setattr(finance, "invoke_registered", fleet)

    class Reads:
        async def account_binding(self, _c: Any, _t: str) -> str:
            return "acct-1"

    context = SimpleNamespace(services={"finance_read_port": Reads()})
    change = {
        "tenant_id": "t1",
        "account_id": "acct-1",
        "order_id": "o1",
        "action": "cancel",
    }
    door = FleetFinanceChangePort()
    receipt = await door.apply(context, "order", "id", change, "k")
    assert receipt["state"] == "applied" and venue == [
        {"action": "cancel", "order_id": "o1"}
    ]
    for kind, body in (("order", {**change, "action": "amend"}), ("position", change)):
        with pytest.raises(OperationRefused) as refused:
            await door.apply(context, kind, "id", body, "k")
        assert refused.value.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as foreign:
        await door.apply(
            context, "order", "id", {**change, "account_id": "acct-9"}, "k"
        )
    assert foreign.value.code == "NOT_FOUND"
    assert len(venue) == 1
