"""Process plan and approval operations (ARBORA-OPS-R898.1, R898.2, R898.4)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import processes
from arbora.api.registry import Confirm, Idempotency, PrincipalRule
from tests.api._engine_double import context
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.processes._support import approver_clock, main_run_task  # noqa: F401
from tests.question_kinds import declare_question_kinds

PRINCIPAL = "principal:one"
_OPS = {op.id: op for op in processes.operations()}


@pytest.mark.spec("ARBORA-OPS-R898.1")
def test_models_round_trip_engine_vectors() -> None:
    plan = processes.ProcessPlanParams(workflow_id="wf", version=3)
    run = processes.ProcessRunParams(workflow_id="wf", version=3, inputs={"a": 1})
    decide = processes.ProcessApprovalDecideParams(
        run_id="r", step_id="s", verdict="approve", comment="ok"
    )
    for model in (plan, run, decide, processes.ProcessApprovalListParams(run_id="r")):
        assert type(model).model_validate(model.model_dump()) == model
    with pytest.raises(ValidationError):
        processes.ProcessApprovalDecideParams(
            run_id="r",
            step_id="s",
            verdict="abstain",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        processes.ProcessPlanParams(workflow_id="wf", version=1, extra=1)  # type: ignore[call-arg]


def _structural_request(
    tenant: str,
    question_id: str,
    schema: Any,
    *,
    graph: str,
    workflow_id: str,
    version: int,
) -> dict[str, Any]:
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    return decide_request(
        tenant,
        (question_id, "process_plan", "ordinary"),
        declared([DeclaredOption("process_plan")]),
        schema,
        params=[
            param("graph", "text", graph),
            param("process_plan", "text", f"{workflow_id}:{version}"),
        ],
    )


@pytest.mark.spec("ARBORA-OPS-R898.2")
async def test_plan_returns_engine_plan_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.decision_client as decision_client

    declare_question_kinds(monkeypatch, "process_plan")
    monkeypatch.setattr(
        decision_client, "process_plan_request", _structural_request, raising=False
    )
    sent: list[tuple[str, dict[str, Any]]] = []
    digest = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
    sealed = {"plan_digest": "sha256:p", "tasks": [{"task_id": "a"}]}

    async def engine(
        _context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        sent.append((method, params))
        request = params["request"]
        record = {
            "caller_principal": f"principal:sha256:{digest}",
            "tenant_id": request["tenant_id"],
            "question": request["question"],
            "inputs": {"params": request["params"]},
            "record_id": "rec:plan",
            "record_digest": "d1",
            "answer": {"type": "process_plan", "value": sealed},
            "premises": [{"id": "binding:a"}, {"id": "other:x"}],
        }
        return {"records": [record]}

    async def graph(_context: Any) -> str:
        return "graph:one"

    async def pin(_context: Any, _tenant: str) -> dict[str, Any]:
        return {"component_id": "pin", "digest": "d"}

    from arbora.api.ops import plans

    monkeypatch.setattr(processes, "engine_send", engine)
    monkeypatch.setattr(processes, "caller_dispatch_graph", graph)
    monkeypatch.setattr(plans, "_feature_schema", pin)
    ctx = context()
    result = await processes.handle_process_plan(
        ctx, {"workflow_id": "wf", "version": 2}, _OPS["process.plan"]
    )
    assert result == {
        "plan": sealed,
        "plan_digest": "sha256:p",
        "bindings": [{"id": "binding:a"}],
        "decision_id": "rec:plan",
    }
    [(method, params)] = sent
    assert method == "Decide" and list(params) == ["request"]


@pytest.mark.spec("ARBORA-OPS-R898.2")
async def test_plan_refuses_unavailable_until_the_pin_names_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(OperationRefused) as refused:
        await processes.handle_process_plan(
            context(), {"workflow_id": "wf", "version": 2}, _OPS["process.plan"]
        )
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R898.4")
def test_the_decide_operation_is_a_planned_human_write_with_the_identity_chain() -> (
    None
):
    decide = _OPS["process.approval.decide"]
    assert decide.principals is PrincipalRule.HUMAN
    assert decide.confirm is Confirm.PLAN
    assert decide.idempotency is Idempotency.KEY_REQUIRED
    assert decide.scopes == frozenset({"process:approve"})
    assert _OPS["process.approvals.list"].scopes == frozenset({"process:read"})
    run = _OPS["process.run"]
    assert run.scopes == frozenset({"process:run"})
    assert run.idempotency is Idempotency.KEY_REQUIRED
    assert "process.run" in processes.NATIVE_IDEMPOTENCY


class _Broker:
    """A broker double that records the deciding call."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.seen = error, []

    async def resolve(self, tenant: str, **fields: Any) -> dict[str, Any]:
        self.seen.append((tenant, fields))
        if self.error:
            raise self.error
        return {
            "run_id": fields["run_id"],
            "step_id": fields["step_id"],
            "state": "decided",
            "verdict": fields["verdict"],
            "approver": fields["principal"],
        }


