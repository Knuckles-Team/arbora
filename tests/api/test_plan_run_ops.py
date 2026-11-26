"""``agents.plan_run``: engine-decided DAGs run in dependency order.

Validation checks c22 (dependency DAG) and c21 (bounded fan-out). The engine
is a fake that speaks the engine's TaskPlan contract: the request shape, the
canonical plan (topological, lexical ready ties), its refusal codes and its
digest scheme, taken from the engine's ``TaskPlan`` types and served method.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("meristem.graph", reason="meristem run loop")
from meristem.graph import DependencyPlan  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from arbora.api.invoke.pipeline import OperationRefused  # noqa: E402
from arbora.api.ops import plans  # noqa: E402
from arbora.security.actor_context import ActorContext, ActorType  # noqa: E402
from arbora.security.session import GraphSession  # noqa: E402

PRINCIPAL = "user:alice"
TENANT = "tenant-a"
SCHEMA_PIN = {
    "component_id": "task-plan-schema",
    "kind": "feature_schema",
    "definition_digest": "sha256:" + "1" * 64,
}


def _digest(domain: str, value: Any) -> str:
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    hasher = hashlib.sha256(domain.encode() + b"\x00" + body.encode())
    return "sha256:" + hasher.hexdigest()


class EngineRefusal(Exception):
    """The engine's INVALID_ARGUMENT refusal, raised by its client."""

    def __init__(self, message: str) -> None:
        self.code = "INVALID_ARGUMENT"
        super().__init__(message)


def _canonical(proposal: dict[str, Any]) -> dict[str, Any]:
    """The engine's ``plan_explicit_tasks``: refusal codes and canonical order."""
    tasks: dict[str, dict[str, Any]] = {}
    if not proposal["tasks"]:
        raise EngineRefusal("invalid parameter: empty_plan")
    if not 0 < proposal["max_concurrency"] <= min(8, proposal["max_steps"]):
        raise EngineRefusal("invalid parameter: plan_bounds")
    for task in proposal["tasks"]:
        tasks[task["task_id"]] = {
            "task_id": task["task_id"],
            "prompt": task["prompt"],
            "depends_on": sorted(task.get("depends_on", [])),
        }
    for task in tasks.values():
        for dep in task["depends_on"]:
            if dep not in tasks:
                raise EngineRefusal(f"invalid parameter: missing_dependency: {dep}")
    done: list[str] = []
    ordered: list[dict[str, Any]] = []
    while len(ordered) < len(tasks):
        ready = [
            t
            for key, t in sorted(tasks.items())
            if key not in done and all(d in done for d in t["depends_on"])
        ]
        if not ready:
            raise EngineRefusal(
                "invalid parameter: plan_cycle: dependency graph is cyclic"
            )
        done.append(ready[0]["task_id"])
        ordered.append(ready[0])
    plan: dict[str, Any] = {
        "tasks": ordered,
        "output_task_id": proposal["output_task_id"],
        "max_concurrency": proposal["max_concurrency"],
        "max_steps": proposal["max_steps"],
        "total_tasks": len(ordered),
        "policy_revision": "bounded-explicit-dag-v1",
        "policy_digest": _digest("eg/task-plan-policy/v1", "fake"),
        "input_digest": _digest("eg/task-plan-input/v1", proposal),
    }
    plan["plan_digest"] = _digest(
        "eg/task-plan/v1",
        [plan[k] for k in ("tasks", "output_task_id", "max_concurrency")]
        + [plan[k] for k in ("max_steps", "total_tasks")]
        + [plan["policy_revision"], plan["policy_digest"]],
    )
    return plan


class FakeEngine:
    """Speaks the engine TaskPlan contract over ``Decide``/``PublishCurrentPlan``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.published: list[tuple[dict[str, Any], str]] = []

    def build_request(self, tenant, question_id, proposal, feature_schema):
        if set(proposal) - {"tasks", "output_task_id", "max_concurrency", "max_steps"}:
            raise ValueError("TaskPlanProposal: extra fields not permitted")
        return {
            "tenant_id": tenant,
            "question": {
                "question_id": question_id,
                "kind": "task_plan",
                "safety": "ordinary",
            },
            "candidates": {
                "source": "declared",
                "options": [
                    {"option_id": t["task_id"]}
                    for t in sorted(proposal["tasks"], key=lambda t: t["task_id"])
                ],
            },
            "feature_schema": feature_schema,
            "head": None,
            "params": [
                {"name": "task_plan", "value": {"type": "task_plan", "value": proposal}}
            ],
            "max_records": 1,
        }

    canonical = staticmethod(_canonical)

    async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        plan = self.canonical(request["params"][0]["value"]["value"])
        principal = "principal:sha256:" + hashlib.sha256(PRINCIPAL.encode()).hexdigest()
        return {
            "schema_version": 2,
            "inputs_digest": "sha256:" + "2" * 64,
            "records": [
                {
                    "record_id": "decision:" + "3" * 64,
                    "caller_principal": principal,
                    "tenant_id": request["tenant_id"],
                    "task_plan": plan,
                }
            ],
        }

    @classmethod
    def verify(cls, record, request, *, expected_principal):
        """The receipt checks of the engine facade's ``verify_task_plan_receipt``."""
        expected = (
            "principal:sha256:"
            + hashlib.sha256(expected_principal.encode()).hexdigest()
        )
        if record["caller_principal"] != expected:
            raise ValueError("task_plan receipt mismatch: caller principal")
        plan = record["task_plan"]
        proposal = request["params"][0]["value"]["value"]
        if plan["input_digest"] != _digest("eg/task-plan-input/v1", proposal):
            raise ValueError("task_plan receipt mismatch: input digest")
        if plan != cls.canonical(proposal):
            raise ValueError("task_plan receipt mismatch: plan digest")
        return plan

    async def publish(self, request: dict[str, Any], idempotency_key: str) -> Any:
        self.published.append((request, idempotency_key))
        return {
            "run_id": request["snapshot"]["run_id"],
            "revision": request["snapshot"]["revision"],
        }


