"""``agents.plan_run``: run an engine-decided task DAG (ARBORA-RELEASE-R003.4.1).

Arbora admits, serves and executes a task plan. It never plans: the plan comes
from the engine's ``Decide``/``TaskPlan`` decision (EG-CQ-ORCH-R001,
EG-DECISION-ENGINE-R134.111). One served call does this:

1. **Decide.** The caller's explicit tasks, dependencies, output task and
   limits become one engine ``TaskPlan`` request. The engine validates the
   graph and returns a canonical, digest-bound plan, or refuses it
   (``plan_cycle``, ``missing_dependency``, ``plan_bounds`` and the rest).
   An engine refusal propagates unchanged.
2. **Verify.** The engine receipt is checked against the request and the
   verified caller principal before anything runs.
3. **Attach.** When the run is bound to a work item, the plan is published to
   that work item's current plan (``PublishCurrentPlan``), so replay and
   recovery read the same plan digest (ARBORA-RELEASE-R003.4.2).
4. **Execute.** The plan maps one to one onto a harness ``RunPlan`` with a
   ``DependencyPlan``. The harness runs it in dependency order with at most
   ``max_concurrency`` active tasks, inside the caller's run authority scope,
   so every child task inherits the caller-credential renewal.

Arbora does not add, drop, reorder or re-validate tasks.

A task is an agent step, so it can call a governed fleet tool only when the
caller names the tool in ``fleet_tools`` (ARBORA-RELEASE-R003.4.5). The step's
tool is ``fleet_call``. It runs with the caller's own authority and refuses
any tool the caller did not name. Dependency outputs reach a dependent task as
data in its prompt (the harness appends them), and every fleet call, with its
arguments and result, is recorded in ``run.fleet_calls``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field
from pydantic_ai import RunContext

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.api.ops._common import Params
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)

#: The engine release that serves ``TaskPlan``. Arbora's floor moves to it once
#: it is on PyPI (ARBORA-RELEASE-R003.4.3).
TASK_PLAN_ENGINE_RELEASE = "epistemic-graph>=2.27.5"
TASK_PLAN_QUESTION_ID = "arbora.agents.plan_run"
#: How long a published plan snapshot stays current.
PLAN_SNAPSHOT_TTL_MS = 3_600_000


class WorkItemLease(Params):
    """The work-item lease a plan is attached to. The engine checks the fence."""

    run_id: str = Field(min_length=1, max_length=512)
    worker_id: str = Field(min_length=1, max_length=512)
    lease_epoch: int = Field(ge=0)
    fencing_token: int = Field(ge=0)
    revision: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class _NativePlanAdmission:
    """The engine-owned identity attached to one served plan run.

    ``item_id`` is deliberately kept separate from ``run_id``.  Native EG
    chooses the former; the latter is the stable RunEvent/current-plan identity
    derived from the verified engine plan.  A terminal replay has no claim and
    therefore cannot be executed a second time.
    """

    item_id: str
    run_id: str
    lease: WorkItemLease | None
    claim: Mapping[str, Any] | None
    terminal_status: str | None = None


class FleetToolRef(Params):
    """One fleet tool a plan's agent steps may call."""

    server: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)


class PlanRunParams(Params):
    """Explicit caller claims. The engine owns graph validity and bounds."""

    plan: dict[str, Any] = Field(
        description=(
            "TaskPlanProposal: tasks [{task_id, prompt, depends_on}], "
            "output_task_id, max_concurrency, max_steps."
        )
    )
    work_item: WorkItemLease | None = None
    fleet_tools: list[FleetToolRef] = Field(
        default_factory=list,
        max_length=16,
        description=(
            "Fleet tools the plan's agent steps may call through fleet_call, "
            "under the caller's authority. Empty means no step has a tool."
        ),
    )


class PlanRunResult(Params):
    value: dict[str, Any]