@pytest.mark.spec("ARBORA-OPS-R898.4")
async def test_decide_stamps_the_verified_person_and_hides_a_missing_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.domain.processes.approvals import (
        ApprovalNotPending,
        ApproverRoleRequired,
    )

    params = {"run_id": "r", "step_id": "s", "verdict": "approve", "comment": "ok"}
    broker = _Broker()
    monkeypatch.setattr(processes, "broker_for", lambda _c: broker)
    ctx = context()
    ctx.caller.engine_claims = {**ctx.caller.engine_claims, "groups": ["role:finance"]}
    done = await processes.handle_approval_decide(
        ctx, params, _OPS["process.approval.decide"]
    )
    assert done["approver"] == "principal:one" and done["state"] == "decided"
    [(tenant, fields)] = broker.seen
    assert tenant == "tenant:one" and fields["groups"] == ("role:finance",)
    for error, code in (
        (ApprovalNotPending(), "APPROVAL_NOT_PENDING"),
        (ApproverRoleRequired(), "APPROVER_NOT_IN_ROLE"),
    ):
        monkeypatch.setattr(processes, "broker_for", lambda _c, e=error: _Broker(e))
        with pytest.raises(OperationRefused) as refused:
            await processes.handle_approval_decide(
                ctx, params, _OPS["process.approval.decide"]
            )
        assert refused.value.code == code


@pytest.mark.spec("ARBORA-OPS-R898.6")
def test_refusals_map_to_stable_codes() -> None:
    from arbora.api.engine_errors import ENGINE_ERRORS
    from arbora.api.errors import _ARBORA_STATUS, ArboraErrorCode

    assert ENGINE_ERRORS["WORKFLOW_UNKNOWN"][0] == 404
    for code in ("PROCESS_STEP_UNBOUND", "PROCESS_PLAN_LOOP", "BRANCH_NO_ARM"):
        assert ENGINE_ERRORS[code][0] == 422
    assert _ARBORA_STATUS[ArboraErrorCode.APPROVAL_NOT_PENDING] == 409
    assert _ARBORA_STATUS[ArboraErrorCode.APPROVER_NOT_IN_ROLE] == 403
    assert _ARBORA_STATUS[ArboraErrorCode.PROCESS_PROMPT_UNRESOLVED] == 422