class RecordingModel:
    """A model that records which task prompts run concurrently."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.order: list[str] = []
        self.model = FunctionModel(self._respond)

    async def _respond(self, messages: Any, info: Any) -> ModelResponse:
        prompt = next(
            part.content
            for message in messages
            for part in getattr(message, "parts", ())
            if getattr(part, "part_kind", "") == "user-prompt"
        )
        task = str(prompt).split("::", 1)[0]
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.order.append(task)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return ModelResponse(parts=[TextPart(content=f"done:{task}")])


def _task(task_id: str, *deps: str) -> dict[str, Any]:
    return {"task_id": task_id, "prompt": f"{task_id}:: work", "depends_on": list(deps)}


def _context(engine: FakeEngine, model: RecordingModel) -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(
            principal=PRINCIPAL, tenant=TENANT, request_id="request:test"
        ),
        client=None,
        services={
            "task_plan_engine": engine,
            "task_plan_model": model.model,
            "task_plan_feature_schema": SCHEMA_PIN,
        },
    )


async def _serve(plan: dict[str, Any], **extra: Any) -> tuple[dict, FakeEngine, Any]:
    engine, model = FakeEngine(), RecordingModel()
    (op,) = plans.operations()
    result = await plans.handle_plan_run(
        _context(engine, model), {"plan": plan, **extra}, op
    )
    return result["value"], engine, model


class StrictFakeEngine(FakeEngine):
    """Apply the released engine DTO at the real invocation boundary."""

    def build_request(self, tenant, question_id, proposal, feature_schema):
        from medulla.decision_client import task_plan_request

        return task_plan_request(tenant, question_id, proposal, feature_schema)


# spec: ARBORA-RELEASE-R003.4.1
@pytest.mark.asyncio
async def test_invoke_thaws_frozen_plan_before_strict_engine_dto():
    """The public invoke path preserves frozen arguments and strict JSON DTOs."""
    from copy import deepcopy

    from arbora.api.invoke import OpResult, invoke
    from arbora.api.registry import Surface
    from tests.api._support import make_verified_caller
    from tests.api.test_invoke_contract import setup_services

    (op,) = plans.operations()
    services, runtime, _, _, _ = setup_services(op)
    engine, model = StrictFakeEngine(), RecordingModel()
    runtime.bindings = {
        "task_plan_engine": engine,
        "task_plan_model": model.model,
        "task_plan_feature_schema": SCHEMA_PIN,
    }

    async def dispatch() -> Any:
        dispatched_op, dispatched_params, context = runtime.calls[-1]
        return await plans.handle_plan_run(context, dispatched_params, dispatched_op)

    runtime.on_dispatch = dispatch
    supplied = make_verified_caller("kg:write")(
        principal=PRINCIPAL,
        tenant=TENANT,
        request_id="request:strict-plan",
    )
    original = deepcopy(TRIAGE_DAG)

    result = await invoke(
        op.id,
        {"plan": TRIAGE_DAG},
        supplied,
        Surface.MCP,
        services=services,
        idempotency_key="strict-plan:good",
    )

    assert isinstance(result, OpResult)
    assert result.value["value"]["plan_digest"]
    assert TRIAGE_DAG == original
    assert engine.requests[0]["params"][0]["value"]["value"] == TRIAGE_DAG

    bad = await invoke(
        op.id,
        {"plan": {**TRIAGE_DAG, "authorize": True}},
        supplied,
        Surface.MCP,
        services=services,
        idempotency_key="strict-plan:bad",
    )
    assert bad.code == "INVALID_ARGUMENT"
    assert len(engine.requests) == 1


TRIAGE_DAG = {
    "tasks": [
        _task("report", "triage", "crosscheck"),
        _task("crosscheck", "incidents", "issues"),
        _task("triage", "incidents"),
        _task("issues"),
        _task("incidents"),
    ],
    "output_task_id": "report",
    "max_concurrency": 2,
    "max_steps": 5,
}


# spec: ARBORA-RELEASE-R003.4.1
async def test_c22_dag_runs_in_dependency_order_with_verified_receipt():
    value, engine, model = await _serve(TRIAGE_DAG)
    run = value["run"]
    assert run["status"] == "completed"
    order = run["started_order"]
    assert sorted(order) == ["crosscheck", "incidents", "issues", "report", "triage"]
    for task in TRIAGE_DAG["tasks"]:
        for dep in task["depends_on"]:
            assert order.index(dep) < order.index(task["task_id"])
    assert run["output"] == "done:report"
    assert all(t["status"] == "completed" for t in run["tasks"].values())
    # The executed plan is the engine's canonical plan, bound by its digest.
    request = engine.requests[0]
    assert value["plan"] == _canonical(request["params"][0]["value"]["value"])
    assert value["plan_digest"] == value["plan"]["plan_digest"]
    assert value["record_id"].startswith("decision:")
    assert model.peak <= 2


# spec: ARBORA-RELEASE-R003.4.1
def test_run_plan_maps_engine_plan_without_reordering():
    plan = _canonical(TRIAGE_DAG)
    run_plan = plans.run_plan_for(plan, model="test")
    graph = run_plan.dependency_plan
    assert isinstance(graph, DependencyPlan)
    assert [t.task_id for t in graph.tasks] == [t["task_id"] for t in plan["tasks"]]
    assert graph.plan_id == plan["plan_digest"]
    assert graph.max_concurrency == run_plan.budget.max_concurrent_requests == 2
    assert run_plan.stop.max_steps == 5


@pytest.mark.parametrize(
    ("tasks", "code"),
    [
        ([_task("a", "b"), _task("b", "a"), _task("out", "a")], "plan_cycle"),
        ([_task("a", "ghost"), _task("out", "a")], "missing_dependency"),
    ],
)
# spec: ARBORA-RELEASE-R003.4.1
async def test_c22_engine_refusals_pass_through_unchanged(tasks, code):
    plan = {"tasks": tasks, "output_task_id": "out", "max_concurrency": 1}
    plan["max_steps"] = len(tasks)
    engine, model = FakeEngine(), RecordingModel()
    (op,) = plans.operations()
    with pytest.raises(EngineRefusal) as refused:
        await plans.handle_plan_run(_context(engine, model), {"plan": plan}, op)
    assert code in str(refused.value)
    assert refused.value.code == "INVALID_ARGUMENT"
    assert model.order == []


# spec: ARBORA-RELEASE-R003.4.1
async def test_receipt_for_another_principal_is_refused_before_running():
    engine, model = FakeEngine(), RecordingModel()
    context = _context(engine, model)
    context.caller = SimpleNamespace(principal="user:mallory", tenant=TENANT)
    (op,) = plans.operations()
    with pytest.raises(OperationRefused) as refused:
        await plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op)
    assert refused.value.code == "RECEIPT_MISMATCH"
    assert model.order == []


# spec: ARBORA-RELEASE-R003.4.1
async def test_proposal_fields_outside_the_contract_are_refused():
    engine, model = FakeEngine(), RecordingModel()
    (op,) = plans.operations()
    with pytest.raises(OperationRefused) as refused:
        await plans.handle_plan_run(
            _context(engine, model), {"plan": {**TRIAGE_DAG, "authorize": True}}, op
        )
    assert refused.value.code == "INVALID_ARGUMENT"
    assert engine.requests == []


# spec: ARBORA-RELEASE-R003.4.1
async def test_c21_fan_out_runs_at_most_max_concurrency_and_returns_all():
    children = [_task(f"child-{i:02d}") for i in range(20)]
    fan_out = {
        "tasks": [*children, _task("join", *(c["task_id"] for c in children))],
        "output_task_id": "join",
        "max_concurrency": 8,
        "max_steps": 21,
    }
    value, _, model = await _serve(fan_out)
    run = value["run"]
    assert run["status"] == "completed"
    assert model.peak == 8
    results = {k: v for k, v in run["tasks"].items() if k != "join"}
    assert len(results) == 20
    assert all(v["output"] == f"done:{k}" for k, v in results.items())
    assert run["started_order"][-1] == "join"


# spec: ARBORA-RELEASE-R003.4.2
async def test_work_item_run_publishes_the_same_plan_for_replay():
    lease = {"run_id": "run-1", "worker_id": "w", "lease_epoch": 3, "fencing_token": 7}
    value, engine, _ = await _serve(TRIAGE_DAG, work_item=lease)
    ((request, key),) = engine.published
    assert request["snapshot"]["task_plan"] == value["plan"]
    assert request["task_plan_proposal"] == TRIAGE_DAG
    assert request["snapshot"]["run_id"] == "run-1"
    assert (request["lease_epoch"], request["fencing_token"]) == (3, 7)
    assert key == f"task-plan:run-1:{value['plan_digest']}"
    assert value["published"] == {"run_id": "run-1", "revision": 0}
    # A replay of the same proposal reads the same plan digest.
    again, _, _ = await _serve(TRIAGE_DAG, work_item=lease)
    assert again["plan_digest"] == value["plan_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        {"run_id": "other-run", "revision": 0},
        {"run_id": "run-1", "revision": 1},
        {"run_id": "run-1", "revision": False},
    ],
)
async def test_legacy_work_item_publish_validates_run_and_revision(receipt):
    """The compatibility publisher cannot return an unchecked opaque receipt."""
    engine, model = FakeEngine(), RecordingModel()

    async def publish(_request, _idempotency_key):
        return receipt

    engine.publish = publish
    (op,) = plans.operations()
    lease = {"run_id": "run-1", "worker_id": "w", "lease_epoch": 3, "fencing_token": 7}
    with pytest.raises(OperationRefused) as refused:
        await plans.handle_plan_run(
            _context(engine, model), {"plan": TRIAGE_DAG, "work_item": lease}, op
        )
    assert refused.value.code == "INVALID_TASK_PLAN_RECEIPT"
    assert model.order == []


def _native_session() -> GraphSession:
    actor = ActorContext(
        actor_id=PRINCIPAL,
        actor_type=ActorType.HUMAN,
        tenant_id=TENANT,
        authenticated=True,
    )
    return GraphSession(
        actor=actor,
        tenant=TENANT,
        graph=TENANT,
        scopes=frozenset({"kg:write"}),
        audience="arbora",
        policy_version="test-policy",
    )


def _native_plan_context(engine: FakeEngine) -> Any:
    context = _context(engine, RecordingModel())
    context.caller = SimpleNamespace(
        principal=PRINCIPAL,
        tenant=TENANT,
        request_id="native-request",
        session=_native_session(),
    )
    context.services["task_plan_work_engine"] = SimpleNamespace(graph_name=TENANT)
    return context


@pytest.mark.asyncio
async def test_native_plan_admission_uses_engine_identity_and_claim(monkeypatch):
    from arbora import work_items
    from arbora.a2a import authority as a2a_authority
    from arbora.a2a.authority import WorkItemA2AAuthority

    item_id = "work-item:" + "a" * 64
    context = _native_plan_context(FakeEngine())
    context.caller.request_id = "b" * 32
    owner = WorkItemA2AAuthority._owner_ref(context.caller.session)
    run_id = "run-" + "b" * 32
    plan_digest = "sha256:" + ("b" * 32) + ("c" * 32)
    plan = {"plan_digest": plan_digest}
    context_id = plans._native_context_id(context.caller.session, owner, run_id)
    request_digest = a2a_authority._digest(
        {
            "context_id": context_id,
            "plan_digest": plan_digest,
            "request_id": context.caller.request_id,
        }
    )
    seen: dict[str, Any] = {}
    item = {
        "kind": "orchestrator_task",
        "status": "ready",
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": owner,
            "a2a_run_id": run_id,
            "a2a_context_id": context_id,
            "a2a_request_digest": request_digest,
            "a2a_plan_digest": plan_digest,
        },
    }

    def submit(_engine, **kwargs):
        seen["submit"] = kwargs
        return item_id, True

    def get(_engine, requested):
        assert requested == item_id
        return item

    def claim(_engine, requested, *, token, lease_ttl_s):
        assert requested == item_id
        assert token == f"arbora-a2a:{item_id}"
        assert lease_ttl_s == 600.0
        return {
            "claimed": True,
            "tenant": TENANT,
            "lease_holder_ref": token,
            "work_item_id": item_id,
            "lease_epoch": 2,
            "fencing_token": 9,
            "lease_expires_at_ms": int(time.time() * 1000) + 600_000,
        }

    monkeypatch.setattr(work_items, "submit_work_item_atomic", submit)
    monkeypatch.setattr(work_items, "get_work_item", get)
    monkeypatch.setattr(work_items, "claim_specific", claim)
    admission = await plans._admit_native_plan_run(context, plan)

    assert admission.item_id == item_id
    assert admission.lease is not None
    assert admission.lease.run_id == run_id
    assert admission.lease.worker_id == f"arbora-a2a:{item_id}"
    assert (admission.lease.lease_epoch, admission.lease.fencing_token) == (2, 9)
    assert seen["submit"]["work_item_id"] is None
    assert seen["submit"]["run_id"] == run_id
    assert seen["submit"]["idempotency_key"] == run_id
    assert seen["submit"]["metadata"]["a2a_owner_ref"] == owner


@pytest.mark.asyncio
async def test_native_plan_replay_does_not_reclaim_terminal_item(monkeypatch):
    from arbora import work_items
    from arbora.a2a import authority as a2a_authority
    from arbora.a2a.authority import WorkItemA2AAuthority

    item_id = "work-item:" + "d" * 64
    context = _native_plan_context(FakeEngine())
    context.caller.request_id = "e" * 32
    owner = WorkItemA2AAuthority._owner_ref(context.caller.session)
    run_id = "run-" + "e" * 32
    plan_digest = "sha256:" + ("e" * 32) + ("f" * 32)
    plan = {"plan_digest": plan_digest}
    context_id = plans._native_context_id(context.caller.session, owner, run_id)
    request_digest = a2a_authority._digest(
        {
            "context_id": context_id,
            "plan_digest": plan_digest,
            "request_id": context.caller.request_id,
        }
    )
    item = {
        "kind": "orchestrator_task",
        "status": "succeeded",
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": owner,
            "a2a_run_id": run_id,
            "a2a_context_id": context_id,
            "a2a_request_digest": request_digest,
            "a2a_plan_digest": plan_digest,
        },
    }

    monkeypatch.setattr(
        work_items,
        "submit_work_item_atomic",
        lambda _engine, **_kwargs: (item_id, False),
    )
    monkeypatch.setattr(work_items, "get_work_item", lambda _engine, _id: item)

    def must_not_claim(*_args, **_kwargs):
        raise AssertionError("terminal native replay must not claim again")

    monkeypatch.setattr(work_items, "claim_specific", must_not_claim)
    admission = await plans._admit_native_plan_run(context, plan)
    assert admission.terminal_status == "succeeded"
    assert admission.claim is None
    assert admission.lease is None


def test_native_plan_replay_identity_is_not_plan_digest():
    plan = {"plan_digest": "sha256:" + "a" * 64}
    first = SimpleNamespace(request_id="request:first")
    second = SimpleNamespace(request_id="request:second")
    assert plans._native_plan_run_id(plan, first) == "run-request:first"
    assert plans._native_plan_run_id(plan, second) == "run-request:second"
    assert plans._native_plan_run_id(plan, first) != plans._native_plan_run_id(
        plan, second
    )


@pytest.mark.asyncio
async def test_native_plan_commits_success_and_failed_terminal_results(monkeypatch):
    context = _native_plan_context(FakeEngine())
    admission = plans._NativePlanAdmission(
        item_id="work-item:" + "1" * 64,
        run_id="2" * 32,
        lease=plans.WorkItemLease(
            run_id="2" * 32,
            worker_id="worker",
            lease_epoch=1,
            fencing_token=3,
        ),
        claim={
            "claimed": True,
            "tenant": TENANT,
            "lease_holder_ref": "worker",
            "work_item_id": "work-item:" + "1" * 64,
            "lease_epoch": 1,
            "fencing_token": 3,
            "lease_expires_at_ms": int(time.time() * 1000) + 600_000,
        },
    )
    commits: list[tuple[str, bool]] = []

    async def admit(_context, _plan):
        return admission

    async def commit(_context, _admission, *, outcome, error=False):
        commits.append((outcome, error))
        return "committed"

    monkeypatch.setattr(plans, "_admit_native_plan_run", admit)
    monkeypatch.setattr(plans, "_commit_native_plan_run", commit)

    async def publish_and_execute(*_args, **_kwargs):
        return {"plan_digest": _canonical(TRIAGE_DAG)["plan_digest"]}, {
            "status": "completed",
            "error": None,
            "output": None,
            "started_order": [],
            "tasks": {},
            "fleet_calls": [],
        }

    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", publish_and_execute)
    (op,) = plans.operations()
    context.services["task_plan_work_engine"] = object()
    value = await plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op)
    assert value["value"]["task_id"] == admission.item_id
    assert commits == [("succeeded", False)]

    async def failed(*_args, **_kwargs):
        return {"plan_digest": _canonical(TRIAGE_DAG)["plan_digest"]}, {
            "status": "failed",
            "error": "bounded failure",
            "output": None,
            "started_order": [],
            "tasks": {},
            "fleet_calls": [],
        }

    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", failed)
    await plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op)
    assert commits[-1] == ("failed", True)

    async def cancelled(*_args, **_kwargs):
        return {"plan_digest": _canonical(TRIAGE_DAG)["plan_digest"]}, {
            "status": "cancelled",
            "error": None,
            "output": None,
            "started_order": [],
            "tasks": {},
            "fleet_calls": [],
        }

    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", cancelled)
    await plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op)
    assert commits[-1] == ("cancelled", False)

    async def exploding(*_args, **_kwargs):
        raise RuntimeError("execution failed")

    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", exploding)
    with pytest.raises(RuntimeError, match="execution failed"):
        await plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op)
    assert commits[-1] == ("failed", True)


@pytest.mark.asyncio
async def test_native_plan_cancellation_commits_cancelled_before_reraising(monkeypatch):
    context = _native_plan_context(FakeEngine())
    admission = plans._NativePlanAdmission(
        item_id="work-item:" + "2" * 64,
        run_id="run-cancelled",
        lease=plans.WorkItemLease(
            run_id="run-cancelled",
            worker_id="worker",
            lease_epoch=1,
            fencing_token=3,
        ),
        claim={
            "claimed": True,
            "tenant": TENANT,
            "lease_holder_ref": "worker",
            "work_item_id": "work-item:" + "2" * 64,
            "lease_epoch": 1,
            "fencing_token": 3,
            "lease_expires_at_ms": int(time.time() * 1000) + 600_000,
        },
    )
    commits: list[tuple[str, bool]] = []

    async def admit(_context, _plan):
        return admission

    async def commit(_context, _admission, *, outcome, error=False):
        commits.append((outcome, error))
        return "committed"

    started = asyncio.Event()

    async def cancelled(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(plans, "_admit_native_plan_run", admit)
    monkeypatch.setattr(plans, "_commit_native_plan_run", commit)
    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", cancelled)
    (op,) = plans.operations()
    task = asyncio.create_task(plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert commits == [("cancelled", False)]


@pytest.mark.asyncio
async def test_native_plan_refuses_noncommitted_terminal_status(monkeypatch):
    context = _native_plan_context(FakeEngine())
    admission = plans._NativePlanAdmission(
        item_id="work-item:" + "3" * 64,
        run_id="run-noop",
        lease=plans.WorkItemLease(
            run_id="run-noop", worker_id="worker", lease_epoch=1, fencing_token=3
        ),
        claim={
            "claimed": True,
            "tenant": TENANT,
            "lease_holder_ref": "worker",
            "work_item_id": "work-item:" + "3" * 64,
            "lease_epoch": 1,
            "fencing_token": 3,
            "lease_expires_at_ms": int(time.time() * 1000) + 600_000,
        },
    )

    async def admit(_context, _plan):
        return admission

    async def publish_and_execute(*_args, **_kwargs):
        return {"plan_digest": _canonical(TRIAGE_DAG)["plan_digest"]}, {
            "status": "completed",
            "error": None,
            "output": None,
            "started_order": [],
            "tasks": {},
            "fleet_calls": [],
        }

    async def noop(*_args, **_kwargs):
        return "noop"

    monkeypatch.setattr(plans, "_admit_native_plan_run", admit)
    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", publish_and_execute)
    monkeypatch.setattr(plans, "_commit_native_plan_run", noop)
    (op,) = plans.operations()
    with pytest.raises(OperationRefused, match="UNAVAILABLE") as refused:
        await plans.handle_plan_run(context, {"plan": TRIAGE_DAG}, op)
    assert refused.value.details["reason"] == "native terminal commit was not applied"


@pytest.mark.parametrize(
    "changes",
    [
        {"work_item_id": "work-item:other"},
        {"lease_expires_at_ms": 1},
        {"fencing_token": 0},
        {"lease_epoch": 0},
    ],
)
def test_native_plan_claim_requires_item_expiry_and_fence(changes):
    claim = {
        "claimed": True,
        "tenant": TENANT,
        "lease_holder_ref": "worker",
        "work_item_id": "work-item:item",
        "lease_epoch": 1,
        "fencing_token": 3,
        "lease_expires_at_ms": int(time.time() * 1000) + 600_000,
    }
    claim.update(changes)
    assert not plans._native_plan_claim_valid(
        claim,
        session=_native_session(),
        worker_id="worker",
        work_item_id="work-item:item",
    )


# spec: ARBORA-RELEASE-R003.4.1
def test_engine_without_task_plan_refuses_unavailable(monkeypatch):
    import medulla.decision_client as facade

    monkeypatch.delattr(facade, "task_plan_request", raising=False)
    with pytest.raises(OperationRefused) as refused:
        plans.engine_task_plan(object())
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["spec"] == "MEDULLA-CQ-ORCH-R001"


class FakeGateway:
    """The governed fleet gateway: effect decision, then the child call."""

    def __init__(self, confirm: str = "none") -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.confirm = confirm

    async def effect(self, op, params, caller):
        from arbora.api.registry import Confirm

        return SimpleNamespace(
            effect=op.effect,
            confirm=Confirm(self.confirm),
            required_scopes=frozenset(),
        )

    async def call(self, caller, server, tool, arguments, **kwargs):
        raise AssertionError("a plan step must go through the invoke pipeline")


class FakeInvoke:
    """The invoke pipeline's effect journal: one effect per idempotency key."""

    def __init__(self) -> None:
        self.effects: list[tuple[str, str, dict[str, Any]]] = []
        self.journal: dict[str, Any] = {}
        self.keys: list[str] = []

    async def __call__(self, op_id, params, caller, key):
        from arbora.api.invoke.steps import OpResult

        assert op_id == "fleet.call"
        self.keys.append(key)
        if key not in self.journal:
            self.effects.append(
                (params["server"], params["tool"], dict(params["arguments"]))
            )
            self.journal[key] = {
                "value": {"number": "INC0001", "echo": dict(params["arguments"])}
            }
        return OpResult(value=self.journal[key])


