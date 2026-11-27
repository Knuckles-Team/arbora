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
    assert "process.run" not in _OPS  # waits for the meristem step kinds


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
    from arbora.processes.approvals import ApprovalNotPending, ApproverRoleRequired

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
        (ApprovalNotPending(), "NOT_FOUND"),
        (ApproverRoleRequired(), "POLICY_DENIED"),
    ):
        monkeypatch.setattr(processes, "broker_for", lambda _c, e=error: _Broker(e))
        with pytest.raises(OperationRefused) as refused:
            await processes.handle_approval_decide(
                ctx, params, _OPS["process.approval.decide"]
            )
        assert refused.value.code == code