@pytest.mark.spec("ARBORA-OPS-R898.5")
async def test_run_binds_ports_and_starts_admitted_plan(
    monkeypatch: pytest.MonkeyPatch,
    main_shape: None,  # noqa: F811
    main_run_task: None,  # noqa: F811
) -> None:
    from arbora.api.ops import plans
    from tests.processes._support import PLAN, ProcessEngine, run_loop

    declare_question_kinds(
        monkeypatch, "process_plan", "branch_select", "decision_table_eval"
    )
    _plan_double(monkeypatch)
    engine = ProcessEngine()
    seen: dict[str, Any] = {}

    async def loop(plan: Any, model: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs, plan=plan)
        return await run_loop(plan, model, **kwargs)

    ctx = context(key="run-1")
    approved: list[str] = []
    wait = approver_clock(processes.broker_for(ctx), seen, approved)
    ctx.services.update(
        process_wait=wait,
        now_ms=lambda: 5,
        task_plan_model="model:one",
        process_question_schema=lambda _t, q: {"component_id": f"decide.schema.{q}"},
    )
    monkeypatch.setattr(processes, "engine_send", engine.send)
    monkeypatch.setattr(plans, "execute_task_plan", loop)
    params = {"workflow_id": "wf", "version": 2, "inputs": {}}
    done = await processes.handle_process_run(ctx, params, _OPS["process.run"])
    assert done["status"] == "completed" and done["plan_digest"] == PLAN["plan_digest"]
    # The run started the admitted plan with the ports bound and the prompt texts resolved.
    assert seen["ports"].decision is not None and seen["ports"].approval is not None
    assert seen["prompts"]["prompt:triage"].startswith("Triage")
    assert approved == ["ap"]
    assert done["tasks"]["b1"] == {"taken": ["x1"]}
    # The decision port sent Decide BranchSelect to the engine as the verified caller.
    assert _kinds(engine)[-1] == "branch_select"
    # The journal cites the plan, the branch verdict record and the approver.
    evidence = await processes.broker_for(ctx).evidence("tenant:one", done["run_id"])
    assert evidence["run"]["plan_digest"] == PLAN["plan_digest"]
    assert evidence["settled"]["branch_verdicts"] == ["rec:branch_select"]
    assert [a["approver"] for a in evidence["approvals"]] == ["person:finance"]
    # The same key again starts no second run and answers the settled outcome.
    again = await processes.handle_process_run(ctx, params, _OPS["process.run"])
    assert again["run_id"] == done["run_id"] and again["status"] == "completed"
    assert _kinds(engine).count("branch_select") == 1


@pytest.mark.spec("ARBORA-OPS-R898.5")
async def test_run_refuses_before_any_step_when_a_prompt_or_the_harness_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.processes._support import PROMPTS, ProcessEngine

    declare_question_kinds(
        monkeypatch, "process_plan", "branch_select", "decision_table_eval"
    )
    _plan_double(monkeypatch)
    engine = ProcessEngine()
    monkeypatch.setattr(processes, "engine_send", engine.send)
    params = {"workflow_id": "wf", "version": 2, "inputs": {}}
    # A harness whose host record has no decision or approval port: the run refuses
    # UNAVAILABLE naming the gap.
    import dataclasses

    from meristem import host_ports

    bare = dataclasses.make_dataclass("HostPorts", [("events", Any, None)])
    monkeypatch.setattr(host_ports, "HostPorts", bare)
    ctx = context(key="k1")
    ctx.services.update(task_plan_model="m")
    with pytest.raises(OperationRefused) as refused:
        await processes.handle_process_run(ctx, params, _OPS["process.run"])
    assert refused.value.code == "UNAVAILABLE"
    assert "meristem" in refused.value.details["missing"]
    # An unresolved bound prompt refuses with its own code, before the ports bind.
    monkeypatch.delitem(PROMPTS, "prompt:ship")
    with pytest.raises(OperationRefused) as unresolved:
        await processes.handle_process_run(ctx, params, _OPS["process.run"])
    assert unresolved.value.code == "PROCESS_PROMPT_UNRESOLVED"
    assert unresolved.value.details == {"task_id": "x1"}
    assert [m for m, _p in engine.sent].count("KvCas") == 0  # nothing journaled


def _plan_double(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.decision_client as decision_client

    from arbora.api.ops import plans

    async def pin(_context: Any, _tenant: str) -> dict[str, Any]:
        return {"component_id": "pin", "digest": "d"}

    async def graph(_context: Any) -> str:
        return "graph:one"

    monkeypatch.setattr(
        decision_client, "process_plan_request", _structural_request, raising=False
    )
    monkeypatch.setattr(plans, "_feature_schema", pin)
    monkeypatch.setattr(processes, "caller_dispatch_graph", graph)


def _kinds(engine: Any) -> list[str]:
    """The question kind of every ``Decide`` the recording engine received."""
    return [p["request"]["question"]["kind"] for m, p in engine.sent if m == "Decide"]