def _fleet_model(prompts: list[str]) -> FunctionModel:
    """Step ``make`` calls fleet_call; step ``use`` reads its dependency's output."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    async def respond(messages: Any, info: Any) -> ModelResponse:
        parts = [p for m in messages for p in getattr(m, "parts", ())]
        prompt = str(next(p.content for p in parts if p.part_kind == "user-prompt"))
        prompts.append(prompt)
        returned = [p for p in parts if isinstance(p, ToolReturnPart)]
        if prompt.startswith("make::") and not returned:
            args = {
                "server": "servicenow-mcp",
                "tool": "servicenow_create_incident",
                "arguments": {"data": {"short_description": "from gitlab"}},
            }
            return ModelResponse(parts=[ToolCallPart("fleet_call", args)])
        if prompt.startswith("make::"):
            return ModelResponse(parts=[TextPart(content="INC0001")])
        if prompt.startswith("deny::") and not returned:
            args = {"server": "other", "tool": "t", "arguments": {}}
            return ModelResponse(parts=[ToolCallPart("fleet_call", args)])
        return ModelResponse(parts=[TextPart(content="finished")])

    return FunctionModel(respond)


BOUND_REQUEST = "a" * 64


async def _serve_fleet(
    plan, gateway, prompts, invoke=None, request_id=BOUND_REQUEST, **extra
):
    engine = FakeEngine()
    context = _context(engine, RecordingModel())
    context.services["task_plan_model"] = _fleet_model(prompts)
    context.services["fleet_gateway"] = gateway
    context.services["plan_step_invoke"] = invoke or FakeInvoke()
    context.caller = SimpleNamespace(
        principal=PRINCIPAL,
        tenant=TENANT,
        effective_scopes=frozenset(),
        request_id=request_id,
    )
    context.service_identity = False
    context.owner = PRINCIPAL
    context.registry_digest = ""
    (op,) = plans.operations()
    result = await plans.handle_plan_run(context, {"plan": plan, **extra}, op)
    return result["value"]["run"]


FLEET_DAG = {
    "tasks": [_task("make"), _task("use", "make")],
    "output_task_id": "use",
    "max_concurrency": 1,
    "max_steps": 2,
}
SN_TOOL = {"server": "servicenow-mcp", "tool": "servicenow_create_incident"}


# spec: ARBORA-RELEASE-R003.4.5
async def test_step_calls_a_named_fleet_tool_and_the_next_step_reads_its_output():
    gateway, prompts, invoke = FakeGateway(), [], FakeInvoke()
    run = await _serve_fleet(FLEET_DAG, gateway, prompts, invoke, fleet_tools=[SN_TOOL])
    assert run["status"] == "completed"
    assert invoke.effects == [
        (
            "servicenow-mcp",
            "servicenow_create_incident",
            {"data": {"short_description": "from gitlab"}},
        )
    ]
    (entry,) = run["fleet_calls"]
    assert entry["ok"] is True and entry["result"]["number"] == "INC0001"
    use_prompt = next(p for p in prompts if p.startswith("use::"))
    assert "Dependency outputs" in use_prompt and "INC0001" in use_prompt


# spec: ARBORA-RELEASE-R003.4.5
async def test_a_tool_the_caller_did_not_name_is_refused_as_data():
    gateway, prompts, invoke = FakeGateway(), [], FakeInvoke()
    plan = {**FLEET_DAG, "tasks": [_task("deny"), _task("use", "deny")]}
    run = await _serve_fleet(plan, gateway, prompts, invoke, fleet_tools=[SN_TOOL])
    assert invoke.effects == []
    assert run["fleet_calls"][0]["ok"] is False
    assert run["fleet_calls"][0]["error"] == "SCOPE_DENIED"
    assert run["status"] == "completed"


# spec: ARBORA-RELEASE-R003.4.5
async def test_a_tool_needing_confirmation_is_not_run():
    gateway, prompts, invoke = FakeGateway(confirm="plan"), [], FakeInvoke()
    run = await _serve_fleet(FLEET_DAG, gateway, prompts, invoke, fleet_tools=[SN_TOOL])
    assert invoke.effects == []
    assert run["fleet_calls"][0]["error"] == "CONFIRMATION_REQUIRED"


# spec: ARBORA-RELEASE-R003.4.5
async def test_without_fleet_tools_a_step_has_no_fleet_call():
    gateway, prompts, invoke = FakeGateway(), [], FakeInvoke()
    run = await _serve_fleet(FLEET_DAG, gateway, prompts, invoke)
    assert invoke.effects == [] and run["fleet_calls"] == []


# spec: ARBORA-RELEASE-R003.4.5
async def test_replayed_plan_returns_the_recorded_outcome_without_a_second_effect():
    gateway, invoke = FakeGateway(), FakeInvoke()
    first = await _serve_fleet(FLEET_DAG, gateway, [], invoke, fleet_tools=[SN_TOOL])
    again = await _serve_fleet(FLEET_DAG, gateway, [], invoke, fleet_tools=[SN_TOOL])
    # The key is the run's replay record, the task and the call index.
    record = "run-" + BOUND_REQUEST[:32]
    assert invoke.keys == [f"plan-step:{record}:make:1"] * 2
    assert len(invoke.effects) == 1
    assert first["fleet_calls"][0]["result"] == again["fleet_calls"][0]["result"]
    assert again["fleet_calls"][0]["idempotency_key"] == invoke.keys[0]


# spec: ARBORA-RELEASE-R003.4.11
async def test_a_new_run_key_is_a_new_effect_and_the_decision_record_is_not_the_key():
    """G2: the decision record id changes per Decide; the replay record does not."""
    gateway, invoke = FakeGateway(), FakeInvoke()
    await _serve_fleet(FLEET_DAG, gateway, [], invoke, fleet_tools=[SN_TOOL])
    await _serve_fleet(
        FLEET_DAG, gateway, [], invoke, request_id="b" * 64, fleet_tools=[SN_TOOL]
    )
    assert len(invoke.effects) == 2
    assert all("decision:" not in key for key in invoke.keys)


# spec: ARBORA-RELEASE-R003.4.11
async def test_a_fleet_run_without_a_bound_request_identity_runs_nothing():
    gateway, invoke = FakeGateway(), FakeInvoke()
    with pytest.raises(plans.OperationRefused) as refused:
        await _serve_fleet(
            FLEET_DAG, gateway, [], invoke, request_id=None, fleet_tools=[SN_TOOL]
        )
    assert refused.value.code == "UNAVAILABLE"
    assert invoke.effects == []


# spec: ARBORA-RELEASE-R003.4.5
async def test_an_unbound_invoke_pipeline_runs_no_effect():
    gateway, prompts = FakeGateway(), []
    plan = {**FLEET_DAG, "tasks": [_task("make"), _task("use", "make")]}
    engine = FakeEngine()
    context = _context(engine, RecordingModel())
    context.services.update(
        task_plan_model=_fleet_model(prompts), fleet_gateway=gateway
    )
    context.caller = SimpleNamespace(
        principal=PRINCIPAL,
        tenant=TENANT,
        effective_scopes=frozenset(),
        request_id=BOUND_REQUEST,
    )
    (op,) = plans.operations()
    result = await plans.handle_plan_run(
        context, {"plan": plan, "fleet_tools": [SN_TOOL]}, op
    )
    (entry,) = result["value"]["run"]["fleet_calls"]
    assert entry["ok"] is False and entry["error"] == "UNAVAILABLE"


# --- typed tool steps and foreach (ARBORA-RELEASE-R003.4.6 to R003.4.17) ---------


def _binding_refs(value: Any) -> list[str]:
    if isinstance(value, dict) and set(value) == {"$ref"}:
        return [value["$ref"]]
    children = value.values() if isinstance(value, dict) else value
    if not isinstance(value, dict | list):
        return []
    return [ref for child in children for ref in _binding_refs(child)]


def _check_bindings(task: dict[str, Any]) -> None:
    """The engine's ``binding_not_dependency`` refusal."""
    refs = _binding_refs((task.get("call") or {}).get("arguments"))
    refs += _binding_refs((task.get("foreach") or {}).get("source"))
    steps = {ref.split(".")[1] for ref in refs if ref.startswith("steps.")}
    if steps - set(task.get("depends_on", [])):
        raise EngineRefusal("invalid parameter: binding_not_dependency")


