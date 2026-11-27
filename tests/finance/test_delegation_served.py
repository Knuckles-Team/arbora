"""The owner's delegation is an engine resource grant (ARBORA-DATA-MARKET-R002)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import finance_delegation as ops
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Idempotency

NO_OP: Any = None


class Engine:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def send_main(
        self, _context: Any, method: str, params: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.sent.append((method, params, kwargs))
        return {}


def ctx(**services: Any) -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t1", principal="owner-1"),
        services={
            "finance_scheduler_principal": "svc-1",
            "now_ms": lambda: 1_000,
            **services,
        },
    )


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    fake = Engine()
    monkeypatch.setattr("arbora.api.ops.pin_guards.send_main", fake.send_main)
    return fake


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_grant_writes_one_engine_resource_grant_for_the_scheduler(
    engine: Engine,
) -> None:
    out = await ops.handle_delegation_grant(
        ctx(), {"grant": "finance:track", "expires_in_s": 120}, NO_OP
    )
    assert out["value"]["held"] is True
    method, params, kwargs = engine.sent[0]
    assert method == "RbacAdmin" and kwargs["idempotent"] is True
    grant = params["op"]["GrantResource"]
    assert grant == {
        "grant_id": "finance-delegation:owner-1:finance:track",
        "subject": {"Principal": "svc-1"},
        "resource_ref": "finance-delegation:owner-1",
        "protocol": "knowledge",
        "operation": "finance:track",
        "scope_ceiling": ["finance:track"],
        "granted_at_ms": 1_000,
        "expires_at_ms": 121_000,
    }


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_revoke_names_the_owners_own_grant_only(engine: Engine) -> None:
    out = await ops.handle_delegation_revoke(ctx(), {"grant": "finance:read"}, NO_OP)
    assert out["value"]["held"] is False
    assert engine.sent[0][1] == {
        "op": {
            "RevokeResource": {"grant_id": "finance-delegation:owner-1:finance:read"}
        }
    }


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_grant_refuses_unavailable_without_a_composed_scheduler(
    engine: Engine,
) -> None:
    context = SimpleNamespace(
        caller=SimpleNamespace(tenant="t1", principal="owner-1"), services={}
    )
    with pytest.raises(OperationRefused) as refused:
        await ops.handle_delegation_grant(
            context, {"grant": "finance:read", "expires_in_s": 60}, NO_OP
        )
    assert refused.value.code == "UNAVAILABLE" and engine.sent == []


@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
def test_operations_are_plan_confirmed_with_a_replay_owner() -> None:
    registry = get_registry()
    for op_id, owner in (
        ("finance.delegation.grant", "medulla:GrantResource"),
        ("finance.delegation.revoke", "medulla:RevokeResource"),
    ):
        spec = registry.get(op_id)
        assert spec is not None
        assert spec.confirm is Confirm.PLAN
        assert spec.idempotency is Idempotency.KEY_REQUIRED
        assert spec.scopes == frozenset({"finance:track"})
        assert ops.NATIVE_IDEMPOTENCY[op_id] == owner
