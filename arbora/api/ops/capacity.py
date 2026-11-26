"""Hosted capacity-throttle status and mode operations.

ARBORA-CAPACITY-R002 (status/mode half): projects the in-process
:class:`~arbora.fleet.throttle_service.ThrottleRegistry` through the same
operation registry, identity and error-handling chokepoint every other
Arbora operation uses (see :mod:`arbora.api.ops.browser` for the sibling
pattern). ``capacity.throttle.status`` declares only ``capacity:read``;
``capacity.throttle.set_mode`` declares ``capacity:admin`` and
``Effect.ADMIN`` -- the shared invoke pipeline enforces the declared scope
and the resulting console step-up before either handler runs, so a reader
can never flip enforcement through this operation.

Updating the engine's own ``CapacityCell`` ceiling is not implemented here:
the engine's run-time method catalog does not yet publish a capacity
method, and ``specs/adaptive-capacity/plan.md`` requires that
exact method and schema to be pinned before this feature advertises it.
Until then, a caller sees the Arbora-owned throttle state only; this
operation never fabricates an engine ceiling.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)
from arbora.fleet.error_budget import Partition, ThrottleMode


class CapacityPartitionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child: str
    operation_class: str


class CapacitySetModeParams(CapacityPartitionParams):
    mode: ThrottleMode


class CapacityStatusResult(BaseModel):
    mode: str
    current_limit: int | None
    reason: str


class CapacityModeResult(BaseModel):
    mode: str


class CapacityReceiptResult(BaseModel):
    """The latest applied-state receipt of a partition, or none yet."""

    receipt: dict[str, Any] | None


def _partition(context: Any, params: Mapping[str, Any]) -> Partition:
    return Partition(
        tenant=context.caller.tenant,
        child=params["child"],
        operation_class=params["operation_class"],
        policy_revision=context.caller.policy_revision,
    )


def _bound_registry(context: Any) -> Any:
    registry = context.services.get("throttle_registry")
    if registry is None:
        raise RuntimeError("capacity throttle controller is not bound")
    return registry


async def handle_capacity_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Report the partition's recorded throttle state, never a fabricated one."""
    status = _bound_registry(context).status(_partition(context, params))
    return {
        "value": {
            "mode": status.mode.value,
            "current_limit": status.current_limit,
            "reason": status.reason,
        }
    }


async def handle_capacity_receipt_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The latest receipt of this caller's partition (ARBORA-CAPACITY-R001.3.4)."""
    from dataclasses import asdict

    receipt = _bound_registry(context).latest_receipt(_partition(context, params))
    return {"value": {"receipt": None if receipt is None else asdict(receipt)}}