def _canonical_tools(proposal: dict[str, Any]) -> dict[str, Any]:
    """The engine's canonical plan with its typed ``call`` and ``foreach`` kept."""
    bare = [
        {k: v for k, v in t.items() if k not in ("call", "foreach")}
        for t in proposal["tasks"]
    ]
    base = _canonical({**proposal, "tasks": bare})
    declared = {t["task_id"]: t for t in proposal["tasks"]}
    for task in proposal["tasks"]:
        _check_bindings(task)
    typed = ("call", "foreach")
    tasks = [
        {
            **t,
            **{
                k: declared[t["task_id"]][k]
                for k in typed
                if k in declared[t["task_id"]]
            },
        }
        for t in base["tasks"]
    ]
    plan = {**base, "tasks": tasks}
    plan["input_digest"] = _digest("eg/task-plan-input/v1", proposal)
    plan["plan_digest"] = _digest("eg/task-plan/v1", [tasks, plan["output_task_id"]])
    return plan


class ToolEngine(FakeEngine):
    """A recorded engine that keeps typed tool steps in the canonical plan."""

    canonical = staticmethod(_canonical_tools)


class ToolInvoke(FakeInvoke):
    """The effect journal over two fixture connectors; ``list_ids`` is a read."""

    def __init__(self) -> None:
        super().__init__()
        self.audit: list[str] = []

    def answer(self, params: dict[str, Any]) -> dict[str, Any]:
        if params["tool"] == "list_ids":
            return self.ids_answer()
        return {"echo": dict(params["arguments"]), "token": "sk-" + "x" * 24}

    def ids_answer(self) -> dict[str, Any]:
        return {"ids": ["a", "b", "c"]}

    def _record(self, params: dict[str, Any], key: str) -> None:
        if params["tool"] != "list_ids":
            self.effects.append(
                (params["server"], params["tool"], dict(params["arguments"]))
            )
            self.audit.append(key)
        self.journal[key] = {"value": self.answer(params)}

    async def __call__(self, op_id, params, caller, key):
        from arbora.api.invoke.steps import OpResult

        self.keys.append(key)
        if key not in self.journal:
            self._record(params, key)
        return OpResult(value=self.journal[key])


