"""Focused contract for the ARBORA-CAPACITY-R002 hosted operations.

Exercises the operation's handler directly, via ``tests/api/_ops_support``'s
shared context builder, rather than the full ``get_registry()`` assembly
(that coverage lives in ``tests/api/test_registry_factory.py``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

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


def _partition_context(**services: Any) -> SimpleNamespace:
    context = _context(tenant="t1", **services)
    context.caller.policy_revision = "p1"
    return context


def test_capacity_ops_declare_exact_scopes_and_admin_effect() -> None:
    registry = Registry(capacity.operations())
    assert len(registry) == 4
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


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.4")
@pytest.mark.asyncio
async def test_receipt_reflects_readback_and_mismatch() -> None:
    from arbora.fleet.capacity_proposals import (
        CAPACITY_APPLY_MISMATCH,
        CapacityAppliedReceipt,
    )

    registry = ThrottleRegistry()
    partition = Partition(tenant="t1", policy_revision="p1", **_PARTITION_PARAMS)
    op = Registry(capacity.operations())["capacity.throttle.receipt.get"]
    assert op.scopes == frozenset({"capacity:read"}) and op.effect is Effect.READ
    assert {Surface.HTTP, Surface.MCP} <= op.surfaces
    context = _partition_context(throttle_registry=registry)
    none_yet = await capacity.handle_capacity_receipt_get(
        context, _PARTITION_PARAMS, op
    )
    assert none_yet == {"value": {"receipt": None}}
    for outcome, applied in (("raised", 6), (CAPACITY_APPLY_MISMATCH, 1)):
        registry.record_receipt(
            partition,
            CapacityAppliedReceipt(
                proposal_digest="a" * 64,
                requested_capacity=6,
                applied_capacity=applied,
                cell_id="fleet:search",
                policy_revision=1,
                state_revision=4,
                outcome=outcome,
            ),
        )
        shown = await capacity.handle_capacity_receipt_get(
            context, _PARTITION_PARAMS, op
        )
        receipt = shown["value"]["receipt"]
        assert receipt["outcome"] == outcome
        assert receipt["applied_capacity"] == applied
        assert receipt["requested_capacity"] == 6
    # Another tenant's partition has no receipt: the answer is the same as none.
    other = _partition_context(throttle_registry=registry)
    other.caller.tenant = "t2"
    assert (
        await capacity.handle_capacity_receipt_get(other, _PARTITION_PARAMS, op)
    ) == {"value": {"receipt": None}}


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.4")
def test_a_reader_gets_the_receipt_over_http_and_mcp_through_the_invoke_pipeline() -> (
    None
):
    from arbora.fleet.capacity_proposals import CapacityAppliedReceipt

    registry = ThrottleRegistry()
    registry.record_receipt(
        Partition(
            tenant="tenant:one", policy_revision="revision:one", **_PARTITION_PARAMS
        ),
        CapacityAppliedReceipt("a" * 64, 6, 6, "fleet:search", 1, 4, "raised"),
    )
    for surface in (Surface.HTTP, Surface.MCP):
        services, runtime, _audit, _journal, _leases = _setup_services(
            registry=Registry(capacity.operations()), effects=None
        )

        async def dispatched() -> dict[str, Any]:
            return await capacity.handle_capacity_receipt_get(
                _partition_context(throttle_registry=registry), _PARTITION_PARAMS, None
            )

        runtime.on_dispatch = dispatched
        result = asyncio.run(
            invoke(
                "capacity.throttle.receipt.get",
                _PARTITION_PARAMS,
                _caller(effective_scopes=frozenset({"capacity:read"})),
                surface,
                services=services,
            )
        )
        assert not isinstance(result, OpError), result
        assert len(runtime.calls) == 1