async def handle_capacity_set_mode(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Select observe or enforce mode; the invoke pipeline already verified authority."""
    registry = _bound_registry(context)
    partition = _partition(context, params)
    registry.set_mode(partition, ThrottleMode(params["mode"]))
    return {"value": {"mode": registry.mode(partition).value}}


class CapacityAdjustParams(BaseModel):
    """The engine's ``CapacityAdjustmentProposal`` as the operator confirmed it."""

    model_config = ConfigDict(extra="forbid")
    proposal: dict[str, Any]


class CapacityAdjustResult(BaseModel):
    proposal_digest: str
    verdict: str
    observed_capacity: int
    headroom: int
    idempotency_key: str


async def _read_back(context: Any, cell_id: str) -> dict[str, int]:
    """The cell's capacity and headroom as the engine reports them after the step."""
    from arbora.api.ops._engine import engine_send

    status = await engine_send(
        context,
        "CapacityStatus",
        {
            "request": {
                "cell_id": cell_id,
                "max_count": 64,
                "schema_version": 1,
                "tenant_ref": str(context.caller.tenant),
            }
        },
    )
    cells = status.get("cells") if isinstance(status, Mapping) else None
    if not cells:
        from arbora.api.ops._engine import unavailable

        raise unavailable("the engine reports no capacity cell for the resource")
    held = sum(
        int(lease["amount"])
        for lease in status.get("leases") or ()
        if str(lease.get("state")) not in {"released", "expired", "reclaimed"}
    )
    capacity = int(cells[0]["capacity"])
    return {"capacity": capacity, "headroom": max(capacity - held, 0)}


async def handle_capacity_adjust_apply(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Actuate the confirmed adjustment through the engine verdict (R017.17)."""
    from medulla import EngineResponseError
    from medulla.decision_client import DecisionClient

    from arbora.api.errors import EngineRefusal
    from arbora.api.invoke.bootstrap import caller_dispatch_graph
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.invoke.steps import claim_values
    from arbora.api.ops._engine import refusal_code
    from arbora.api.ops.deploy import deploy_config_of, feature_schema_of
    from arbora.deployment.assembly import require_confirmed_plan
    from arbora.deployment.capacity_actuation import actuate
    from arbora.security.anonymous import client_scope

    require_confirmed_plan(op)
    tool = ((deploy_config_of(context).get("tools") or {}).get("capacity_scale")) or {}
    invoke = context.services.get("plan_step_invoke")
    if invoke is None or not (tool.get("server") and tool.get("tool")):
        raise OperationRefused(
            "AUTHORITY_UNAVAILABLE",
            {"reason": "the scale tool or the invoke pipeline is not bound"},
        )
    tenant = str(context.caller.tenant)
    graph = await caller_dispatch_graph(context)

    async def decide(request: dict[str, Any]) -> Any:
        with client_scope(context.client, claim_values(context.caller.engine_claims)):
            return await DecisionClient(context.client, graph).decide(request)

    async def read_back(cell_id: str) -> Any:
        return await _read_back(context, cell_id)

    try:
        return await actuate(
            decide,
            invoke,
            context.caller,
            tenant=tenant,
            principal=str(context.caller.principal),
            graph=graph,
            feature_schema=feature_schema_of(context, tenant),
            proposal=params["proposal"],
            scale_tool=tool,
            read_back=read_back,
        )
    except EngineResponseError as exc:
        raise EngineRefusal(refusal_code(exc.code, exc.detail)) from exc


#: The engine replays the verdict question by its record identity, and the
#: scale step is one ``fleet.call`` reserved under ``capacity:<proposal_digest>``.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "capacity.adjust.apply": "arbora.mcp_server.effect_journal:EgEffectJournal",
}


def operations() -> tuple[OpSpec, ...]:
    from arbora.api.ops._common import build_write_op

    return (
        build_write_op(
            op_id="capacity.adjust.apply",
            summary="Actuate a capacity adjustment the engine allowed",
            examples=("apply this capacity adjustment",),
            params=CapacityAdjustParams,
            result=CapacityAdjustResult,
            handler="arbora.api.ops.capacity.handle_capacity_adjust_apply",
            scope="fleet:control",
            confirm=Confirm.PLAN,
        ),
        OpSpec(
            id="capacity.throttle.status",
            verb=Verb.ASK,
            summary="Show this partition's current throttle mode and limit",
            examples=("show capacity throttle status for this child",),
            params=CapacityPartitionParams,
            result=CapacityStatusResult,
            binding=Composite(handler="arbora.api.ops.capacity.handle_capacity_status"),
            scopes=frozenset({"capacity:read"}),
            effect=Effect.READ,
            surfaces=frozenset(
                {Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE}
            ),
        ),
        OpSpec(
            id="capacity.throttle.receipt.get",
            verb=Verb.ASK,
            summary="Show the latest applied capacity receipt for this partition",
            examples=("show the last capacity change receipt for this child",),
            params=CapacityPartitionParams,
            result=CapacityReceiptResult,
            binding=Composite(
                handler="arbora.api.ops.capacity.handle_capacity_receipt_get"
            ),
            scopes=frozenset({"capacity:read"}),
            effect=Effect.READ,
            surfaces=frozenset(
                {Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE}
            ),
        ),
        OpSpec(
            id="capacity.throttle.set_mode",
            verb=Verb.MANAGE,
            summary="Select observe or enforce throttle mode for this partition",
            examples=("switch this child to enforce mode",),
            params=CapacitySetModeParams,
            result=CapacityModeResult,
            binding=Composite(
                handler="arbora.api.ops.capacity.handle_capacity_set_mode"
            ),
            scopes=frozenset({"capacity:admin"}),
            effect=Effect.ADMIN,
            confirm=Confirm.CONSOLE,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
            audit=AuditClass.EVENT,
            surfaces=frozenset({Surface.MCP, Surface.HTTP, Surface.CONSOLE}),
        ),
    )


specs = operations

__all__ = [
    "CapacityAdjustParams",
    "CapacityAdjustResult",
    "CapacityModeResult",
    "CapacityPartitionParams",
    "CapacityReceiptResult",
    "CapacitySetModeParams",
    "CapacityStatusResult",
    "handle_capacity_adjust_apply",
    "handle_capacity_receipt_get",
    "handle_capacity_set_mode",
    "handle_capacity_status",
    "operations",
    "specs",
]
