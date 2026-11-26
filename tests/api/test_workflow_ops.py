"""Workflow operations through the business-process connector (ARBORA-OPS-R913)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import operator_surface as surface
from tests.api._engine_double import context

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
    fleet = Fleet({"run": {"run_id": "run-7", "state": "queued"}})
    monkeypatch.setattr(surface, "invoke_registered", fleet)
    reply = await surface.handle_workflow(
        context(), {"workflow_id": "w", "inputs": {"a": 1}}, _OPS["workflows.run"]
    )
    assert reply == {"value": {"run_id": "run-7"}}
    tool, arguments = fleet.calls[0]
    assert tool == "run" and arguments["server"] == surface.WORKFLOW_SERVER
    assert arguments["arguments"] == {"workflow_id": "w", "inputs": {"a": 1}}
    # A connector that returns no run id has no run machine entry to name.
    fleet.replies["run"] = {"state": "queued"}
    with pytest.raises(OperationRefused) as refused:
        await surface.handle_workflow(
            context(), {"workflow_id": "w", "inputs": {}}, _OPS["workflows.run"]
        )
    assert refused.value.code == "UNAVAILABLE"


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