@dataclass(frozen=True)
class TaskPlanEngine:
    """The engine's task-plan surface, bound to one authenticated client."""

    build_request: Callable[..., dict[str, Any]]
    decide: Callable[[dict[str, Any]], Awaitable[Any]]
    verify: Callable[..., dict[str, Any]]
    publish: Callable[[dict[str, Any], str], Awaitable[Any]]


def engine_task_plan(client: Any, graph: str | None = None) -> TaskPlanEngine:
    """Bind the engine facade's task-plan helpers to ``client``.

    Refuses with ``UNAVAILABLE`` when the installed engine predates TaskPlan.
    """
    import importlib

    try:
        # Resolved by name: the pinned engine floor predates TaskPlan until
        # ARBORA-RELEASE-R003.4.3 raises it, and this is the refusal point.
        facade = importlib.import_module("epistemic_graph.decision_client")
        senders = importlib.import_module("epistemic_graph.generated.coordination")
        DecisionClient = facade.DecisionClient
        task_plan_request = facade.task_plan_request
        verify_task_plan_receipt = facade.verify_task_plan_receipt
        send_publish_current_plan = senders.send_publish_current_plan
    except (ImportError, AttributeError) as exc:
        raise OperationRefused(
            "UNAVAILABLE",
            {
                "reason": "the engine does not serve TaskPlan decisions",
                "requires": TASK_PLAN_ENGINE_RELEASE,
                "spec": "EG-CQ-ORCH-R001",
            },
        ) from exc
    decisions = DecisionClient(client, graph)

    async def publish(request: dict[str, Any], idempotency_key: str) -> Any:
        return await send_publish_current_plan(
            client, {"request": request}, graph, idempotency_key=idempotency_key
        )

    return TaskPlanEngine(
        build_request=task_plan_request,
        decide=decisions.decide,
        verify=verify_task_plan_receipt,
        publish=publish,
    )


def _single_record(batch: Any) -> Mapping[str, Any]:
    records = batch.get("records") if isinstance(batch, Mapping) else None
    if not isinstance(records, list) or len(records) != 1:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "expected exactly one decision record"}
        )
    record = records[0]
    if not isinstance(record, Mapping):
        raise OperationRefused("RECEIPT_MISMATCH", {"reason": "malformed record"})
    return record


