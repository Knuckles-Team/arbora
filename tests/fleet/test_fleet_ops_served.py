"""Reconcile, scale and deploy-watch dispatch are served operations (ARBORA-FLEET-R024, R024.2)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.fleet.scale_records import (
    ScaleActuationRequest,
    ScaleActuationResult,
    ScaleIntentState,
)
from arbora.api.domain.fleet.scaling.scaling_authority import ScaleIntent
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.pipeline_services import _declared_owners
from arbora.api.ops import fleet_deploy_watch, fleet_scaling
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Effect, Idempotency, OpSpec
from arbora.fleet.scaling.scaling_intent_reconciler import (
    AllowScalePolicy,
    MemoryScaleIntentLedger,
    ScaleIntentReconciler,
)
from tests.fleet.scaling.test_scaling_authority_contract import INTENT as PLAN_INTENT
from tests.fleet.scaling.test_scaling_authority_contract import authority
from tests.fleet.scaling.test_scaling_intent_reconciler import INTENT, NOW

OPERATOR = SimpleNamespace(effective_scopes=frozenset({"mcp:delegate"}))
OUTSIDER = SimpleNamespace(effective_scopes=frozenset({"kg:read"}))


class _Actuator:
    name = "fixture"

    def __init__(self) -> None:
        self.calls = 0

    def apply(self, request: ScaleActuationRequest) -> ScaleActuationResult:
        self.calls += 1
        return ScaleActuationResult(
            execution_key=request.execution_key,
            target=request.target,
            state=ScaleIntentState.SUCCEEDED,
            scaled=True,
            retryable=False,
            error_code=None,
            observed_replicas=request.desired_replicas,
        )


async def _snapshot(context: Any) -> Any:
    return authority()


def _context(caller: Any, **services: Any) -> Any:
    return SimpleNamespace(caller=caller, services=services)


def _op(module: Any, op_id: str) -> OpSpec:
    return next(op for op in module.operations() if op.id == op_id)


def _reconciler(actuator: _Actuator, ledger: MemoryScaleIntentLedger) -> Any:
    return ScaleIntentReconciler(
        ledger, actuator, policy=AllowScalePolicy(), clock=lambda: NOW
    )


@pytest.mark.spec("ARBORA-FLEET-R024.2")
def test_operations_are_registered_with_a_declared_replay_owner() -> None:
    registry = get_registry()
    run = registry["fleet.reconcile.run"]
    assert run.effect is not Effect.READ and run.idempotency is Idempotency.KEY_REQUIRED
    assert registry["fleet.scale.evaluate"].effect is Effect.READ
    assert (
        registry["fleet.deploy_watch.dispatch"].idempotency is Idempotency.KEY_REQUIRED
    )
    owners = _declared_owners()
    assert owners["fleet.reconcile.run"] and owners["fleet.deploy_watch.dispatch"]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.2")
@pytest.mark.spec("ARBORA-FLEET-R024")
async def test_reconcile_scale_and_watch_served() -> None:
    actuator, ledger = _Actuator(), MemoryScaleIntentLedger()
    context = _context(
        OPERATOR,
        scale_reconciler=_reconciler(actuator, ledger),
        scale_authority=SimpleNamespace(snapshot=_snapshot),
    )
    op = _op(fleet_scaling, "fleet.reconcile.run")
    params = {"intent": INTENT, "controller_id": "writer-1"}
    first = await fleet_scaling.handle_fleet_reconcile_run(context, params, op)
    again = await fleet_scaling.handle_fleet_reconcile_run(context, params, op)
    assert first["value"]["scaled"] is True and again["value"]["replayed"] is True
    assert actuator.calls == 1
    evaluated = await fleet_scaling.handle_fleet_scale_evaluate(
        context, {"intent": ScaleIntent.model_validate(PLAN_INTENT)}, op
    )
    assert evaluated["admitted"] is True
    too_big = {**PLAN_INTENT, "desired_replicas": 9}
    refused = await fleet_scaling.handle_fleet_scale_evaluate(
        context, {"intent": too_big}, op
    )
    assert refused["admitted"] is False and "min/max" in refused["detail"]
    rolled: list[tuple[str, str]] = []

    async def rollback(context: Any, service: str, detail: str) -> None:
        rolled.append((service, detail))

    watch_context = _context(OPERATOR, deploy_rollback=rollback)
    dispatch = _op(fleet_deploy_watch, "fleet.deploy_watch.dispatch")
    down = {"service": "api", "probes": ({"status": "down", "detail": "503"},)}
    failed = await fleet_deploy_watch.handle_fleet_deploy_watch_dispatch(
        watch_context, down, dispatch
    )
    assert failed["outcome"] == "failed" and failed["dispatched"] is True
    assert rolled == [("api", "503")]
    for probes in ((), ({"status": "up", "detail": "ok"},)):
        quiet = await fleet_deploy_watch.handle_fleet_deploy_watch_dispatch(
            watch_context, {"service": "api", "probes": probes}, dispatch
        )
        assert quiet["dispatched"] is False
    assert len(rolled) == 1


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.2")
async def test_unauthorized_caller_and_stale_lease_refuse() -> None:
    ledger = MemoryScaleIntentLedger()
    op = _op(fleet_scaling, "fleet.reconcile.run")
    params = {"intent": INTENT, "controller_id": "writer-1"}
    with pytest.raises(OperationRefused) as scope:
        await fleet_scaling.handle_fleet_reconcile_run(
            _context(OUTSIDER, scale_reconciler=_reconciler(_Actuator(), ledger)),
            params,
            op,
        )
    assert scope.value.code == "FLEET_SCOPE_REQUIRED"
    with pytest.raises(OperationRefused) as unbound:
        await fleet_scaling.handle_fleet_reconcile_run(_context(OPERATOR), params, op)
    assert unbound.value.code == "UNAVAILABLE"
    actuator = _Actuator()
    holder = _reconciler(actuator, ledger)
    await fleet_scaling.handle_fleet_reconcile_run(
        _context(OPERATOR, scale_reconciler=holder), params, op
    )
    late = ScaleIntentReconciler(
        ledger,
        actuator,
        policy=AllowScalePolicy(),
        clock=lambda: NOW + timedelta(hours=1),
    )
    stale = {"intent": INTENT, "controller_id": "writer-1"}
    with pytest.raises(OperationRefused) as fenced:
        await fleet_scaling.handle_fleet_reconcile_run(
            _context(OPERATOR, scale_reconciler=late), stale, op
        )
    assert fenced.value.code == "FLEET_LEASE_FENCED"
