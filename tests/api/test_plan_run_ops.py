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
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("agent_utilities.graph", reason="agent-utilities run loop")
from agent_utilities.graph import DependencyPlan  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from arbora.api.invoke.pipeline import OperationRefused  # noqa: E402
from arbora.api.ops import plans  # noqa: E402

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

    async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        plan = _canonical(request["params"][0]["value"]["value"])
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

    @staticmethod
    def verify(record, request, *, expected_principal):
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
        if plan != _canonical(proposal):
            raise ValueError("task_plan receipt mismatch: plan digest")
        return plan

    async def publish(self, request: dict[str, Any], idempotency_key: str) -> Any:
        self.published.append((request, idempotency_key))
        return {"run_id": request["snapshot"]["run_id"], "revision": 1}


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
        caller=SimpleNamespace(principal=PRINCIPAL, tenant=TENANT),
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
    assert value["published"] == {"run_id": "run-1", "revision": 1}
    # A replay of the same proposal reads the same plan digest.
    again, _, _ = await _serve(TRIAGE_DAG, work_item=lease)
    assert again["plan_digest"] == value["plan_digest"]


# spec: ARBORA-RELEASE-R003.4.1
def test_engine_without_task_plan_refuses_unavailable(monkeypatch):
    import epistemic_graph.decision_client as facade

    monkeypatch.delattr(facade, "task_plan_request", raising=False)
    with pytest.raises(OperationRefused) as refused:
        plans.engine_task_plan(object())
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["spec"] == "EG-CQ-ORCH-R001"


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
        self.calls.append((server, tool, dict(arguments)))
        return {"number": "INC0001", "echo": dict(arguments)}


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


async def _serve_fleet(plan, gateway, prompts, **extra):
    engine = FakeEngine()
    context = _context(engine, RecordingModel())
    context.services["task_plan_model"] = _fleet_model(prompts)
    context.services["fleet_gateway"] = gateway
    context.caller = SimpleNamespace(
        principal=PRINCIPAL, tenant=TENANT, effective_scopes=frozenset()
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
    gateway, prompts = FakeGateway(), []
    run = await _serve_fleet(FLEET_DAG, gateway, prompts, fleet_tools=[SN_TOOL])
    assert run["status"] == "completed"
    assert gateway.calls == [
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
    gateway, prompts = FakeGateway(), []
    plan = {**FLEET_DAG, "tasks": [_task("deny"), _task("use", "deny")]}
    run = await _serve_fleet(plan, gateway, prompts, fleet_tools=[SN_TOOL])
    assert gateway.calls == []
    assert run["fleet_calls"][0]["ok"] is False
    assert run["fleet_calls"][0]["error"] == "SCOPE_DENIED"
    assert run["status"] == "completed"


# spec: ARBORA-RELEASE-R003.4.5
async def test_a_tool_needing_confirmation_is_not_run():
    gateway, prompts = FakeGateway(confirm="plan"), []
    run = await _serve_fleet(FLEET_DAG, gateway, prompts, fleet_tools=[SN_TOOL])
    assert gateway.calls == []
    assert run["fleet_calls"][0]["error"] == "CONFIRMATION_REQUIRED"


# spec: ARBORA-RELEASE-R003.4.5
async def test_without_fleet_tools_a_step_has_no_fleet_call():
    gateway, prompts = FakeGateway(), []
    run = await _serve_fleet(FLEET_DAG, gateway, prompts)
    assert gateway.calls == [] and run["fleet_calls"] == []