async def decide_task_plan(
    engine: TaskPlanEngine,
    proposal: Mapping[str, Any],
    *,
    tenant: str,
    principal: str,
    feature_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Ask the engine for the plan and verify its receipt; return plan + record.

    Engine refusals propagate unchanged. A proposal the engine facade rejects
    locally is ``INVALID_ARGUMENT`` with the facade's message.
    """
    try:
        request = engine.build_request(
            tenant, TASK_PLAN_QUESTION_ID, dict(proposal), dict(feature_schema)
        )
    except ValueError as exc:
        raise OperationRefused("INVALID_ARGUMENT", {"message": str(exc)}) from exc
    record = _single_record(await engine.decide(request))
    try:
        plan = engine.verify(record, request, expected_principal=principal)
    except ValueError as exc:
        raise OperationRefused("RECEIPT_MISMATCH", {"message": str(exc)}) from exc
    return dict(plan), record


def publish_request(
    tenant: str,
    plan: Mapping[str, Any],
    proposal: Mapping[str, Any],
    lease: WorkItemLease,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """The ``PublishCurrentPlan`` request that binds ``plan`` to the work item."""
    return {
        "tenant": tenant,
        "snapshot": {
            "run_id": lease.run_id,
            "revision": lease.revision,
            "observed_at_ms": now_ms,
            "expires_at_ms": now_ms + PLAN_SNAPSHOT_TTL_MS,
            "items": [
                {"text": f"{task['task_id']}: {task['prompt']}", "status": "pending"}
                for task in plan["tasks"]
            ],
            "task_plan": dict(plan),
        },
        "worker_id": lease.worker_id,
        "lease_epoch": lease.lease_epoch,
        "fencing_token": lease.fencing_token,
        "idempotency_key": f"task-plan:{lease.run_id}:{plan['plan_digest']}",
        "task_plan_proposal": dict(proposal),
    }


def run_plan_for(plan: Mapping[str, Any], model: Any, toolsets: tuple = ()) -> Any:
    """The harness ``RunPlan`` for an engine plan: same tasks, edges and limits."""
    from agent_utilities.graph import (
        DependencyPlan,
        RunBudget,
        RunPlan,
        RunTask,
        StopRule,
    )

    graph = DependencyPlan(
        tasks=tuple(
            RunTask(
                task_id=task["task_id"],
                prompt=task["prompt"],
                depends_on=tuple(task["depends_on"]),
            )
            for task in plan["tasks"]
        ),
        output_task_id=plan["output_task_id"],
        max_concurrency=int(plan["max_concurrency"]),
        plan_id=plan["plan_digest"],
    )
    return RunPlan(
        model=model,
        dependency_plan=graph,
        toolsets=tuple(toolsets),
        stop=StopRule(max_steps=int(plan["max_steps"])),
        budget=RunBudget(max_concurrent_requests=int(plan["max_concurrency"])),
    )


class LateStepInvoke:
    """The served invoke pipeline, bound once the invocation services exist."""

    def __init__(self) -> None:
        self._services: Any = None

    def bind(self, services: Any) -> None:
        if self._services is not None:
            raise RuntimeError("the step invoke pipeline is already bound")
        self._services = services

    async def __call__(
        self, op_id: str, params: Mapping[str, Any], caller: Any, key: str
    ) -> Any:
        from arbora.api.invoke.pipeline import invoke
        from arbora.api.registry import Surface

        if self._services is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
            )
        return await invoke(
            op_id,
            params,
            caller,
            Surface.MCP,
            services=self._services,
            idempotency_key=key,
        )


def step_idempotency_key(record_id: str, task_id: str, index: int) -> str:
    """The key of one step call: the plan run's replay record, task and index.

    ``record_id`` is the run's replay record (:func:`replay_record`). A replay
    of the same served call makes the same calls with the same keys, so the
    effect journal returns the recorded outcome and runs no second effect.
    """
    return f"plan-step:{record_id}:{task_id}:{index}"


def replay_record(caller: Any) -> str:
    """The plan run's replay record: the pipeline-bound request identity.

    The invoke pipeline binds a governed call's ``request_id`` to the tenant,
    the principal, the operation and the caller's idempotency key, so a replay
    of the same served call carries the same value. The engine decision record
    id is not stable: each ``Decide`` stamps its own ``created_at_ms``.
    """
    request_id = getattr(caller, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the plan run has no bound replay identity"}
        )
    return f"run-{request_id[:32]}"


#: The replay owner of a served plan run (ARBORA-RELEASE-R003.4.11). A run's only
#: external effects are its step ``fleet.call`` effects; each is reserved in the
#: EG effect journal under ``plan-step:<record>:<task>:<idx>``, where
#: ``<record>`` derives from the run's own idempotency key. The engine
#: ``TaskPlan`` decision is evaluate-only, and ``PublishCurrentPlan`` is keyed by
#: the run and the plan digest. A replayed run therefore returns each recorded
#: step outcome and starts no second effect.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "agents.plan_run": "arbora.mcp_server.effect_journal:EgEffectJournal",
}


@dataclass
class FleetStepTools:
    """The governed fleet tool an agent step may call (ARBORA-RELEASE-R003.4.5).

    Each call goes through the invoke pipeline as ``fleet.call``: the same
    effect authority, audit reservation and effect journal as any other
    governed write. A tool the caller did not name, a tool that needs a plan
    confirmation or a console step-up, and a tool whose scopes the caller lacks
    are refused as data for the step, never run.
    """

    context: Any
    allowed: frozenset[tuple[str, str]]
    record_id: str = ""
    ledger: list[dict[str, Any]] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict)

    async def call(
        self, task_id: str, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from arbora.api.invoke.steps import OpError
        from arbora.api.ops.research import FleetCaller
        from arbora.api.registry import Confirm

        index = self._counts[task_id] = self._counts.get(task_id, 0) + 1
        key = step_idempotency_key(self.record_id, task_id, index)
        entry: dict[str, Any] = {
            "sequence": len(self.ledger) + 1,
            "task_id": task_id,
            "index": index,
            "idempotency_key": key,
            "server": server,
            "tool": tool,
            "arguments": arguments,
        }
        self.ledger.append(entry)
        try:
            if (server, tool) not in self.allowed:
                raise OperationRefused(
                    "SCOPE_DENIED", {"reason": "tool is not named in fleet_tools"}
                )
            step_invoke = self.context.services.get("plan_step_invoke")
            if step_invoke is None:
                raise OperationRefused(
                    "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
                )
            decision = await FleetCaller(self.context).decision(server, tool, arguments)
            if decision.confirm != Confirm.NONE:
                raise OperationRefused(
                    "CONFIRMATION_REQUIRED",
                    {"reason": "the tool needs a human-confirmed plan"},
                )
            outcome = await step_invoke(
                "fleet.call",
                {"server": server, "tool": tool, "arguments": arguments},
                self.context.caller,
                key,
            )
            if isinstance(outcome, OpError) or outcome.code != "OK":
                entry.update(
                    ok=False, error=outcome.code, details=dict(outcome.details)
                )
                return {"ok": False, "error": outcome.code}
            value = outcome.value
            result = value.get("value") if isinstance(value, Mapping) else value
            entry["result"] = json.loads(json.dumps(result, default=str))
            entry["ok"] = True
            return {"ok": True, "result": entry["result"]}
        except OperationRefused as exc:
            entry.update(ok=False, error=exc.code, details=exc.details)
        except Exception as exc:  # noqa: BLE001 - the step sees the failure as data
            entry.update(ok=False, error=type(exc).__name__)
        return {"ok": False, "error": entry["error"]}

    def toolset(self) -> Any:
        from pydantic_ai.toolsets import FunctionToolset

        async def fleet_call(
            ctx: RunContext[Any], server: str, tool: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            """Call one governed fleet tool. Returns {ok, result} or {ok, error}."""
            task_id = str(getattr(ctx.deps, "task_id", None) or "task")
            return await self.call(task_id, server, tool, arguments)

        return FunctionToolset([fleet_call])


def _fleet_step_tools(
    context: Any, refs: list[FleetToolRef], record_id: str
) -> FleetStepTools | None:
    if not refs:
        return None
    return FleetStepTools(
        context, frozenset((r.server, r.tool) for r in refs), record_id
    )


def _step_toolsets(fleet: FleetStepTools | None) -> tuple:
    """The toolsets a plan's agent steps run with: the fleet tools, or none."""
    return (fleet.toolset(),) if fleet is not None else ()


def _fleet_calls(fleet: FleetStepTools | None) -> list[dict[str, Any]]:
    """The recorded fleet calls of one plan run, in call order."""
    return list(fleet.ledger) if fleet is not None else []


async def execute_task_plan(
    plan: Mapping[str, Any],
    model: Any,
    *,
    run_id: str,
    fleet: FleetStepTools | None = None,
) -> dict[str, Any]:
    """Run the plan in the harness and collect every task's terminal result."""
    from agent_utilities.graph import RunRequest, iterate_run

    request = RunRequest(task=plan["output_task_id"], run_id=run_id)
    tasks: dict[str, dict[str, Any]] = {}
    started: list[str] = []
    terminal: dict[str, Any] = {}
    run_plan = run_plan_for(plan, model, _step_toolsets(fleet))
    async for event in iterate_run(request, {}, plan=run_plan):
        kind = event.get("type")
        task_id = event.get("task_id")
        if kind == "task_started" and task_id:
            started.append(task_id)
        elif str(kind).startswith("task_") and task_id:
            tasks[task_id] = {
                "status": event.get("status"),
                "output": event.get("output"),
                "error": event.get("error"),
            }
        elif kind in ("graph_complete", "error"):
            terminal = dict(event.get("result") or {})
    output_task = plan["output_task_id"]
    return {
        "status": terminal.get("status"),
        "error": terminal.get("error"),
        "output": (tasks.get(output_task) or {}).get("output"),
        "started_order": started,
        "tasks": tasks,
        "fleet_calls": _fleet_calls(fleet),
    }


def _plan_model(context: Any) -> Any:
    model = context.services.get("task_plan_model")
    if model is not None:
        return model
    from arbora.agent_runs import resolve_model, run_config

    model_id = run_config().get("agent_model")
    if not model_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no agent model is configured for task plans"}
        )
    return resolve_model(model_id)