class WriteGateway(FakeGateway):
    async def effect(self, op, params, caller):
        from arbora.api.registry import Confirm, Effect

        return SimpleNamespace(
            effect=Effect.WRITE, confirm=Confirm.NONE, required_scopes=frozenset()
        )


def _json_model(texts: dict[str, str]) -> FunctionModel:
    async def respond(messages: Any, info: Any) -> ModelResponse:
        parts = [p for m in messages for p in getattr(m, "parts", ())]
        prompt = str(next(p.content for p in parts if p.part_kind == "user-prompt"))
        return ModelResponse(parts=[TextPart(content=texts[prompt.split("::")[0]])])

    return FunctionModel(respond)


def _call_task(
    task_id: str, server: str, tool: str, arguments: Any, *deps: str, **more: Any
):
    return {
        "task_id": task_id,
        "prompt": f"{task_id}:: call",
        "depends_on": list(deps),
        "call": {"server": server, "tool": tool, "arguments": arguments},
        **more,
    }


def _plan(*tasks: dict[str, Any], output: str) -> dict[str, Any]:
    return {
        "tasks": list(tasks),
        "output_task_id": output,
        "max_concurrency": 1,
        "max_steps": len(tasks),
    }


TOOL_PIN = {
    **SCHEMA_PIN,
    "body": {"features": [{"name": "task_call"}, {"name": "task_foreach"}]},
}
BINDING_PLAN = _plan(
    {"task_id": "emit", "prompt": "emit:: json", "depends_on": []},
    _call_task(
        "open",
        "alpha-mcp",
        "create",
        {"title": {"$ref": "steps.emit.output.title"}, "n": 2},
        "emit",
    ),
    _call_task(
        "link",
        "beta-mcp",
        "attach",
        {"ref": {"$ref": "steps.open.output.echo.title"}, "password": "hunter2"},
        "open",
    ),
    output="link",
)


