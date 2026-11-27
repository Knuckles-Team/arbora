"""``agents.plan_run``: run an engine-decided task DAG (ARBORA-RELEASE-R003.4.1).

Arbora admits, serves and executes a task plan. It never plans: the plan comes
from the engine's ``Decide``/``TaskPlan`` decision (MEDULLA-CQ-ORCH-R001,
MEDULLA-DECISION-ENGINE-R134.111). One served call does this:

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
from dataclasses import dataclass
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from pydantic import Field

from arbora.api.domain.control_plane.runs.engine_policy import EngineRunPolicy
from arbora.api.invoke.certification_gate import (
    MAX_RUN_SKILLS,
    SkillRef,
    require_run_attestation,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params
from arbora.api.ops.plan_execution import (
    _host_ports,
    _screen,
    execute_task_plan,
)
from arbora.api.ops.plan_fleet_steps import (
    FleetToolRef,
    _fleet_step_tools,
    replay_record,
)
from arbora.api.ops.policy import policy_guard
from arbora.api.ops.ports import port_of
from arbora.api.ops.run_capacity import (
    CapacityReservation,
    release_quietly,
    run_capacity_of,
)
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)
from arbora.core.claims import claim_values

#: The engine release that serves ``TaskPlan``. Arbora's floor moves to it once
#: it is on PyPI (ARBORA-RELEASE-R003.4.3).
TASK_PLAN_ENGINE_RELEASE = "medulla>=2.27.5"
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
    context_id: str | None = None
    owner_graph: str | None = None
    capacity: CapacityReservation | None = None
    owner_digest: str | None = None


class PlanRunParams(Params):
    """Explicit caller claims. The engine owns graph validity and bounds."""

    plan: dict[str, Any] = Field(
        description=(
            "TaskPlanProposal: tasks [{task_id, prompt, depends_on}], "
            "output_task_id, max_concurrency, max_steps."
        )
    )
    work_item: WorkItemLease | None = None
    skills: list[SkillRef] = Field(
        default_factory=list,
        max_length=MAX_RUN_SKILLS,
        description=(
            "Atomic skills the plan uses. Each needs its current stored attestation "
            "before the plan is decided; a missing, stale or mismatched one refuses "
            "POLICY_DENIED."
        ),
    )
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
        facade = importlib.import_module("medulla.decision_client")
        senders = importlib.import_module("medulla.generated.coordination")
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
                "spec": "MEDULLA-CQ-ORCH-R001",
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
    question_id: str = TASK_PLAN_QUESTION_ID,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Ask the engine for the plan and verify its receipt; return plan + record.

    Engine refusals propagate unchanged. A proposal the engine facade rejects
    locally is ``INVALID_ARGUMENT`` with the facade's message.
    """
    try:
        request = engine.build_request(
            tenant, question_id, dict(proposal), dict(feature_schema)
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


def _validated_publish_receipt(receipt: Any, lease: WorkItemLease) -> dict[str, Any]:
    """Validate the legacy publisher's opaque receipt before returning it."""
    if isinstance(receipt, Mapping):
        run_id = receipt.get("run_id")
        revision = receipt.get("revision")
    else:
        run_id = getattr(receipt, "run_id", None)
        revision = getattr(receipt, "revision", None)
    if (
        run_id != lease.run_id
        or type(revision) is not int
        or revision != lease.revision
    ):
        raise OperationRefused("INVALID_TASK_PLAN_RECEIPT")
    return {"run_id": run_id, "revision": revision}


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


def _plan_model(context: Any) -> Any:
    model = context.services.get("task_plan_model")
    if model is not None:
        return model
    settings = port_of(context, "run_settings")
    config = context.services["config"]
    model_id = settings.run_config(config).get("agent_model")
    if not model_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no agent model is configured for task plans"}
        )
    return settings.resolve_model(model_id, config=config)


async def _feature_schema(context: Any, tenant: str) -> Mapping[str, Any]:
    """The tenant's published task-plan schema pin (ARBORA-RELEASE-R003.4.4).

    Read-only: the server lifespan publishes the pin under the process
    authority, and a request never publishes it.
    """
    fixed = context.services.get("task_plan_feature_schema")
    if isinstance(fixed, Mapping):
        return fixed
    from arbora.api.task_plan_schema import published_pin

    return published_pin(context.services.get("task_plan_schema_pins"), tenant)


def _pinned_feature_names(pin: Mapping[str, Any]) -> set[Any]:
    """The feature names the pin's schema body describes (empty when it has none)."""
    body: Any = pin.get("body", pin)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return set()
    features = body.get("features", ()) if isinstance(body, Mapping) else ()
    return {f.get("name") for f in features if isinstance(f, Mapping)}


