"""Development surface reads and reviews served over HTTP and MCP (ARBORA-OPS-R896)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest
from medulla import EngineResponseError

from arbora.api.ops import development
from tests.api._served_support import (
    McpAsk,
    confirmable_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

KEY = "Idempotency-Key"
PLAN = "Arbora-Plan-Ref"


def _confirmed(app: Any, op: str, params: dict[str, Any], key: str) -> Any:
    """Preview, then resubmit under the plan: the one confirmation path."""
    preview = _call(app, op, params, **{KEY: key})
    assert preview.json()["error"]["code"] == "CONFIRMATION_REQUIRED", preview.text
    ref = preview.json()["error"]["details"]["plan_ref"]
    return _call(app, op, params, **{KEY: key, PLAN: ref})


class LoopbackEngine:
    """Records the engine calls of the development surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.modes: dict[str, str] = {}
        self.proposals = {
            "proposal:1": {
                "id": "proposal:1",
                "kind": "row_add",
                "target": "R001",
                "evidence": [
                    {"kind": "gate_result", "record_id": "gate:1", "digest": "d1"},
                    {"kind": "gate_result", "record_id": "gate:gone", "digest": "d2"},
                    {"kind": "code_fact", "record_id": "code:1", "digest": "d3"},
                ],
                "state": "open",
            }
        }

    def node(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("GetNodeProperties", params))
        if params["node_id"] == "gate:1":
            return msgpack.packb(
                {"gate_id": "pre-commit", "verdict": {"outcome": "pass"}}
            )
        return None

    def mode_get(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("ProjectModeGet", params))
        project = params["request"]["project"]
        return {
            "node_id": "n",
            "project": project,
            "mode": self.modes.get(project, "reference"),
            "recorded": project in self.modes,
        }

    def mode_set(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("ProjectModeSet", params))
        request = params["request"]
        if request["mode"] == "evolving" and "approval" not in request:
            raise EngineResponseError("MODE_APPROVAL_REQUIRED", "needs approval")
        previous = self.modes.get(request["project"], "reference")
        self.modes[request["project"]] = request["mode"]
        return {"node_id": "n", "mode": request["mode"], "previous": previous}

    def proposal_list(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("ProposalList", params))
        wanted = params["request"].get("state")
        return [p for p in self.proposals.values() if wanted in (None, p["state"])]

    def proposal_decide(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("ProposalDecide", params))
        request = params["request"]
        row = self.proposals.get(request["id"])
        if row is None:
            raise EngineResponseError("PROPOSAL_NOT_FOUND", "no such proposal")
        if row["state"] != "open":
            raise EngineResponseError("PROPOSAL_NOT_OPEN", "closed")
        row["state"] = "approved" if request["decision"] == "approve" else "rejected"
        return {"id": row["id"], "state": row["state"]}

    def question(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("SpecQuestion", params))
        if params["question"] == "next_unblocked_task" and params["subject"] == "nope":
            raise EngineResponseError("COMPETENCY_SUBJECT_UNKNOWN", "unknown phase")
        return {
            "rows": [{"task": "T001", "phase": params["subject"] or ""}],
            "digests": ["abc"],
        }


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    return confirmable_invocation(monkeypatch)


