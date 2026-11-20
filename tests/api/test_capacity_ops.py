"""Focused contract for the ARBORA-CAPACITY-R002 hosted operations.

Exercises the operation's handler directly, via ``tests/api/_ops_support``'s
shared context builder, rather than the full ``get_registry()`` assembly
(that coverage lives in ``tests/api/test_registry_factory.py``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_invoke_contract import caller as _caller
from test_invoke_contract import setup_services as _setup_services

from arbora.api.invoke import OpError, invoke
from arbora.api.ops import capacity
from arbora.api.registry import Confirm, Effect, PrincipalRule, Registry, Surface
from arbora.fleet.error_budget import (
    AimdConfig,
    BudgetWindow,
    OutcomeClass,
    OutcomeSample,
    Partition,
    ThrottleMode,
    decide,
)
from arbora.fleet.throttle_service import ThrottleRegistry
from tests.api._ops_support import service_context as _context

pytestmark = pytest.mark.spec("ARBORA-CAPACITY-R002")

_PARTITION_PARAMS = {
    "child": "search",
    "operation_class": "read",
}


def _partition_context(**services: object) -> SimpleNamespace:
    context = _context(tenant="t1", **services)
    context.caller.policy_revision = "p1"
    return context


def test_capacity_ops_declare_exact_scopes_and_admin_effect() -> None:
    registry = Registry(capacity.operations())
    assert len(registry) == 2
    status_op = registry["capacity.throttle.status"]
    mode_op = registry["capacity.throttle.set_mode"]
    assert status_op.scopes == frozenset({"capacity:read"})
    assert status_op.effect is Effect.READ
    assert mode_op.scopes == frozenset({"capacity:admin"})
    assert mode_op.effect is Effect.ADMIN
    assert mode_op.confirm is Confirm.CONSOLE
    assert mode_op.principals is PrincipalRule.HUMAN_UNDELEGATED
    assert Surface.MCP in status_op.surfaces
    assert all(op.examples for op in registry)


@pytest.mark.asyncio
async def test_status_handler_reports_unbound_controller_closed() -> None:
    op = capacity.operations()[0]
    with pytest.raises(RuntimeError, match="not bound"):
        await capacity.handle_capacity_status(
            _partition_context(), _PARTITION_PARAMS, op
        )


@pytest.mark.asyncio
async def test_status_handler_reports_recorded_decision() -> None:
    registry = ThrottleRegistry()
    partition = Partition(tenant="t1", policy_revision="p1", **_PARTITION_PARAMS)
    window = BudgetWindow(
        window_id="w1",
        partition=partition,
        samples=tuple(OutcomeSample(OutcomeClass.TIMEOUT) for _ in range(8)),
    )
    config = AimdConfig(
        version="v1",
        alpha=2,
        beta=0.5,
        floor=1,
        min_sample_count=4,
        error_budget_fraction=0.2,
    )
    decision = decide(
        window=window,
        prior_limit=10,
        engine_headroom=20,
        config=config,
        mode=ThrottleMode.ENFORCE,
    )
    registry.record_decision(decision)

    op = capacity.operations()[0]
    result = await capacity.handle_capacity_status(
        _partition_context(throttle_registry=registry), _PARTITION_PARAMS, op
    )
    assert result == {
        "value": {
            "mode": "observe",
            "current_limit": decision.new_limit,
            "reason": "budget_breach_decrease",
        }
    }


@pytest.mark.asyncio
async def test_set_mode_handler_updates_and_is_read_back() -> None:
    registry = ThrottleRegistry()
    op = capacity.operations()[1]
    params = {**_PARTITION_PARAMS, "mode": "enforce"}
    result = await capacity.handle_capacity_set_mode(
        _partition_context(throttle_registry=registry), params, op
    )
    assert result == {"value": {"mode": "enforce"}}
    assert (
        registry.mode(Partition(tenant="t1", policy_revision="p1", **_PARTITION_PARAMS))
        is ThrottleMode.ENFORCE
    )


@pytest.mark.asyncio
async def test_set_mode_handler_reports_unbound_controller_closed() -> None:
    op = capacity.operations()[1]
    params = {**_PARTITION_PARAMS, "mode": "enforce"}
    with pytest.raises(RuntimeError, match="not bound"):
        await capacity.handle_capacity_set_mode(_partition_context(), params, op)


def test_denied_set_mode_leaves_throttle_state_unchanged() -> None:
    """A read-scoped caller cannot reach the administrative mode mutation."""
    throttle_registry = ThrottleRegistry()
    services, runtime, _audit, _journal, _leases = _setup_services(
        registry=Registry(capacity.operations()), effects=None
    )

    result = asyncio.run(
        invoke(
            "capacity.throttle.set_mode",
            {**_PARTITION_PARAMS, "mode": "enforce"},
            _caller(
                effective_scopes=frozenset({"capacity:read"}),
                engine_claims={
                    "principal": "person:one",
                    "tenant": "tenant:one",
                    "scopes": ["capacity:read"],
                    "policy_version": "revision:one",
                    "delegation": False,
                },
            ),
            Surface.HTTP,
            services=services,
        )
    )

    assert isinstance(result, OpError)
    assert result.code == "SCOPE_REQUIRED"
    assert result.details == {"missing_scopes": ["capacity:admin"]}
    assert runtime.calls == []
    denied_partition = Partition(
        tenant="tenant:one", policy_revision="revision:one", **_PARTITION_PARAMS
    )
    assert throttle_registry.mode(denied_partition) is ThrottleMode.OBSERVE