def _require_pinned_features(
    proposal: Mapping[str, Any], pin: Mapping[str, Any]
) -> None:
    """Refuse a call or foreach the published schema pin does not describe.

    Arbora holds no binding validator (ARBORA-RELEASE-R003.4.6): the engine
    validates the calls. It only refuses to send a feature its pin predates.
    """
    needs: set[str] = set()
    for task in proposal.get("tasks", ()):
        if isinstance(task, Mapping) and task.get("call") is not None:
            needs.add("task_call")
        if isinstance(task, Mapping) and task.get("foreach") is not None:
            needs.add("task_foreach")
    if not needs:
        return
    known = _pinned_feature_names(pin)
    missing = sorted(needs - known)
    if missing:
        raise OperationRefused(
            "UNAVAILABLE",
            {
                "reason": "the task-plan schema pin does not describe these features",
                "features": missing,
                "spec": "ARBORA-RELEASE-R003.4.4",
            },
        )


def _native_plan_run_id(plan: Mapping[str, Any], caller: Any | None = None) -> str:
    """Use the caller-bound replay identity for a native plan run.

    The verified request identity is the idempotency authority.  The engine
    plan digest remains request metadata, but two requests can legitimately
    produce the same canonical plan and must still receive distinct runs.
    """
    if caller is not None:
        return replay_record(caller)
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
    claim: Mapping[str, Any],
    *,
    session: Any,
    worker_id: str,
    work_item_id: str,
    now_ms: int | None = None,
) -> bool:
    """Validate the native lease before it becomes a served execution claim."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    expiry = claim.get("lease_expires_at_ms")
    return (
        claim.get("claimed") is True
        and claim.get("tenant") == session.tenant
        and claim.get("work_item_id") == work_item_id
        and claim.get("lease_holder_ref") == worker_id
        and isinstance(worker_id, str)
        and bool(worker_id.strip())
        and all(
            type(claim.get(key)) is int and claim[key] >= 1
            for key in ("lease_epoch", "fencing_token")
        )
        and type(expiry) is int
        and expiry > now
    )


def _native_context_id(identity: Any, session: Any, owner_ref: str, run_id: str) -> str:
    """Derive the canonical A2A context identity from the admitted owner."""
    return "a2a-context-" + identity.digest(
        {"tenant": session.tenant, "owner": owner_ref, "context": run_id}
    )


def _native_plan_host(context: Any) -> Any:
    """Return the already-authorized hosted runtime for native publication."""
    from arbora.core.task_plan_config import TaskPlanConfig

    services = getattr(context, "operation_services", None)
    config = context.services.get("task_plan_config")
    if services is None or not isinstance(config, TaskPlanConfig):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native task-plan runtime is not composed"}
        )
    if getattr(services, "runtime", None) is None:
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "native task-plan invocation authority unavailable"},
        )
    return SimpleNamespace(services=services, task_plan_config=config)


async def _submit_through_policy(
    context: Any, engine: Any, submission: Mapping[str, Any]
) -> Any:
    """Submit one native plan WorkItem; the engine's active policy decides it.

    The decision is the one ``RunPolicyDecision`` port (ARBORA-OPS-R050.3); the
    caller's ``policy_guard`` reads its refusals as ``POLICY_DENIED`` and
    ``POLICY_UNAVAILABLE``.
    """
    work_items = port_of(context, "work_items")

    async def _submit(subject: object, _now: int) -> Any:
        item_id, _created = await asyncio.to_thread(
            work_items.submit_work_item_atomic, engine, **submission
        )
        return item_id

    policy = context.services.get("run_policy") or EngineRunPolicy(_submit)
    return await policy.decide(submission, now=int(time.time() * 1000))


async def _admit_native_plan_run(
    context: Any, plan: Mapping[str, Any]
) -> _NativePlanAdmission:
    """Submit, verify and claim a plan run through the existing native owner.

    The operation is composed with ``task_plan_work_engine`` only by the real
    served invocation.  The engine assigns the opaque WorkItem identity; this
    function never accepts a caller-selected native id or manufactures a lease.
    """
    from arbora.security.session import use_session

    work_items = port_of(context, "work_items")
    identity = port_of(context, "run_identity")
    engine = context.services.get("task_plan_work_engine")
    if engine is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native task-plan owner is not composed"}
        )
    caller = context.caller
    session = getattr(caller, "session", None)
    if session is None or session.tenant != caller.tenant:
        raise OperationRefused("UNAUTHENTICATED")

    run_id = _native_plan_run_id(plan, caller)
    plan_digest = str(plan["plan_digest"])
    try:
        owner_ref = identity.owner_ref(session)
    except (AttributeError, PermissionError, TypeError, ValueError) as exc:
        raise OperationRefused("UNAUTHENTICATED") from exc

    context_id = _native_context_id(identity, session, owner_ref, run_id)
    # Keep the A2A owner metadata schema because the existing run reader and
    # terminal projections already enforce this exact owner/tenant contract.
    # The plan digest is content identity; the request digest binds it to this
    # caller-bound replay identity and context.
    request_digest = identity.digest(
        {
            "context_id": context_id,
            "plan_digest": plan_digest,
            "request_id": caller.request_id,
        }
    )
    metadata = {
        "a2a_schema": identity.metadata_schema,
        "a2a_owner_ref": owner_ref,
        "a2a_run_id": run_id,
        "a2a_context_id": context_id,
        "a2a_request_digest": request_digest,
        "a2a_plan_digest": plan_digest,
    }
    idempotency_key = run_id
    worker_id: str | None = None
    with use_session(session):
        item_id = await _submit_through_policy(
            context,
            engine,
            {
                "kind": "orchestrator_task",
                "queue": "orchestrator_task",
                "payload_ref": run_id,
                "tenant": session.tenant,
                "work_item_id": None,
                "run_id": run_id,
                "idempotency_key": idempotency_key,
                "created_by": owner_ref,
                "description": "agents.plan_run",
                "resource_class": "agent_dispatch",
                "metadata": metadata,
            },
        )
        if not isinstance(item_id, str) or not item_id.strip():
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem returned no identity"}
            )
        try:
            if identity.opaque_work_item_id(item_id) != item_id:
                raise ValueError("native WorkItem identity is not opaque")
        except ValueError as exc:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem identity is invalid"}
            ) from exc
        item = await asyncio.to_thread(work_items.get_work_item, engine, item_id)
        if not identity.owned(item, owner_ref):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "native WorkItem owner verification failed"}
            )
        item_metadata = item.get("metadata") if isinstance(item, dict) else None
        if (
            not isinstance(item_metadata, dict)
            or item_metadata.get("a2a_run_id") != run_id
            or item_metadata.get("a2a_context_id") != context_id
            or item_metadata.get("a2a_request_digest") != request_digest
            or item_metadata.get("a2a_plan_digest") != plan_digest
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
                context_id=context_id,
                owner_graph=work_items.native_owner_graph(engine),
            )

        # The engine capacity lease is taken before the claim and released when
        # the terminal outcome commits (ARBORA-HOST-R031.1).
        owner_digest = identity.digest({"owner": owner_ref})
        capacity = await run_capacity_of(context).reserve(
            context, run_id=run_id, work_item_id=item_id, owner_digest=owner_digest
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
        await release_quietly(context, capacity, owner_digest=owner_digest)
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native WorkItem claim was not granted"}
        )
    if worker_id is None or not _native_plan_claim_valid(
        claim,
        session=session,
        worker_id=worker_id,
        work_item_id=item_id,
    ):
        await release_quietly(context, capacity, owner_digest=owner_digest)
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
        context_id=context_id,
        owner_graph=work_items.native_owner_graph(engine),
        capacity=capacity,
        owner_digest=owner_digest,
    )


async def _commit_native_plan_run(
    context: Any,
    admission: _NativePlanAdmission,
    *,
    outcome: str,
    error: bool = False,
) -> str:
    """Commit exactly one native terminal outcome under the returned fence."""
    from scion_sdk.privacy import persistence_reference

    from arbora.security.session import use_session

    work_items = port_of(context, "work_items")

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


async def _settle_native_plan_run(
    context: Any,
    admission: _NativePlanAdmission,
    *,
    outcome: str,
    error: bool = False,
) -> str:
    """Commit a terminal result and refuse a non-applied native status.

    A cancellation can interrupt the awaiting task while the WorkItem commit
    is still required.  Shield the commit task and keep waiting through an
    interruption; the caller receives cancellation again only after the
    durable outcome has been attempted.
    """
    commit_task = asyncio.create_task(
        _commit_native_plan_run(context, admission, outcome=outcome, error=error)
    )
    interrupted = False
    while True:
        try:
            status = await asyncio.shield(commit_task)
            break
        except asyncio.CancelledError:
            interrupted = True
            if commit_task.done() and commit_task.cancelled():
                raise OperationRefused(
                    "UNAVAILABLE",
                    {"reason": "native terminal commit was cancelled"},
                ) from None
            continue
    await release_quietly(
        context, admission.capacity, owner_digest=admission.owner_digest
    )
    if status != "committed":
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "native terminal commit was not applied", "status": status},
        )
    if interrupted:
        raise asyncio.CancelledError
    return status


async def _publish_and_execute_native_plan(
    context: Any,
    admission: _NativePlanAdmission,
    plan: Mapping[str, Any],
    proposal: Mapping[str, Any],
    parsed: PlanRunParams,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Run native plans through the existing publication/owner scopes."""
    from arbora.api.domain.mcp_server import task_plan_runtime
    from arbora.security.session import use_session

    work_items = port_of(context, "work_items")
    agent_runs = port_of(context, "run_settings")
    bind_admitted_claim = port_of(context, "plan_publication").bind_admitted_claim

    try:
        eg = import_module("medulla.generated.coordination")
    except (ImportError, AttributeError) as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "admitted_task_plan_runtime"}
        ) from exc
    if admission.claim is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native task-plan claim is unavailable"}
        )
    host = _native_plan_host(context)
    owner_graph = admission.owner_graph or work_items.native_owner_graph(
        context.services["task_plan_work_engine"]
    )
    context_id = admission.context_id
    if not isinstance(context_id, str) or not context_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native task-plan context is unavailable"}
        )
    owner = SimpleNamespace(
        run_id=admission.run_id,
        claim=admission.claim,
        owner_graph=owner_graph,
    )
    session = context.caller.session
    # ``hosted_invocation=None`` on native_run_scope intentionally preserves
    # the ordinary AU run loop.  The task-plan publication itself uses the
    # already-authorized hosted runtime above; AU is not given a second host
    # or authority source.
    with (
        use_session(session),
        agent_runs.native_run_scope(
            run_id=admission.run_id,
            task_id=admission.run_id,
            context_id=context_id,
            work_item_id=admission.item_id,
            session=session,
            claim=admission.claim,
            owner_graph=owner_graph,
            hosted_invocation=None,
        ),
        bind_admitted_claim(
            context.services["task_plan_work_engine"],
            session=session,
            run_id=admission.run_id,
            work_item_id=admission.item_id,
            claim=admission.claim,
            hosted_invocation=host,
            owner_graph=owner_graph,
        ),
    ):
        published_plan = await task_plan_runtime._publish(
            host,
            context.caller,
            owner,
            plan,
            proposal,
            eg,
        )
        run = await execute_task_plan(
            plan,
            _plan_model(context),
            run_id=admission.run_id,
            fleet=_fleet_step_tools(context, parsed.fleet_tools, context.caller, plan),
            screen=_screen(context),
            ports=_host_ports(context),
        )
    return published_plan, run


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