@pytest.fixture
def env(stack: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    engine = LoopbackEngine()
    route_methods(
        client,
        {
            "ProjectModeGet": engine.mode_get,
            "GetNodeProperties": engine.node,
            "ProjectModeSet": engine.mode_set,
            "ProposalList": engine.proposal_list,
            "ProposalDecide": engine.proposal_decide,
            "SpecQuestion": engine.question,
        },
        monkeypatch,
        {"fixture-verified": person("one")},
    )
    return app, engine, stack["invocation"], monkeypatch


@pytest.mark.spec("ARBORA-OPS-R896.1")
def test_artifact_questions_reach_registered_queries(env: Any) -> None:
    app, engine, *_ = env
    answer = _value(
        _call(
            app, "spec.query", {"question": "next_unblocked_task", "subject": "phase-1"}
        )
    )
    assert answer == {
        "rows": [{"task": "T001", "phase": "phase-1"}],
        "digests": ["abc"],
    }
    assert [name for name, _ in engine.calls] == ["SpecQuestion"]
    assert engine.calls[0][1]["question"] == "next_unblocked_task"
    assert (
        _call(app, "spec.query", {"question": "next_unblocked_task"}).status_code == 400
    )
    assert (
        _call(
            app, "spec.query", {"question": "unbound_requirements", "subject": "x"}
        ).status_code
        == 400
    )
    unknown = _call(
        app, "spec.query", {"question": "next_unblocked_task", "subject": "nope"}
    )
    assert unknown.json()["error"]["code"] == "COMPETENCY_SUBJECT_UNKNOWN"


@pytest.mark.spec("ARBORA-OPS-R896.2")
def test_mode_reads_the_stored_record_and_defaults_to_reference(env: Any) -> None:
    app, engine, *_ = env
    assert (
        _value(_call(app, "project.mode.get", {"repository": "repo"}))["mode"]
        == "reference"
    )
    engine.modes["repo"] = "development"
    assert (
        _value(_call(app, "project.mode.get", {"repository": "repo"}))["mode"]
        == "development"
    )


@pytest.mark.spec("ARBORA-OPS-R896.2")
def test_evolving_mode_needs_operator_approval(env: Any) -> None:
    app, engine, *_ = env
    # The evolving move needs a stated reason, and nothing is sent without it.
    bare = {"repository": "repo", "mode": "evolving"}
    assert _call(app, "project.mode.set", bare, **{KEY: "m2"}).status_code == 400
    assert [n for n, _ in engine.calls if n == "ProjectModeSet"] == []
    body = {**bare, "reason": "goal work"}
    unconfirmed = _call(app, "project.mode.set", body, **{KEY: "m3"})
    assert unconfirmed.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert [n for n, _ in engine.calls if n == "ProjectModeSet"] == []
    moved = _value(_confirmed(app, "project.mode.set", body, "m4"))
    assert moved == {"repository": "repo", "mode": "evolving", "previous": "reference"}
    sent = [p for n, p in engine.calls if n == "ProjectModeSet"][0]["request"]
    assert sent["approval"]["approver"] == "person:one"


@pytest.mark.spec("ARBORA-OPS-R896.2")
async def test_mode_set_stamps_the_verified_approver_for_evolving() -> None:
    engine = LoopbackEngine()
    from unittest import mock

    import tests.api._served_support as support  # noqa: F401

    sent: list[dict[str, Any]] = []

    async def fake_send(
        context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        sent.append(params)
        return engine.mode_set(params, "")

    context = SimpleNamespace(caller=SimpleNamespace(principal="person:one"))
    with mock.patch.object(development, "engine_send", fake_send):
        done = await development.handle_project_mode_set(
            context,
            {"repository": "repo", "mode": "evolving", "reason": "goal work"},
            None,  # type: ignore[arg-type]
        )
        dev = await development.handle_project_mode_set(
            context,
            {"repository": "repo", "mode": "development"},
            None,  # type: ignore[arg-type]
        )
    assert done == {"repository": "repo", "mode": "evolving", "previous": "reference"}
    assert sent[0]["request"]["approval"]["approver"] == "person:one"
    assert sent[0]["request"]["approval"]["reason"] == "goal work"
    assert "approval" not in sent[1]["request"] and dev["previous"] == "evolving"


@pytest.mark.spec("ARBORA-OPS-R896.5")
def test_accept_opens_a_pull_request_and_never_writes_specs(env: Any) -> None:
    app, engine, *_ = env
    listed = _value(
        _call(app, "development.proposal.list", {"repository": "repo", "state": "open"})
    )
    assert (
        listed["items"][0]["id"] == "proposal:1"
        and listed["items"][0]["state"] == "open"
    )
    review = {
        "proposal_id": "proposal:1",
        "decision": "accept",
        "reason": "evidence holds",
    }
    done = _value(_call(app, "development.proposal.review", review, **{KEY: "r1"}))
    assert done == {"id": "proposal:1", "state": "approved", "pull_request": None}
    sent = [p for n, p in engine.calls if n == "ProposalDecide"][0]["request"]
    assert sent["reviewer"] == "person:one" and sent["decision"] == "approve"
    again = _call(app, "development.proposal.review", review, **{KEY: "r2"})
    assert again.json()["error"]["code"] == "PROPOSAL_NOT_OPEN"
    assert (
        _call(
            app,
            "development.proposal.review",
            {**review, "path": "specs/x.md"},
            **{KEY: "r3"},
        ).status_code
        == 400
    )
    # No operation writes a file: every call is one of the engine's proposal methods.
    assert {n for n, _ in engine.calls} <= {
        "ProposalList",
        "ProposalDecide",
        "GetNodeProperties",
    }


@pytest.mark.spec("ARBORA-OPS-R896.5")
def test_evidence_carries_the_sealed_gate_verdict_or_null(env: Any) -> None:
    app, engine, *_ = env
    listed = _value(_call(app, "development.proposal.list", {"repository": "repo"}))
    evidence = listed["items"][0]["evidence"]
    assert [e["gate_verdict"] for e in evidence] == [{"outcome": "pass"}, None, None]
    reads = [p["node_id"] for n, p in engine.calls if n == "GetNodeProperties"]
    assert reads == ["gate:1", "gate:gone"]


@pytest.mark.spec("ARBORA-OPS-R896.1", "ARBORA-OPS-R896.5")
def test_artifact_graph_drives_proposals_over_mcp(env: Any) -> None:
    _app, engine, invocation, monkeypatch = env
    ask = McpAsk(invocation, monkeypatch)
    plan = ask(
        "ask", "spec.query", {"question": "next_unblocked_task", "subject": "phase-1"}
    )
    assert plan["ok"] is True, plan
    mode = ask("ask", "project.mode.get", {"repository": "repo"})
    assert mode["result"]["mode"] == "reference"
    proposals = ask("find", "development.proposal.list", {"repository": "repo"})
    assert proposals["result"]["items"][0]["id"] == "proposal:1"
    decided = ask(
        "act",
        "development.proposal.review",
        {"proposal_id": "proposal:1", "decision": "reject", "reason": "no"},
        idempotency_key="m1",
    )
    assert decided["result"]["state"] == "rejected"


@pytest.mark.spec("ARBORA-OPS-R896.1")
@pytest.mark.parametrize(
    "question",
    ["task_context", "plan_section", "contract", "seam_routing", "plan_to_goal_phase"],
)
def test_plan_and_contract_questions_take_a_subject(env: Any, question: str) -> None:
    app, engine, *_ = env
    answer = _value(_call(app, "spec.query", {"question": question, "subject": "n1"}))
    assert answer["digests"] == ["abc"]
    assert engine.calls[-1][1]["question"] == question
    assert _call(app, "spec.query", {"question": question}).status_code == 400


@pytest.mark.spec("ARBORA-OPS-R896.5")
def test_accept_forwards_the_two_branches_and_nothing_else(env: Any) -> None:
    app, engine, *_ = env
    review = {
        "proposal_id": "proposal:1",
        "decision": "accept",
        "reason": "evidence holds",
        "pull_request": {"head": "change/x", "base": "main"},
    }
    _value(_call(app, "development.proposal.review", review, **{KEY: "r9"}))
    sent = [p for n, p in engine.calls if n == "ProposalDecide"][0]["request"]
    assert sent["pull_request"] == {"head": "change/x", "base": "main"}
    wider = {**review, "pull_request": {"head": "a", "base": "b", "server": "s"}}
    assert (
        _call(app, "development.proposal.review", wider, **{KEY: "r10"}).status_code
        == 400
    )


@pytest.mark.spec("ARBORA-OPS-R896.2")
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["ProjectModeGet", "DashboardAsset", "SpecQuestion"])
async def test_unreleased_engine_methods_refuse_unavailable(method: str) -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops._engine import engine_send

    with pytest.raises(OperationRefused) as refused:
        await engine_send(None, method, {})
    assert refused.value.code == "UNAVAILABLE"


class RunEngine:
    """The engine's delivery records; a run is a delivery owned by the opening principal."""

    def __init__(self, modes: dict[str, str]) -> None:
        self.modes = modes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.calls_order: list[str] = []
        self.task_plan: Any = None
        self.owners: dict[str, str] = {
            "run:theirs": "person:two",
            "run:mine": "person:one",
        }

    def open(self, params: dict[str, Any], who: str) -> Any:
        self.calls.append(("DeliveryOpen", params))
        self.calls_order.append("DeliveryOpen")
        self.owners["run:mine"] = who
        return {
            "delivery": {"id": "run:mine"},
            "would_dispatch": None,
            "reopened": False,
        }

    def status(self, params: dict[str, Any], who: str) -> Any:
        self.calls.append(("DeliveryStatus", params))
        run = params["request"]["delivery_id"]
        # A foreign delivery answers exactly like an absent one.
        if self.owners.get(run) != who:
            raise EngineResponseError("DELIVERY_NOT_FOUND", "no such delivery")
        return {
            "stage": "working",
            "mandate": {"scope_rows": ["T001", "T002"]},
            "proposals": [{"id": "p1", "state": "open"}],
            "assignments": [{"id": "a1", "report": {"evidence": ["d1"]}}],
        }


class TaskPlanDouble:
    """The engine ``TaskPlan`` decision: one sealed record per question, in call order."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.requests: list[dict[str, Any]] = []

    def build_request(
        self, tenant: str, question_id: str, proposal: dict[str, Any], schema: Any
    ) -> dict[str, Any]:
        return {"tenant": tenant, "question_id": question_id, "proposal": proposal}

    async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self.order.append("Decide")
        self.requests.append(request)
        return {"records": [{"record_id": f"decision:tp:{len(self.requests)}"}]}

    def verify(self, record: Any, request: Any, *, expected_principal: str) -> Any:
        return {"plan_digest": "p"}

    async def publish(self, request: Any, key: str) -> Any:
        raise AssertionError("a run start never publishes a plan")


@pytest.fixture
def runs_env(stack: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    from arbora.api.ops import plans

    app, client = served
    engine = LoopbackEngine()
    runs = RunEngine(engine.modes)
    task_plan = TaskPlanDouble(runs.calls_order)
    monkeypatch.setattr(plans, "engine_task_plan", lambda *_a, **_k: task_plan)

    async def pin(_context: Any, _tenant: str) -> dict[str, Any]:
        return {"digest": "pin"}

    monkeypatch.setattr(plans, "_feature_schema", pin)
    runs.task_plan = task_plan
    route_methods(
        client,
        {
            "ProjectModeGet": engine.mode_get,
            "ProjectModeSet": engine.mode_set,
            "ProposalList": engine.proposal_list,
            "ProposalDecide": engine.proposal_decide,
            "GetNodeProperties": engine.node,
            "SpecQuestion": engine.question,
            "DeliveryOpen": runs.open,
            "DeliveryStatus": runs.status,
        },
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("lifecycle:write",))},
    )
    return app, engine, runs, stack["invocation"], monkeypatch


@pytest.mark.spec("ARBORA-OPS-R896.3")
def test_run_start_returns_run_id_and_refuses_reference_mode(runs_env: Any) -> None:
    app, engine, runs, *_ = runs_env
    start = {"repository": "repo", "phase": "phase-1"}
    refused = _call(app, "development.run.start", start, **{KEY: "s1"})
    assert refused.json()["error"]["code"] == "PROJECT_MODE_REFERENCE"
    assert [n for n, _ in runs.calls] == []
    engine.modes["repo"] = "development"
    done = _value(_call(app, "development.run.start", start, **{KEY: "s2"}))
    assert done == {"run_id": "run:mine"}
    sent = runs.calls[0][1]["request"]
    # Arbora names no task: the slice is the engine's own next_unblocked_task answer.
    assert sent["scope_rows"] == ["T001"]
    assert sent["evidence_digests"] == ["abc"]
    assert sent["repo"] == "repo" and sent["origin"] == "requested"
    assert sent["slice_root"] == "phase-1" and sent["run_trace_id"].startswith("run:")
    nothing = _call(
        app, "development.run.start", {**start, "task_id": "T9"}, **{KEY: "s3"}
    )
    assert nothing.json()["error"]["code"] == "NOT_FOUND"
    assert len(runs.calls) == 1
    assert _call(app, "development.run.start", start).status_code == 400


@pytest.mark.spec("ARBORA-OPS-R896.3")
def test_run_start_opens_the_delivery_under_the_engine_task_plan_record(
    runs_env: Any,
) -> None:
    app, engine, runs, *_ = runs_env
    engine.modes["repo"] = "development"
    start = {"repository": "repo", "phase": "phase-1"}
    _value(_call(app, "development.run.start", start, **{KEY: "d1"}))
    # The TaskPlan question is asked first, over the engine's own slice, and its record id
    # (never a digest arbora computed) is the delivery's dispatch decision.
    assert runs.calls_order == ["Decide", "DeliveryOpen"]
    (asked,) = runs.task_plan.requests
    assert [t["task_id"] for t in asked["proposal"]["tasks"]] == ["T001"]
    sent = runs.calls[0][1]["request"]
    assert sent["dispatch_decision_id"] == "decision:tp:1"
    assert sent["dispatch_decision_id"] not in sent["evidence_digests"]


@pytest.mark.spec("ARBORA-OPS-R896.4")
def test_run_status_reports_evidence_and_hides_other_tenants(runs_env: Any) -> None:
    app, _engine, runs, *_ = runs_env
    status = _value(_call(app, "development.run.status", {"run_id": "run:mine"}))
    assert status["state"] == "working"
    assert status["scope_rows"] == ["T001", "T002"] and status["evidence"] == ["d1"]
    assert status["proposals"] == [{"id": "p1", "state": "open"}]
    foreign = _call(app, "development.run.status", {"run_id": "run:theirs"})
    absent = _call(app, "development.run.status", {"run_id": "run:nope"})
    assert foreign.json()["error"]["code"] == "RUN_NOT_FOUND"
    # A foreign run answers byte-for-byte like an absent one (only the request id differs).
    assert foreign.status_code == absent.status_code == 404
    drop = {"request_id"}
    assert {k: v for k, v in foreign.json()["error"].items() if k not in drop} == {
        k: v for k, v in absent.json()["error"].items() if k not in drop
    }
    assert {n for n, _ in runs.calls} == {"DeliveryStatus"}


@pytest.mark.spec("ARBORA-OPS-R896.6")
def test_artifact_graph_drives_a_run_over_mcp(runs_env: Any) -> None:
    _app, engine, runs, invocation, monkeypatch = runs_env
    ask = McpAsk(
        invocation, monkeypatch, person("one", extra_scopes=("lifecycle:write",))
    )
    next_task = ask(
        "ask", "spec.query", {"question": "next_unblocked_task", "subject": "phase-1"}
    )
    assert next_task["ok"] is True, next_task
    moved = ask(
        "manage",
        "project.mode.set",
        {"repository": "repo", "mode": "development"},
        idempotency_key="j1",
    )
    plan_ref = moved["meta"]["plan"]["plan_ref"]
    done = ask(
        "manage",
        "project.mode.set",
        {"repository": "repo", "mode": "development"},
        idempotency_key="j1",
        plan_ref=plan_ref,
        execute=True,
    )
    assert done["result"]["mode"] == "development", done
    started = ask(
        "act",
        "development.run.start",
        {"repository": "repo", "phase": "phase-1"},
        idempotency_key="j2",
    )
    assert started["result"] == {"run_id": "run:mine"}, started
    status = ask("ask", "development.run.status", {"run_id": "run:mine"})
    assert status["result"]["scope_rows"] == ["T001", "T002"]
    proposals = ask("find", "development.proposal.list", {"repository": "repo"})
    assert proposals["result"]["items"][0]["id"] == "proposal:1"
    accepted = ask(
        "act",
        "development.proposal.review",
        {
            "proposal_id": "proposal:1",
            "decision": "accept",
            "reason": "evidence holds",
            "pull_request": {"head": "change/x", "base": "main"},
        },
        idempotency_key="j3",
    )
    assert accepted["result"]["state"] == "approved"
    reviews = [p for n, p in engine.calls if n == "ProposalDecide"]
    assert len(reviews) == 1 and "specs/" not in str(reviews[0])
    assert {n for n, _ in engine.calls} <= {
        "SpecQuestion",
        "ProjectModeGet",
        "ProjectModeSet",
        "ProposalList",
        "ProposalDecide",
        "GetNodeProperties",
    }
    assert [n for n, _ in runs.calls] == ["DeliveryOpen", "DeliveryStatus"]