async def _feature_schema(context: Any, tenant: str) -> Mapping[str, Any]:
    """The tenant's published task-plan schema pin (ARBORA-RELEASE-R003.4.4)."""
    fixed = context.services.get("task_plan_feature_schema")
    if isinstance(fixed, Mapping):
        return fixed
    pins = context.services.get("task_plan_schema_pins")
    publish = context.services.get("task_plan_schema_publish")
    if pins is None or publish is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no verified task-plan schema pin"}
        )
    try:
        return await pins.for_tenant(tenant, publish)
    except OperationRefused:
        raise
    except Exception as exc:
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": f"task-plan schema publish failed: {type(exc).__name__}"},
        ) from exc


def _native_plan_run_id(plan: Mapping[str, Any]) -> str:
    """Use the existing bounded plan-run identity for native WorkItem metadata."""
    digest = plan.get("plan_digest")
    if not isinstance(digest, str) or not digest:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "verified task plan has no digest"}
        )
    run_id = digest.removeprefix("sha256:")[:32]
    if not run_id:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "verified task plan has no run identity"}
        )
    return run_id


def _native_plan_claim_valid(
    claim: Mapping[str, Any], *, session: Any, worker_id: str
) -> bool:
    """Validate the native lease before it becomes a served execution claim."""
    return (
        claim.get("claimed") is True
        and claim.get("tenant") == session.tenant
        and claim.get("lease_holder_ref") == worker_id
        and isinstance(worker_id, str)
        and bool(worker_id.strip())
        and all(
            type(claim.get(key)) is int and claim[key] >= 1
            for key in ("lease_epoch", "fencing_token")
        )
    )


