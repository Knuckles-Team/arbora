"""A served ``agents.run`` call runs end to end.

Admission and the invoke pipeline admit the call, the A2A service routes it,
an engine WorkItem is admitted and leased, the harness run loop executes the
agent with a fake model, the outcome is committed, and every run event is
written to the engine. The engine is a fake client with the WorkItem and
typed-batch methods only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from agent_utilities import host_ports

from arbora import agent_runs
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import OrchestratorA2ARouter
from arbora.a2a.service import A2AService
from arbora.api.invoke import OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.ops import agents
from arbora.api.registry import Registry, Surface
from arbora.core.settings import runtime_config
from arbora.mcp_server import runtime as mcp_runtime
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


@pytest.mark.spec("ARBORA-HOST-R001.2.2")
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


@pytest.mark.spec("ARBORA-HOST-R001.2.2", "ARBORA-A2A-R001.5.1")
@pytest.mark.parametrize(
    "attachment_case",
    [
        "none",
        "text",
        "oversize",
        "unsupported",
        "uri",
        "invalid_utf8",
        "configured_limit",
        "too_many",
        "aggregate",
        "invalid_base64",
        "empty",
    ],
)
def test_served_process_runs_an_agent_over_its_http_a2a_route(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, attachment_case: str
) -> None:
    """Boot the served factory the ``arbora`` entry point runs, then POST A2A."""
    import httpx

    import arbora.a2a.mcp as a2a_mcp
    from arbora.security import request_identity
    from arbora.security.actor_context import ActorContext, ActorType

    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")

    async def credential_owner(credential: str) -> ActorContext:
        assert credential == "fixture-verified"  # sanitizer:ignore - synthetic
        return ActorContext(
            actor_id="person:one",
            actor_type=ActorType.HUMAN,
            tenant_id="tenant:one",
            authenticated=True,
            roles=("kg:write",),
            credential_expires_at=int(time.time()) + 3600,
        )

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    observed: list[Any] = []

    def model(messages: Any, info: Any) -> ModelResponse:
        observed.extend(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        return ModelResponse(parts=[TextPart("received")])

    host_ports.register_model_provider(lambda _id: FunctionModel(model))
    parts: list[dict[str, Any]] = [{"kind": "text", "text": "say hello"}]
    if attachment_case != "none":
        content = b"attached text: maple"
        if attachment_case == "oversize":
            content = b"x" * 65_537
        elif attachment_case == "invalid_utf8":
            content = b"\xff"
        file: dict[str, Any] = {
            "bytes": base64.b64encode(content).decode("ascii"),
            "mimeType": "image/png"
            if attachment_case == "unsupported"
            else "text/plain",
            "name": "../../never-read-this.txt",
        }
        if attachment_case == "invalid_base64":
            file["bytes"] = "!!!"
        elif attachment_case == "empty":
            file["bytes"] = ""
        if attachment_case == "uri":
            file = {"uri": "file:///never-read-this", "mimeType": "text/plain"}
        if attachment_case == "configured_limit":
            monkeypatch.setattr(runtime_config, "max_upload_size", 4)
        if attachment_case == "aggregate":
            file["bytes"] = base64.b64encode(b"a" * 40_000).decode("ascii")
        count = (
            9
            if attachment_case == "too_many"
            else (2 if attachment_case == "aggregate" else 1)
        )
        parts.extend({"kind": "file", "file": file} for _ in range(count))

    monkeypatch.setattr(a2a_mcp, "_SERVICE", None)
    _args, mcp, _middlewares = mcp_runtime._build_server(bootstrap=False)
    app = mcp.http_app()

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            return await client.post(
                "/a2a",
                headers={
                    "Authorization": "Bearer fixture-verified",
                    "X-Request-Id": "request:served",
                    "Idempotency-Key": "served-run-1",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "run:one",
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "messageId": "message-served",
                            "parts": parts,
                            "metadata": {"agent_name": "helper"},
                        }
                    },
                },
            )

    response = asyncio.run(call())

    if attachment_case not in {"none", "text"}:
        assert "error" in response.json(), response.text
        assert engine.work_items.rows == {}
        assert observed == []
        return
    assert response.status_code == 200, response.text
    assert any("say hello" in str(value) for value in observed)
    if attachment_case == "text":
        assert any("attached text: maple" in str(value) for value in observed)
    body = response.json()
    assert "error" not in body, body
    assert body["result"]["status"]["state"] == "completed"
    assert engine.work_items.outcomes == ["succeeded"]
    assert [
        json.loads(event["properties"]["payload"])["type"]
        for event in engine.events
        if event["node_type"] == "RunEvent"
    ] == ["run_started", "node_transition", "graph_complete"]
