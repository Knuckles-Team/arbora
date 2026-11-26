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