async def _serve_tools(
    plan,
    *,
    invoke=None,
    gateway=None,
    pin=TOOL_PIN,
    request_id=BOUND_REQUEST,
    texts=None,
):
    engine = ToolEngine()
    context = _context(engine, RecordingModel())
    context.services.update(
        task_plan_model=_json_model(texts or {"emit": '{"title": "T1"}'}),
        task_plan_feature_schema=pin,
        fleet_gateway=gateway or FakeGateway(),
        plan_step_invoke=invoke or ToolInvoke(),
    )
    context.caller = SimpleNamespace(
        principal=PRINCIPAL,
        tenant=TENANT,
        effective_scopes=frozenset(),
        request_id=request_id,
    )
    context.service_identity = False
    context.owner = PRINCIPAL
    context.registry_digest = ""
    (op,) = plans.operations()
    result = await plans.handle_plan_run(context, {"plan": plan}, op)
    return result["value"], engine


# spec: ARBORA-RELEASE-R003.4.6
async def test_plan_run_maps_tool_steps_to_run_tasks():
    value, engine = await _serve_tools(BINDING_PLAN)
    # The proposal reaches the engine unchanged: Arbora validates no binding.
    sent = engine.requests[0]["params"][0]["value"]["value"]
    assert sent["tasks"][1]["call"] == BINDING_PLAN["tasks"][1]["call"]
    run_plan = plans.run_plan_for({**value["plan"]}, None)
    mapped = {t.task_id: t for t in run_plan.dependency_plan.tasks}
    assert mapped["emit"].call is None
    assert mapped["open"].call.server == "alpha-mcp"
    assert mapped["open"].call.tool == "create"
    assert (
        dict(mapped["open"].call.arguments)
        == BINDING_PLAN["tasks"][1]["call"]["arguments"]
    )
    assert [t.task_id for t in run_plan.dependency_plan.tasks] == [
        t["task_id"] for t in value["plan"]["tasks"]
    ]


