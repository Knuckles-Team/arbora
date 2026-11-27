"""Process operations: plan from the engine, list and decide approvals (ARBORA-OPS-R898).

``process.plan`` asks the engine ``Decide`` ``ProcessPlan`` question over one stored
workflow version and returns the sealed plan, its digest and the bindings unchanged:
arbora adds no binding and no ranking. ``process.approvals.list`` and
``process.approval.decide`` read and decide the approval waits of the run journal through
the :class:`arbora.processes.approvals.ApprovalBroker`. ``process.run`` plans, resolves the
bound prompts, binds the decision port and the approval waiter, and runs the sealed plan
through the meristem run loop (MERISTEM-RUN-R018, ARBORA-OPS-R898.5). Against a pinned
meristem without the control step kinds and ports it refuses ``UNAVAILABLE`` naming them.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, _build_op, build_read_op
from arbora.api.ops.engine_calls import engine_send, unavailable
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
RUN_SCOPE = "process:run"
#: The replay owner of ``process.run``: the run id derives from the verified tenant and the
#: idempotency key, and the engine journal admits one run entry per run id.
NATIVE_IDEMPOTENCY: dict[str, str] = {"process.run": "process.journal:run-by-key"}
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


class ProcessRunResult(Params):
    run_id: str
    plan_digest: str
    status: str
    output: Any = None
    error: Any = None
    tasks: dict[str, Any] = Field(default_factory=dict)


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
    from arbora.api.ops.pin_guards import refusal
    from arbora.api.ops.plans import _feature_schema
    from arbora.deployment.placement import (
        check_receipt,
        decide_params,
        single_record,
    )
    from arbora.pin_lag.medulla import PinLagUnavailable, require_question_kind

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
        raise OperationRefused("APPROVAL_NOT_PENDING", {}) from None
    except ApproverRoleRequired:
        raise OperationRefused("APPROVER_NOT_IN_ROLE", {}) from None
    return {
        "run_id": decided["run_id"],
        "step_id": decided["step_id"],
        "state": decided["state"],
        "verdict": decided["verdict"],
        "approver": decided["approver"],
    }


def _run_id(context: Any) -> str:
    """The run id of one request: stable per tenant and idempotency key, so a replay finds it."""
    seed = f"{_tenant(context)}\0{context.idempotency_key}".encode()
    return "process:" + hashlib.sha256(seed).hexdigest()[:32]


def _prompt_reader(context: Any) -> Any:
    """``prompt IRI -> text`` read through the engine as the verified caller."""
    from arbora.agent_host.port_services import BODY_PROPERTIES

    async def read(iri: str) -> str | None:
        try:
            answer = await engine_send(context, "GetNodeProperties", {"node_id": iri})
        except EngineRefusal:
            return None
        found = answer.get("properties", answer) if isinstance(answer, Mapping) else {}
        for name in BODY_PROPERTIES:
            value = found.get(name) if isinstance(found, Mapping) else None
            if isinstance(value, str) and value:
                return value
        return None

    return read


def _decide_call(context: Any) -> Any:
    """The ``RunCall`` of the control-step ``Decide``: the engine answer, checked and wrapped."""
    from arbora.deployment.placement import check_receipt, single_record

    async def call(
        op_id: str, params: Mapping[str, Any], run_ref: str, plan_ref: str | None = None
    ) -> dict[str, Any]:
        try:
            batch = await engine_send(context, "Decide", dict(params))
            record = single_record(batch)
            check_receipt(record, params["request"], str(context.caller.principal))
        except EngineRefusal as refused:
            return {"outcome": "refused", "refusal_code": refused.code}
        return {"outcome": "answered", "payload": {"records": [record]}}

    return call


def _run_ports(context: Any, evidence: Any, graph: str) -> Any:
    from arbora.api.ops.pin_guards import refusal
    from arbora.pin_lag.medulla import PinLagUnavailable
    from arbora.processes.run_ports import process_host_ports

    clock = context.services.get("now_ms")
    wait = context.services.get("process_wait") or asyncio.sleep
    schema = context.services.get("process_question_schema")
    try:
        return process_host_ports(
            call=_decide_call(context),
            tenant=_tenant(context),
            graph=graph,
            schema_of=schema if callable(schema) else _published_schema(context),
            broker=broker_for(context),
            wait=wait,
            now_ms=clock or (lambda: int(time.time() * 1000)),
            evidence=evidence,
        )
    except PinLagUnavailable as lag:
        raise refusal(lag) from lag


def _published_schema(context: Any) -> Any:
    """``(tenant, question id) -> the pin published for it`` (ARBORA-OPS-R898.5), read-only."""
    from arbora.api.task_plan_schema import rank_pin

    def of(tenant: str, question_id: str) -> Mapping[str, Any] | None:
        return rank_pin(
            context.services.get("catalog_rank_schema_pins"), tenant, question_id
        )

    return of


async def _begin_run(
    context: Any, planned: Mapping[str, Any], params: Mapping[str, Any]
) -> str:
    """Journal the run once; a replay of the same key never starts a second run."""
    broker = broker_for(context)
    tenant, run_id = _tenant(context), _run_id(context)
    evidence = await broker.evidence(tenant, run_id)
    if evidence["run"] is not None:
        done = evidence["settled"]
        raise _Replayed(run_id, str(planned["plan_digest"]), done)
    names = (b.get("id") if isinstance(b, Mapping) else b for b in planned["bindings"])
    sources = {str(n).removeprefix("binding:"): "engine" for n in names}
    await broker.journal_run(
        tenant,
        run_id=run_id,
        plan_digest=str(planned["plan_digest"]),
        workflow_id=params["workflow_id"],
        version=params["version"],
        binding_sources=sources,
    )
    return run_id


class _Replayed(Exception):
    """The key already started this run; ``settled`` is its settled entry, or ``None``."""

    def __init__(
        self, run_id: str, digest: str, settled: Mapping[str, Any] | None
    ) -> None:
        super().__init__(run_id)
        self.run_id, self.digest, self.settled = run_id, digest, settled


async def handle_process_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Plan, bind the ports and run one stored workflow as one governed agent DAG."""
    from arbora.api.ops.plans import _plan_model, execute_task_plan
    from arbora.processes.run_plan import ProcessPromptUnresolved, resolve_prompts
    from arbora.processes.run_ports import RunEvidence

    planned = await handle_process_plan(context, params, op)
    plan = planned["plan"]
    try:
        prompts = await resolve_prompts(_prompt_reader(context), plan)
    except ProcessPromptUnresolved as unresolved:
        raise OperationRefused(
            "PROCESS_PROMPT_UNRESOLVED", {"task_id": unresolved.task_id}
        ) from None
    evidence = RunEvidence()
    ports = _run_ports(context, evidence, await caller_dispatch_graph(context))
    try:
        run_id = await _begin_run(context, planned, params)
    except _Replayed as replay:
        status = str((replay.settled or {}).get("status") or "")
        if not status:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "this process run already started"}
            ) from None
        return {
            "run_id": replay.run_id,
            "plan_digest": replay.digest,
            "status": status,
            "tasks": {},
        }
    run = await execute_task_plan(
        plan, _plan_model(context), run_id=run_id, ports=ports, prompts=prompts
    )
    status = str(run.get("status") or "failed")
    await broker_for(context).settle_run(
        _tenant(context),
        run_id=run_id,
        status=status,
        branch_verdicts=evidence.branch_verdicts,
        decision_records=evidence.decision_records,
    )
    return {
        "run_id": run_id,
        "plan_digest": planned["plan_digest"],
        "status": status,
        "output": run.get("output"),
        "error": run.get("error"),
        "tasks": run.get("tasks") or {},
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
        _build_op(
            op_id="process.run",
            summary="Run one stored process workflow as a governed agent run",
            examples=("run this process workflow",),
            params=ProcessRunParams,
            result=ProcessRunResult,
            handler=f"{module}.handle_process_run",
            scope=RUN_SCOPE,
            verb=Verb.ACT,
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
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
    "NATIVE_IDEMPOTENCY",
    "RUN_SCOPE",
    "READ_SCOPE",
    "broker_for",
    "handle_approval_decide",
    "handle_approvals_list",
    "handle_process_plan",
    "handle_process_run",
    "operations",
    "specs",
]
