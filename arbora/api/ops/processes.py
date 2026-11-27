"""Process operations: plan from the engine, list and decide approvals (ARBORA-OPS-R898).

``process.plan`` asks the engine ``Decide`` ``ProcessPlan`` question over one stored
workflow version and returns the sealed plan, its digest and the bindings unchanged:
arbora adds no binding and no ranking. ``process.approvals.list`` and
``process.approval.decide`` read and decide the approval waits of the run journal through
the :class:`arbora.processes.approvals.ApprovalBroker`. ``process.run`` waits for the
meristem step kinds and the ``ApprovalWaiter`` port (MERISTEM-RUN-R018) and is not served.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, _build_op, build_read_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import (
    AuditClass,
    Confirm,
    Effect,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Verb,
)
from arbora.processes.approvals import (
    ApprovalBroker,
    ApprovalNotPending,
    ApproverRoleRequired,
)

READ_SCOPE = "process:read"
APPROVE_SCOPE = "process:approve"
QUESTION_ID = "arbora.process.plan"
_ID = Field(min_length=1, max_length=256)


class ProcessPlanParams(Params):
    workflow_id: str = _ID
    version: int = Field(ge=0)


class ProcessPlanResult(Params):
    plan: dict[str, Any]
    plan_digest: str
    bindings: list[Any]
    decision_id: str


class ProcessRunParams(Params):
    workflow_id: str = _ID
    version: int = Field(ge=0)
    inputs: dict[str, Any] = Field(default_factory=dict)


class ProcessApprovalListParams(Params):
    run_id: str = _ID


class ProcessApprovalListResult(Params):
    items: list[dict[str, Any]]


class ProcessApprovalDecideParams(Params):
    run_id: str = _ID
    step_id: str = _ID
    verdict: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=2000)


class ProcessApprovalDecideResult(Params):
    run_id: str
    step_id: str
    state: str
    verdict: str
    approver: str


def _tenant(context: Any) -> str:
    return str(context.caller.tenant)


def broker_for(context: Any) -> ApprovalBroker:
    """The broker over the engine journal, as the verified caller."""

    async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
        return await engine_send(context, method, params, idempotent=key is not None)

    return ApprovalBroker(send)


async def handle_process_plan(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's sealed plan of one workflow version, its digest and its bindings."""
    from arbora.api.ops.plans import _feature_schema
    from arbora.deployment.placement import (
        check_receipt,
        decide_params,
        single_record,
    )
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_question_kind

    try:
        require_question_kind("process_plan")
        from medulla import decision_client

        process_plan_request = getattr(decision_client, "process_plan_request", None)
        if process_plan_request is None:
            raise PinLagUnavailable("decision_client.process_plan_request")
    except PinLagUnavailable as lag:
        raise refusal(lag) from lag
    request = process_plan_request(
        _tenant(context),
        QUESTION_ID,
        await _feature_schema(context, _tenant(context)),
        graph=await caller_dispatch_graph(context),
        workflow_id=params["workflow_id"],
        version=params["version"],
    )
    record = single_record(await engine_send(context, "Decide", decide_params(request)))
    check_receipt(record, request, str(context.caller.principal))
    answer = record.get("answer")
    plan = answer.get("value") if isinstance(answer, Mapping) else None
    if not isinstance(plan, Mapping) or not plan.get("plan_digest"):
        raise unavailable("engine result malformed")
    premises = record.get("premises") or ()
    return {
        "plan": dict(plan),
        "plan_digest": str(plan["plan_digest"]),
        "bindings": [p for p in premises if _is_binding(p)],
        "decision_id": str(record.get("record_id", "")),
    }


def _is_binding(premise: Any) -> bool:
    name = premise.get("id") if isinstance(premise, Mapping) else premise
    return isinstance(name, str) and name.startswith("binding:")


async def handle_approvals_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Pending and decided approvals of one run; another tenant's run lists nothing."""
    rows = await broker_for(context).records(_tenant(context), params["run_id"])
    return {"items": [r for r in rows if r.get("kind") == "approval"]}


def _groups(context: Any) -> tuple[str, ...]:
    claims = getattr(context.caller, "engine_claims", None) or {}
    groups = claims.get("groups") if isinstance(claims, Mapping) else None
    return tuple(str(g) for g in groups or ())


async def handle_approval_decide(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Decide one pending approval once, as the verified person holding the approver role."""
    try:
        decided = await broker_for(context).resolve(
            _tenant(context),
            run_id=params["run_id"],
            step_id=params["step_id"],
            verdict=params["verdict"],
            comment=params.get("comment", ""),
            principal=str(context.caller.principal),
            groups=_groups(context),
            decided_ms=int(time.time() * 1000),
        )
    except ApprovalNotPending:
        raise OperationRefused("NOT_FOUND", {}) from None
    except ApproverRoleRequired:
        raise OperationRefused(
            "POLICY_DENIED", {"reason": "approver_role_required"}
        ) from None
    return {
        "run_id": decided["run_id"],
        "step_id": decided["step_id"],
        "state": decided["state"],
        "verdict": decided["verdict"],
        "approver": decided["approver"],
    }


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.processes"
    return (
        build_read_op(
            op_id="process.plan",
            summary="Read the engine's sealed plan, digest and bindings of one workflow version",
            examples=("plan this process workflow",),
            params=ProcessPlanParams,
            result=ProcessPlanResult,
            handler=f"{module}.handle_process_plan",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="process.approvals.list",
            summary="List the pending and decided approvals of one process run",
            examples=("which approvals are pending for this run",),
            params=ProcessApprovalListParams,
            result=ProcessApprovalListResult,
            handler=f"{module}.handle_approvals_list",
            scope=READ_SCOPE,
            verb=Verb.FIND,
        ),
        _build_op(
            op_id="process.approval.decide",
            summary="Approve or reject one pending approval step of a process run",
            examples=("approve this process step",),
            params=ProcessApprovalDecideParams,
            result=ProcessApprovalDecideResult,
            handler=f"{module}.handle_approval_decide",
            scope=APPROVE_SCOPE,
            verb=Verb.ACT,
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.IDENTITY_CHAIN,
            principals=PrincipalRule.HUMAN,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "APPROVE_SCOPE",
    "READ_SCOPE",
    "broker_for",
    "handle_approval_decide",
    "handle_approvals_list",
    "handle_process_plan",
    "operations",
    "specs",
]