async def _admit_native_plan_run(
    context: Any, plan: Mapping[str, Any]
) -> _NativePlanAdmission:
    """Submit, verify and claim a plan run through the existing native owner.

    The operation is composed with ``task_plan_work_engine`` only by the real
    served invocation.  The engine assigns the opaque WorkItem identity; this
    function never accepts a caller-selected native id or manufactures a lease.
    """
    import arbora.work_items as work_items
    from arbora.a2a import authority as a2a_authority
    from arbora.api.contracts.session import use_session

    engine = context.services.get("task_plan_work_engine")
    if engine is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native task-plan owner is not composed"}
        )
    caller = context.caller
    session = getattr(caller, "session", None)
    if session is None or session.tenant != caller.tenant:
        raise OperationRefused("UNAUTHENTICATED")

    run_id = _native_plan_run_id(plan)
    plan_digest = str(plan["plan_digest"])
    try:
        owner_ref = a2a_authority.WorkItemA2AAuthority._owner_ref(session)
    except (AttributeError, PermissionError, TypeError, ValueError) as exc:
        raise OperationRefused("UNAUTHENTICATED") from exc

    # Keep the A2A owner metadata schema because the existing run reader and
    # terminal projections already enforce this exact owner/tenant contract.
    metadata = {
        "a2a_schema": a2a_authority._A2A_METADATA_SCHEMA,
        "a2a_owner_ref": owner_ref,
        "a2a_run_id": run_id,
        "a2a_request_digest": plan_digest,
    }
    idempotency_key = f"agents.plan_run:{plan_digest}"
    worker_id: str | None = None
    with use_session(session):
        item_id, _created = await asyncio.to_thread(
            work_items.submit_work_item_atomic,
            engine,
            kind="orchestrator_task",
            queue="orchestrator_task",
            payload_ref=run_id,
            tenant=session.tenant,
            work_item_id=None,
            run_id=run_id,
            idempotency_key=idempotency_key,
            created_by=owner_ref,
            description="agents.plan_run",
            resource_class="agent_dispatch",
            metadata=metadata,
        )
        if not isinstance(item_id, str) or not item_id.strip():
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem returned no identity"}
            )
        try:
            if a2a_authority.WorkItemA2AAuthority._work_item_id(item_id) != item_id:
                raise ValueError("native WorkItem identity is not opaque")
        except ValueError as exc:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem identity is invalid"}
            ) from exc
        item = await asyncio.to_thread(work_items.get_work_item, engine, item_id)
        if not a2a_authority.WorkItemA2AAuthority._owned(item, owner_ref):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem owner verification failed"}
            )
        item_metadata = item.get("metadata") if isinstance(item, dict) else None
        if (
            not isinstance(item_metadata, dict)
            or item_metadata.get("a2a_run_id") != run_id
            or item_metadata.get("a2a_request_digest") != plan_digest
        ):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem run identity mismatch"}
            )
        status = str(item.get("status") or "") if isinstance(item, dict) else ""
        if status in work_items.TERMINAL_WORK_ITEM_STATUSES:
            return _NativePlanAdmission(
                item_id=item_id,
                run_id=run_id,
                lease=None,
                claim=None,
                terminal_status=status,
            )

        worker_id = f"arbora-a2a:{item_id}"
        claim = await asyncio.to_thread(
            work_items.claim_specific,
            engine,
            item_id,
            token=worker_id,
            lease_ttl_s=600.0,
        )
    if claim is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native WorkItem claim was not granted"}
        )
    if worker_id is None or not _native_plan_claim_valid(
        claim, session=session, worker_id=worker_id
    ):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native WorkItem claim is invalid"}
        )
    return _NativePlanAdmission(
        item_id=item_id,
        run_id=run_id,
        lease=WorkItemLease(
            run_id=run_id,
            worker_id=worker_id,
            lease_epoch=int(claim["lease_epoch"]),
            fencing_token=int(claim["fencing_token"]),
        ),
        claim=claim,
    )


