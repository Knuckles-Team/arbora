"""Workflow operations through the business-process connector (ARBORA-OPS-R913)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import operator_surface as surface
from tests.api._engine_double import EngineDouble, context

pytestmark = pytest.mark.spec("ARBORA-OPS-R913")
_OPS = {op.id: op for op in surface.operations()}


class Fleet:
    """The fleet call door: one connector reply per tool, recorded."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, _ctx: Any, op_id: str, arguments: dict[str, Any]) -> Any:
        assert op_id == "fleet.call"
        self.calls.append((arguments["tool"], arguments))
        return {"value": self.replies[arguments["tool"]]}


async def test_run_returns_a_run_id_in_the_run_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    engine.serve("SubmitWorkItem", lambda p: {"work_item_id": "run-7", "created": True})
    engine.install(surface)

    async def graph(_context: Any) -> str:
        return "graph:one"

    monkeypatch.setattr("arbora.api.invoke.bootstrap.caller_dispatch_graph", graph)
    reply = await surface.handle_workflow(
        context(), {"workflow_id": "w", "inputs": {"a": 1}}, _OPS["workflows.run"]
    )
    assert reply == {"value": {"run_id": "run-7"}}
    method, params, idempotent = engine.sent[0]
    request = params["request"]
    assert method == "SubmitWorkItem" and idempotent is True
    assert request["kind"] == "workflow.run" and request["idempotency_key"] == "k"
    assert request["metadata"] == {"workflow_id": "w", "inputs": {"a": 1}}
    assert request["context"]["tenant_id"] == "tenant:one"
    assert request["context"]["graph"] == "graph:one"
    # An engine that returns no work item id has no run machine entry to name.
    engine.serve("SubmitWorkItem", lambda p: {"created": True})
    with pytest.raises(OperationRefused) as refused:
        await surface.handle_workflow(
            context(), {"workflow_id": "w", "inputs": {}}, _OPS["workflows.run"]
        )
    assert refused.value.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as keyless:
        await surface.handle_workflow(
            context(key=""), {"workflow_id": "w", "inputs": {}}, _OPS["workflows.run"]
        )
    assert keyless.value.code == "INVALID_ARGUMENT"


async def test_list_and_create_pass_the_connector_answer_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = Fleet({"list": {"workflows": [{"id": "w"}]}, "create": {"id": "w2"}})
    monkeypatch.setattr(surface, "invoke_registered", fleet)
    listed = await surface.handle_workflow(
        context(), {"limit": 5, "cursor": None}, _OPS["workflows.list"]
    )
    assert listed["value"]["workflows"] == [{"id": "w"}]
    assert fleet.calls[0][1]["arguments"] == {"limit": 5}
    created = await surface.handle_workflow(
        context(), {"definition": {"name": "n"}}, _OPS["workflows.create"]
    )
    assert created["value"] == {"id": "w2"}


async def test_a_fleet_without_the_connector_tool_refuses_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def absent(_ctx: Any, _op_id: str, arguments: dict[str, Any]) -> Any:
        return {"value": {"error": f"'{arguments['server']}' is not in the catalog"}}

    monkeypatch.setattr(surface, "invoke_registered", absent)
    for op_id, params in (
        ("workflows.list", {"limit": 5, "cursor": None}),
        ("workflows.create", {"definition": {}}),
    ):
        with pytest.raises(OperationRefused) as refused:
            await surface.handle_workflow(context(), params, _OPS[op_id])
        assert refused.value.code == "UNAVAILABLE"
        assert surface.WORKFLOW_SERVER in refused.value.details["reason"]


class _Fleet:
    def __init__(self, *, serves: bool = True, fail: str | None = None) -> None:
        self.serves, self.fail = serves, fail
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def search(self, query: str) -> list[dict[str, Any]]:
        return [{"server": "business-process", "name": "run"}] if self.serves else []

    async def call(self, server: str, tool: str, arguments: Any) -> Any:
        from arbora.modality.fleet_port import FleetCallFailed

        self.calls.append((server, tool, dict(arguments)))
        if self.fail:
            raise FleetCallFailed(self.fail)
        return {"ok": True}


class _Engine:
    def __init__(self, status: str = "ready") -> None:
        self.status = status
        self.commits: list[dict[str, Any]] = []
        self.work_items = self

    def get(self, **_: Any) -> dict[str, Any]:
        return {
            "work_item_id": "run-7",
            "kind": "workflow.run",
            "status": self.status,
            "metadata": {"workflow_id": "w", "inputs": {"a": 1}},
        }

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"claimed": True, "lease_holder_ref": request["worker_ref"]}

    def commit_result(self, **kwargs: Any) -> dict[str, Any]:
        self.commits.append(kwargs)
        return {"status": kwargs["outcome"]}


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "arbora.security.session.current_session",
        lambda: SimpleNamespace(tenant="tenant:one"),
    )


@pytest.mark.spec("ARBORA-OPS-R913")
async def test_worker_claims_calls_the_connector_and_commits(session: None) -> None:
    from arbora.workflow_worker import WorkflowWorker

    engine, fleet = _Engine(), _Fleet()
    worker = WorkflowWorker(engine, fleet, worker_ref="w1")
    assert await worker.run("run-7") == "committed"
    assert fleet.calls == [
        ("business-process", "run", {"workflow_id": "w", "inputs": {"a": 1}})
    ]
    assert engine.commits[0]["outcome"] == "succeeded"
    assert engine.commits[0]["result_ref"].startswith("sha256:")
    failing = _Engine()
    refused = WorkflowWorker(failing, _Fleet(fail="UNAVAILABLE"), worker_ref="w1")
    assert await refused.run("run-7") == "committed"
    assert failing.commits[0]["outcome"] == "failed"
    assert failing.commits[0]["error_ref"] == "fleet:UNAVAILABLE"
    done = _Engine("succeeded")
    assert await WorkflowWorker(done, _Fleet(), worker_ref="w1").run("run-7") == "noop"
    assert done.commits == []


@pytest.mark.spec("ARBORA-OPS-R913")
async def test_worker_refuses_unavailable_while_the_fleet_lacks_the_tool(
    session: None,
) -> None:
    from arbora.workflow_worker import WorkflowWorker

    engine = _Engine()
    with pytest.raises(OperationRefused) as refused:
        await WorkflowWorker(engine, _Fleet(serves=False), worker_ref="w1").run("run-7")
    assert refused.value.code == "UNAVAILABLE"
    assert engine.commits == []