def _plan_assignees(plan: Any) -> tuple[str, ...]:
    """The agents the plan's tasks name as assignee: their definitions hold the skills."""
    tasks = plan.get("tasks", ()) if isinstance(plan, Mapping) else ()
    names = (t.get("assignee") for t in tasks if isinstance(t, Mapping))
    return tuple(dict.fromkeys(str(n) for n in names if n))


async def handle_plan_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Decide, verify, attach and execute one task plan for the verified caller."""
    parsed = PlanRunParams.model_validate(dict(params))
    await require_run_attestation(
        context, parsed.skills, agents=_plan_assignees(parsed.plan)
    )
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
    _require_pinned_features(proposal, feature_schema)
    with policy_guard():
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
        with policy_guard():
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
            if native is not None:
                published_plan, run = await _publish_and_execute_native_plan(
                    context, native, plan, proposal, parsed
                )
                # _publish performs receipt and readback verification.  Do
                # not expose its opaque transport receipt as if it were a
                # trusted result; return only the verified run/digest.
                published = {
                    "run_id": native.run_id,
                    "plan_digest": published_plan.get("plan_digest")
                    if isinstance(published_plan, Mapping)
                    else plan["plan_digest"],
                    "verified": True,
                }
            else:
                request = publish_request(
                    caller.tenant,
                    plan,
                    proposal,
                    lease,
                    now_ms=int(time.time() * 1000),
                )
                published = _validated_publish_receipt(
                    await engine.publish(request, request["idempotency_key"]),
                    lease,
                )
                run = await execute_task_plan(
                    plan,
                    _plan_model(context),
                    run_id=lease.run_id,
                    fleet=_fleet_step_tools(context, parsed.fleet_tools, caller, plan),
                    screen=_screen(context),
                    ports=_host_ports(context),
                )
        except asyncio.CancelledError:
            if native is not None:
                await _settle_native_plan_run(
                    context, native, outcome="cancelled", error=False
                )
            raise
        except Exception:
            if native is not None:
                await _settle_native_plan_run(
                    context, native, outcome="failed", error=True
                )
            raise
    else:
        # The non-native compatibility path has no WorkItem idempotency
        # authority.  Keep its historical plan identity; native admission
        # always uses the caller-bound replay identity above.
        run_id = _native_plan_run_id(plan)
        run = await execute_task_plan(
            plan,
            _plan_model(context),
            run_id=run_id,
            fleet=_fleet_step_tools(context, parsed.fleet_tools, caller, plan),
            screen=_screen(context),
            ports=_host_ports(context),
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
        await _settle_native_plan_run(
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
    "specs",
]