async def _commit_native_plan_run(
    context: Any,
    admission: _NativePlanAdmission,
    *,
    outcome: str,
    error: bool = False,
) -> str:
    """Commit exactly one native terminal outcome under the returned fence."""
    from agent_connector_sdk.privacy import persistence_reference

    import arbora.work_items as work_items
    from arbora.api.contracts.session import use_session

    if admission.claim is None:
        raise RuntimeError("native plan terminal commit has no claim")
    result_ref = (
        persistence_reference("run", admission.run_id, namespace="arbora-plan")
        if not error
        else None
    )
    error_ref = (
        persistence_reference("error", "plan-failed", namespace="arbora-plan")
        if error
        else None
    )
    completion = {"schema_version": 1, "run_id": admission.run_id}
    session = context.caller.session
    with use_session(session):
        return await asyncio.to_thread(
            work_items.commit_result,
            context.services["task_plan_work_engine"],
            admission.item_id,
            admission.claim,
            outcome=outcome,
            result_ref=result_ref,
            error_ref=error_ref,
            run_completion=completion,
        )


def _terminal_replay(status: str) -> dict[str, Any]:
    """Project an already terminal native WorkItem without re-running it."""
    return {
        "status": status,
        "error": None,
        "output": None,
        "started_order": [],
        "tasks": {},
        "fleet_calls": [],
    }