# spec: ARBORA-RELEASE-R003.4.6
async def test_engine_binding_refusal_passes_through_and_starts_nothing():
    bad = _plan(
        {"task_id": "emit", "prompt": "emit:: json", "depends_on": []},
        _call_task("open", "a", "t", {"x": {"$ref": "steps.emit.output.title"}}),
        output="open",
    )
    invoke = ToolInvoke()
    with pytest.raises(EngineRefusal) as refused:
        await _serve_tools(bad, invoke=invoke)
    assert "binding_not_dependency" in str(refused.value)
    assert invoke.effects == []


# spec: ARBORA-RELEASE-R003.4.6
async def test_call_against_unpinned_schema_refuses_unavailable():
    invoke = ToolInvoke()
    with pytest.raises(OperationRefused) as refused:
        await _serve_tools(BINDING_PLAN, invoke=invoke, pin=SCHEMA_PIN)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["features"] == ["task_call"]
    assert invoke.effects == []


# spec: ARBORA-RELEASE-R003.4.8
async def test_reply_lists_resolved_arguments_and_results_per_step():
    value, _ = await _serve_tools(BINDING_PLAN)
    steps = value["run"]["steps"]
    assert [s["task_id"] for s in steps] == ["open", "link"]
    first, second = steps
    assert first["resolved_arguments"] == {"title": "T1", "n": 2}
    assert first["outcome"] == "ok"
    assert first["result"]["echo"] == {"title": "T1", "n": 2}
    assert second["resolved_arguments"]["ref"] == "T1"
    record = "run-" + BOUND_REQUEST[:32]
    assert first["idempotency_key"] == f"plan-step:{record}:open:1"
    assert value["plan_digest"] == value["plan"]["plan_digest"]


# spec: ARBORA-RELEASE-R003.4.8
async def test_reply_masks_secret_values():
    value, _ = await _serve_tools(BINDING_PLAN)
    second = value["run"]["steps"][1]
    assert "hunter2" not in json.dumps(value["run"]["steps"])
    assert second["resolved_arguments"]["password"] != "hunter2"
    assert "sk-" + "x" * 24 not in json.dumps(value["run"]["steps"])


# spec: ARBORA-RELEASE-R003.4.8
async def test_unresolved_binding_reply_names_the_reference():
    plan = _plan(
        {"task_id": "emit", "prompt": "emit:: json", "depends_on": []},
        _call_task(
            "open", "a", "t", {"x": {"$ref": "steps.emit.output.missing"}}, "emit"
        ),
        output="open",
    )
    invoke = ToolInvoke()
    value, _ = await _serve_tools(plan, invoke=invoke)
    (step,) = value["run"]["steps"]
    assert step["outcome"] == "failed"
    assert step["failed_reference"] == "steps.emit.output.missing"
    assert step["resolved_arguments"] is None
    assert invoke.effects == []


# spec: ARBORA-RELEASE-R003.4.9
async def test_handle_plan_run_binds_the_governed_caller():
    from meristem.host_ports import tool_caller

    invoke = ToolInvoke()
    await _serve_tools(BINDING_PLAN, invoke=invoke)
    assert tool_caller() is plans.governed_tool_caller
    # Every typed step went through the invoke pipeline as ``fleet.call``.
    assert [e[:2] for e in invoke.effects] == [
        ("alpha-mcp", "create"),
        ("beta-mcp", "attach"),
    ]
    # No pipeline bound: the step is refused as data and no effect runs.
    engine = ToolEngine()
    context = _context(engine, RecordingModel())
    context.services.update(
        task_plan_model=_json_model({"emit": '{"title": "T1"}'}),
        task_plan_feature_schema=TOOL_PIN,
        fleet_gateway=FakeGateway(),
    )
    context.caller = SimpleNamespace(
        principal=PRINCIPAL,
        tenant=TENANT,
        effective_scopes=frozenset(),
        request_id=BOUND_REQUEST,
    )
    (op,) = plans.operations()
    result = await plans.handle_plan_run(context, {"plan": BINDING_PLAN}, op)
    first = result["value"]["run"]["steps"][0]
    assert first["outcome"] == "refused" and first["failure_reason"] == "UNAVAILABLE"


