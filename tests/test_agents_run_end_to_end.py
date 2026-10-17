"""A served ``agents.run`` call runs end to end.

Admission and the invoke pipeline admit the call, the A2A service routes it,
an engine WorkItem is admitted and leased, the harness run loop executes the
agent with a fake model, the outcome is committed, and every run event is
written to the engine. The engine is a fake client with the WorkItem and
typed-batch methods only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agent_utilities import host_ports

from graph_os import agent_runs
from graph_os.a2a.authority import WorkItemA2AAuthority
from graph_os.a2a.routing import OrchestratorA2ARouter
from graph_os.a2a.service import A2AService
from graph_os.api.invoke import OpResult, invoke
from graph_os.api.invoke.executor import BoundOperationRuntime
from graph_os.api.ops import agents
from graph_os.api.registry import Registry, Surface
from graph_os.core.settings import runtime_config
from graph_os.mcp_server import runtime as mcp_runtime
from tests.a2a.test_operation_bridge import session
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import FixtureRuntime, setup_services


class FakeWorkItems:
    """The engine ``work_items`` namespace: rows, one lease, outcomes."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.leases: list[str] = []
        self.outcomes: list[str] = []

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        item_id = request["work_item_id"]
        created = item_id not in self.rows
        self.rows.setdefault(
            item_id,
            {
                "work_item_id": item_id,
                "kind": request["kind"],
                "status": "ready",
                "metadata": dict(request["metadata"]),
            },
        )
        return {"work_item_id": item_id, "created": created}

    def get(self, *, tenant: str, work_item_id: str) -> dict[str, Any] | None:
        assert tenant == "tenant:one"
        row = self.rows.get(work_item_id)
        return None if row is None else dict(row)

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        row = self.rows[request["work_item_id"]]
        if row["status"] != "ready":
            return {"claimed": False}
        row["status"] = "leased"
        self.leases.append(request["work_item_id"])
        return {
            "claimed": True,
            "lease_holder_ref": request["worker_ref"],
            "lease_epoch": 1,
            "fencing_token": 1,
        }

    def commit_result(self, **request: Any) -> dict[str, Any]:
        self.rows[request["work_item_id"]]["status"] = request["outcome"]
        self.outcomes.append(request["outcome"])
        return {"status": request["outcome"]}


class FakeEngine:
    def __init__(self) -> None:
        self.work_items = FakeWorkItems()
        self.events: list[dict[str, Any]] = []

    def batch_typed_mutations(self, mutations: list[dict[str, Any]]) -> bool:
        self.events.extend(mutations)
        return True


class ServedRuntime(FixtureRuntime):
    """The fixture identity runtime with the production composite dispatch."""

    def __init__(self, a2a: A2AService) -> None:
        super().__init__()
        self.bindings = {"a2a": a2a}

    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        return await BoundOperationRuntime.dispatch(self, op, params, context)  # type: ignore[arg-type]


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    registry = tmp_path / "models.json"
    registry.write_text(json.dumps({"default": "test"}), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "model_registry_path", str(registry))
    fake = FakeEngine()
    monkeypatch.setattr(mcp_runtime, "_get_engine", lambda: fake)
    agent_runs.bind_run_ports()
    yield fake
    host_ports.register_run_decision_provider(None)
    host_ports.register_model_provider(None)
    host_ports.register_run_event_sink(None)


@pytest.mark.spec("GRAPHOS-HOST-R001.2.2")
def test_served_agents_run_executes_through_a2a_work_items_and_the_harness(
    engine: FakeEngine,
) -> None:
    service = A2AService(
        authority=WorkItemA2AAuthority(lambda: engine),
        router=OrchestratorA2ARouter(lambda: engine),
    )
    runtime = ServedRuntime(service)
    services, *_ = setup_services(
        registry=Registry(agents.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
        native_idempotency=agents.NATIVE_IDEMPOTENCY,
    )
    caller = make_verified_caller("kg:write")(session=session(frozenset({"kg:write"})))
    message = {
        "role": "user",
        "messageId": "message-1",
        "parts": [{"kind": "text", "text": "say hello"}],
        "metadata": {"agent_name": "helper"},
    }

    result = asyncio.run(
        invoke(
            "agents.run",
            {"message": message},
            caller,
            Surface.HTTP,
            services=services,
            idempotency_key="run-1",
        )
    )

    assert isinstance(result, OpResult), result
    task = result.value["value"]
    assert task["status"]["state"] == "completed"
    item_id = f"workitem:orchestrator:{task['id']}"
    assert engine.work_items.leases == [item_id]
    assert engine.work_items.outcomes == ["succeeded"]
    run_events = [
        json.loads(event["properties"]["payload"])["type"]
        for event in engine.events
        if event["node_type"] == "RunEvent"
    ]
    assert run_events == ["run_started", "node_transition", "graph_complete"]