async def handle_plan_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Decide, verify, attach and execute one task plan for the verified caller."""
    parsed = PlanRunParams.model_validate(dict(params))
    # The shared invocation boundary freezes all nested caller arguments before
    # dispatch.  The engine's strict TaskPlan DTO is a JSON boundary and
    # requires fresh lists and dictionaries; thaw only this proposal at that
    # boundary, retaining the frozen invocation arguments and caller claims.
    proposal = claim_values(parsed.plan)
    if not isinstance(proposal, dict):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "plan"})
    caller = context.caller
    feature_schema = await _feature_schema(context, str(caller.tenant))
    engine = context.services.get("task_plan_engine") or engine_task_plan(
        context.client
    )
    plan, record = await decide_task_plan(
        engine,
        proposal,
        tenant=caller.tenant,
        principal=caller.principal,
        feature_schema=feature_schema,
    )
    native: _NativePlanAdmission | None = None
    lease = parsed.work_item
    if lease is None and context.services.get("task_plan_work_engine") is not None:
        native = await _admit_native_plan_run(context, plan)
        if native.terminal_status is not None:
            return {
                "value": {
                    "record_id": record.get("record_id"),
                    "plan_digest": plan["plan_digest"],
                    "plan": plan,
                    "published": None,
                    "task_id": native.item_id,
                    "run": _terminal_replay(native.terminal_status),
                }
            }
        lease = native.lease
    published = None
    if lease is not None:
        try:
            request = publish_request(
                caller.tenant,
                plan,
                proposal,
                lease,
                now_ms=int(time.time() * 1000),
            )
            published = await engine.publish(request, request["idempotency_key"])
            run = await execute_task_plan(
                plan,
                _plan_model(context),
                run_id=lease.run_id,
                fleet=_fleet_step_tools(
                    context,
                    parsed.fleet_tools,
                    replay_record(caller) if parsed.fleet_tools else "",
                ),
            )
        except Exception:
            if native is not None:
                await _commit_native_plan_run(
                    context, native, outcome="failed", error=True
                )
            raise
    else:
        run_id = _native_plan_run_id(plan)
        run = await execute_task_plan(
            plan,
            _plan_model(context),
            run_id=run_id,
            fleet=_fleet_step_tools(
                context,
                parsed.fleet_tools,
                replay_record(caller) if parsed.fleet_tools else "",
            ),
        )
    if native is not None:
        status = run.get("status")
        outcome = (
            "succeeded"
            if status == "completed"
            else "cancelled"
            if status in {"cancelled", "canceled"}
            else "failed"
        )
        await _commit_native_plan_run(
            context,
            native,
            outcome=outcome,
            error=outcome == "failed",
        )
    return {
        "value": {
            "record_id": record.get("record_id"),
            "plan_digest": plan["plan_digest"],
            "plan": plan,
            "published": published,
            **({"task_id": native.item_id} if native is not None else {}),
            "run": run,
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="agents.plan_run",
            verb=Verb.ACT,
            summary="Run an engine-decided task plan of dependent agent tasks",
            examples=(
                "plan and run agents that triage the latest incidents and "
                "cross-check related issues",
            ),
            params=PlanRunParams,
            result=PlanRunResult,
            binding=Composite(handler="arbora.api.ops.plans.handle_plan_run"),
            scopes=frozenset({"kg:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "FleetStepTools",
    "FleetToolRef",
    "LateStepInvoke",
    "TASK_PLAN_ENGINE_RELEASE",
    "PlanRunParams",
    "PlanRunResult",
    "TaskPlanEngine",
    "WorkItemLease",
    "decide_task_plan",
    "engine_task_plan",
    "execute_task_plan",
    "handle_plan_run",
    "operations",
    "publish_request",
    "replay_record",
    "run_plan_for",
    "specs",
]