# spec: ARBORA-RELEASE-R003.4.10
async def test_cross_connector_plan_passes_data_by_binding_end_to_end():
    invoke = ToolInvoke()
    value, _ = await _serve_tools(BINDING_PLAN, invoke=invoke)
    assert value["run"]["status"] == "completed"
    assert value["plan_digest"].startswith("sha256:")
    assert invoke.effects[0][2] == {"title": "T1", "n": 2}
    assert invoke.effects[1][2]["ref"] == "T1"
    assert len(invoke.audit) == 2
    # A second run of the same record repeats no effect.
    await _serve_tools(BINDING_PLAN, invoke=invoke)
    assert len(invoke.effects) == 2
    # A binding to a missing path is a typed step failure; nothing depending starts.
    missing = _plan(
        {"task_id": "emit", "prompt": "emit:: json", "depends_on": []},
        _call_task("open", "a", "t", {"x": {"$ref": "steps.emit.output.nope"}}, "emit"),
        _call_task("link", "b", "t", {}, "open"),
        output="link",
    )
    again = ToolInvoke()
    failed, _ = await _serve_tools(missing, invoke=again, request_id="c" * 64)
    assert again.effects == []
    assert failed["run"]["status"] != "completed"
    assert "link" not in failed["run"]["started_order"]


FOREACH_PLAN = _plan(
    _call_task("list", "alpha-mcp", "list_ids", {}),
    _call_task(
        "each",
        "beta-mcp",
        "write",
        {"id": {"$ref": "item.id"}, "at": {"$ref": "item_index"}},
        "list",
        foreach={"source": {"$ref": "steps.list.output.ids"}, "max_items": 3},
    ),
    output="each",
)


def _objects(ids):
    return {"ids": [{"id": i} for i in ids]}


class ObjectInvoke(ToolInvoke):
    def __init__(self, ids) -> None:
        super().__init__()
        self.ids = ids

    def ids_answer(self) -> dict[str, Any]:
        return _objects(self.ids)


# spec: ARBORA-RELEASE-R003.4.14
async def test_foreach_runs_one_call_per_item_in_order_and_enforces_max_items():
    invoke = ObjectInvoke(["a", "b", "c"])
    value, _ = await _serve_tools(FOREACH_PLAN, invoke=invoke, gateway=WriteGateway())
    assert [e[2] for e in invoke.effects] == [
        {"id": "a", "at": 0},
        {"id": "b", "at": 1},
        {"id": "c", "at": 2},
    ]
    record = "run-" + BOUND_REQUEST[:32]
    assert invoke.keys[1:] == [f"plan-step:{record}:each:{i}" for i in range(3)]
    (step,) = [s for s in value["run"]["steps"] if s["task_id"] == "each"]
    assert [r["echo"]["id"] for r in step["result"]] == ["a", "b", "c"]
    over = ObjectInvoke(["a", "b", "c", "d"])
    failed, _ = await _serve_tools(FOREACH_PLAN, invoke=over, gateway=WriteGateway())
    assert over.effects == []
    (bad,) = [s for s in failed["run"]["steps"] if s["task_id"] == "each"]
    assert bad["outcome"] == "failed" and bad["failure_reason"] == "FOREACH_ITEM_LIMIT"


class Scalar(ObjectInvoke):
    def ids_answer(self) -> dict[str, Any]:
        return {"ids": "nope"}


# spec: ARBORA-RELEASE-R003.4.14
async def test_foreach_source_that_is_not_an_array_fails_the_step():
    invoke = Scalar([])
    value, _ = await _serve_tools(FOREACH_PLAN, invoke=invoke, gateway=WriteGateway())
    (step,) = [s for s in value["run"]["steps"] if s["task_id"] == "each"]
    assert step["failure_reason"] == "FOREACH_SOURCE_NOT_ARRAY"
    assert invoke.effects == []


# spec: ARBORA-RELEASE-R003.4.15
async def test_duplicate_write_item_fails_without_a_call_and_keeps_earlier_calls():
    invoke = ObjectInvoke(["a", "b", "a"])
    plan = _plan(
        FOREACH_PLAN["tasks"][0],
        _call_task(
            "each",
            "beta-mcp",
            "write",
            {"id": {"$ref": "item.id"}},
            "list",
            foreach={"source": {"$ref": "steps.list.output.ids"}, "max_items": 3},
        ),
        output="each",
    )
    value, _ = await _serve_tools(plan, invoke=invoke, gateway=WriteGateway())
    assert [e[2] for e in invoke.effects] == [{"id": "a"}, {"id": "b"}]
    (step,) = [s for s in value["run"]["steps"] if s["task_id"] == "each"]
    assert step["failure_reason"] == "FOREACH_DUPLICATE_WRITE"
    assert [i["outcome"] for i in step["items"]] == ["ok", "ok", "failed"]


# spec: ARBORA-RELEASE-R003.4.16
async def test_reply_lists_each_foreach_item_and_masks_secrets():
    invoke = ObjectInvoke(["a", "b"])
    value, _ = await _serve_tools(FOREACH_PLAN, invoke=invoke, gateway=WriteGateway())
    (step,) = [s for s in value["run"]["steps"] if s["task_id"] == "each"]
    assert [i["index"] for i in step["items"]] == [0, 1]
    assert step["items"][0]["resolved_arguments"] == {"id": "a", "at": 0}
    assert all(i["idempotency_key"] for i in step["items"])
    assert "sk-" + "x" * 24 not in json.dumps(step["items"])


# spec: ARBORA-RELEASE-R003.4.17
async def test_foreach_plan_served_end_to_end():
    # Entered at the operation handler: the step calls go through the invoke
    # pipeline's ``fleet.call`` caller; the HTTP and MCP doors share that handler.
    for run_key in ("first", "second"):
        invoke = ObjectInvoke(["x", "y", "z"])
        request_id = hashlib.sha256(run_key.encode()).hexdigest()
        value, _ = await _serve_tools(
            FOREACH_PLAN, invoke=invoke, gateway=WriteGateway(), request_id=request_id
        )
        assert len(invoke.effects) == 3
        assert value["run"]["status"] == "completed"
    duplicate = ObjectInvoke(["x", "y", "x"])
    plan = _plan(
        FOREACH_PLAN["tasks"][0],
        {
            **FOREACH_PLAN["tasks"][1],
            "call": {
                **FOREACH_PLAN["tasks"][1]["call"],
                "arguments": {"id": {"$ref": "item.id"}},
            },
        },
        output="each",
    )
    value, _ = await _serve_tools(plan, invoke=duplicate, gateway=WriteGateway())
    assert len(duplicate.effects) == 2
    assert value["run"]["status"] != "completed"
