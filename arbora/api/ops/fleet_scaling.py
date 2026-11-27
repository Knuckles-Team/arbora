"""Reconcile and scale-evaluate operations (ARBORA-FLEET-R024.2).

``fleet.reconcile.run`` drives one scale intent through the durable
``ScaleIntentReconciler``; ``fleet.scale.evaluate`` validates a proposed intent against
the scaling authority. Both run behind the registry chokepoint, so caller verification,
the policy filter and audit apply. The rules move unchanged: this module adds no
threshold. The scheduled tick calls ``fleet.reconcile.run`` through ``invoke`` as the
service actor.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import bound_service, build_write_op
from arbora.api.registry import Composite, Effect, OpSpec, Verb
from arbora.fleet.scaling.fleet_actuator import bind_request
from arbora.fleet.scaling.scaling_authority import ScaleIntent, validate_scale_intent
from arbora.fleet.scaling.scaling_intent_reconciler import (
    ScaleIntentConflict,
    ScaleIntentRecord,
    ScaleLeaseLost,
    ScaleLeaseUnavailable,
)

#: The replay owner of the write: the durable ledger keys an execution by its execution key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "fleet.reconcile.run": (
        "arbora.fleet.scaling.scaling_intent_reconciler:ScaleIntentLedger"
    ),
}

SCOPE = "mcp:delegate"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FleetReconcileRunParams(_Params):
    intent: ScaleIntentRecord
    controller_id: str = Field(min_length=1, max_length=256)
    dry_run: bool = False


class FleetReconcileRunResult(BaseModel):
    value: dict[str, Any]


class FleetScaleEvaluateParams(_Params):
    intent: ScaleIntent


class FleetScaleEvaluateResult(BaseModel):
    admitted: bool
    detail: str


def _require_scope(context: Any) -> None:
    scopes = getattr(context.caller, "effective_scopes", None) or ()
    if SCOPE not in scopes:
        raise OperationRefused("FLEET_SCOPE_REQUIRED", {"scope": SCOPE})


def _model(value: Any, model: type[BaseModel]) -> Any:
    return value if isinstance(value, model) else model.model_validate(value)


async def handle_fleet_reconcile_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Reconcile one durable scale intent; a stale lease refuses ``FLEET_LEASE_FENCED``."""
    del op
    _require_scope(context)
    reconciler = bound_service(
        context, "scale_reconciler", reason="the scale reconciler is not composed"
    )
    intent = _model(params["intent"], ScaleIntentRecord)
    try:
        with bind_request(context):
            result = await reconciler.reconcile(
                intent,
                controller_id=params["controller_id"],
                dry_run=bool(params.get("dry_run", False)),
            )
    except (ScaleLeaseUnavailable, ScaleLeaseLost, ScaleIntentConflict) as exc:
        raise OperationRefused(
            "FLEET_LEASE_FENCED", {"reason": type(exc).__name__}
        ) from None
    return {"value": result.model_dump(mode="json")}


async def handle_fleet_scale_evaluate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Validate a proposed intent against the current scaling authority revision."""
    del op
    _require_scope(context)
    source = bound_service(
        context, "scale_authority", reason="the scaling authority is not composed"
    )
    intent = _model(params["intent"], ScaleIntent)
    authority = await source.snapshot(context)
    try:
        validate_scale_intent(intent, authority)
    except ValueError as exc:
        return {"admitted": False, "detail": str(exc)[:512]}
    return {"admitted": True, "detail": "intent admitted by the scaling authority"}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="fleet.reconcile.run",
            summary="Reconcile one durable scale intent through the fenced reconciler",
            examples=("reconcile this scale intent",),
            params=FleetReconcileRunParams,
            result=FleetReconcileRunResult,
            handler="arbora.api.ops.fleet_scaling.handle_fleet_reconcile_run",
            scope=SCOPE,
        ),
        OpSpec(
            id="fleet.scale.evaluate",
            verb=Verb.ASK,
            summary="Evaluate a proposed scale intent against the scaling authority",
            examples=("would this scale intent be admitted",),
            params=FleetScaleEvaluateParams,
            result=FleetScaleEvaluateResult,
            binding=Composite(
                handler="arbora.api.ops.fleet_scaling.handle_fleet_scale_evaluate"
            ),
            scopes=frozenset({SCOPE}),
            effect=Effect.READ,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "FleetReconcileRunParams",
    "FleetScaleEvaluateParams",
    "handle_fleet_reconcile_run",
    "handle_fleet_scale_evaluate",
    "operations",
    "specs",
]
