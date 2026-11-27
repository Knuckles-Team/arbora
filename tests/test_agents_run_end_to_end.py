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
from meristem.host_ports import HostPorts

from arbora import agent_runs
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import OrchestratorA2ARouter
from arbora.a2a.service import A2AService
from arbora.agent_host.host_port_adapters import HostPortsBinding, HostPortsSlot
from arbora.api.invoke import OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.ops import agents
from arbora.api.registry import Registry, Surface
from arbora.core.settings import runtime_config
from arbora.mcp_server import runtime as mcp_runtime
from arbora.run_origin import RunOriginRegistry
from tests.a2a._route_support import bind_route_decision
from tests.a2a._support import streaming_model
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
        row = self.rows.get(work_item_id)
        # The engine scopes a read to the verified tenant: another tenant's
        # item answers like an absent one.
        return None if row is None or tenant != "tenant:one" else dict(row)

    def list(self, **request: Any) -> dict[str, Any]:
        """One page of the tenant's items of one kind that match the metadata."""
        match = request.get("metadata_match") or {}
        items = [
            dict(row)
            for row in self.rows.values()
            if request["tenant"] == "tenant:one"
            and row["kind"] == request["kind"]
            and all(row["metadata"].get(k) == v for k, v in match.items())
        ]
        return {"items": items[: request["limit"]], "next_cursor": None}

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        row = self.rows[request["work_item_id"]]
        if row["status"] != "ready":
            return {"claimed": False}
        row["status"] = "leased"
        self.leases.append(request["work_item_id"])
        token = self.leases.count(request["work_item_id"])
        row["lease"] = (request["worker_ref"], 1, token)
        return {
            "claimed": True,
            "lease_holder_ref": request["worker_ref"],
            "lease_epoch": 1,
            "fencing_token": token,
        }

    def cas_metadata(self, **request: Any) -> dict[str, Any]:
        """Replace metadata when status, metadata and the lease fence all match."""
        row = self.rows.get(request["work_item_id"])
        if row is None:
            return {"outcome": "not_found"}
        lease = request.get("expected_lease")
        fenced = lease is None or row.get("lease") == (
            lease["worker_ref"],
            lease["lease_epoch"],
            lease["fencing_token"],
        )
        if (
            row["status"] not in request["expected_status"]
            or not fenced
            or row["metadata"] != request["expected_metadata"]
        ):
            return {"outcome": "conflict"}
        row["metadata"] = dict(request["set_metadata"])
        return {"outcome": "applied"}

    def commit_result(self, **request: Any) -> dict[str, Any]:
        self.rows[request["work_item_id"]]["status"] = request["outcome"]
        self.outcomes.append(request["outcome"])
        return {"status": request["outcome"]}


class FakeEngine:
    def __init__(self) -> None:
        self.work_items = FakeWorkItems()
        self.events: list[dict[str, Any]] = []
        #: The engine KV store: (namespace, key) -> (value, version).
        self.kv: dict[tuple[str, str], tuple[bytes, int]] = {}

    async def _send(
        self,
        method: str,
        params: Any,
        graph: str | None,
        **_kwargs: Any,
    ) -> Any:
        """The versioned KV methods the A2A event log uses."""
        slot = (params["namespace"], params["key"])
        stored = self.kv.get(slot)
        if method == "KvGet":
            return (
                None if stored is None else {"value": stored[0], "version": stored[1]}
            )
        assert method == "KvCas", method
        version = 0 if stored is None else stored[1]
        if params["expected_version"] != version:
            return {"swapped": False, "version": version}
        self.kv[slot] = (params["new"], version + 1)
        return {"swapped": True, "version": version + 1}

    def batch_typed_mutations(self, mutations: list[dict[str, Any]]) -> bool:
        self.events.extend(mutations)
        return True

    def query_cypher(self, query: str, params: dict[str, Any]) -> list[Any]:
        assert "MATCH (e) WHERE e.type = 'RunEvent'" in query
        return [
            {"payload": event["properties"]["payload"]}
            for event in self.events
            if event["node_type"] == "RunEvent"
            and event["properties"]["run_id"] == params["run_id"]
            and event["properties"]["task_id"] == params["run_id"]
            and event["properties"]["event_type"] == params["event_type"]
        ][:2]


class ServedRuntime(FixtureRuntime):
    """The fixture identity runtime with the production composite dispatch."""

    def __init__(self, a2a: A2AService) -> None:
        super().__init__()
        self.bindings = {"a2a": a2a}

    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        return await BoundOperationRuntime.dispatch(self, op, params, context)  # type: ignore[arg-type]


#: The host's ports slot every authority in this module runs its agent on.
PORTS = HostPortsSlot()


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    registry = tmp_path / "models.json"
    registry.write_text(json.dumps({"default": "test"}), encoding="utf-8")
    monkeypatch.setitem(runtime_config._overrides, "model_registry_path", str(registry))
    fake = FakeEngine()
    monkeypatch.setattr(mcp_runtime, "_get_engine", lambda: fake)
    PORTS.bind(
        HostPortsBinding(
            HostPorts(
                decide=agent_runs.decide_run,
                models=agent_runs.resolve_model,
                events=agent_runs.record_run_event,
            ),
            None,
        )
    )
    return fake


@pytest.mark.usefixtures("default_agent_registered")
@pytest.mark.spec("ARBORA-HOST-R001.2.2")
def test_served_agents_run_executes_through_a2a_work_items_and_the_harness(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def library_entry(_context: Any, _agent: str) -> dict[str, Any]:
        # The named agent's graph definition: it uses no definition-bound skill.
        return {"skills": []}

    monkeypatch.setattr("arbora.api.ops.agent_library.current_entry", library_entry)
    service = A2AService(
        authority=WorkItemA2AAuthority(
            lambda: engine, origins=RunOriginRegistry(), ports=PORTS
        ),
        router=OrchestratorA2ARouter(lambda: engine),
    )
    runtime = ServedRuntime(service)
    services, *_ = setup_services(
        registry=Registry(agents.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    caller = make_verified_caller("kg:write")(
        session=session(frozenset({"kg:write"})),
        engine_claims={
            "principal": "person:one",
            "tenant": "tenant:one",
            "scopes": ["kg:write"],
            "policy_version": "revision:one",
            "delegation": False,
            "session_ref": "session-7",
        },
    )
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
    # The item carries the opening session (ARBORA-IDENTITY-R026.3).
    stored = engine.work_items.rows[item_id]["metadata"]
    assert stored["a2a_client_session_id"] == "session-7"
    assert stored["a2a_client_principal"] == "person:one"
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
        "combined_limit",
        "combined_oversize",
    ],
)
def test_served_process_runs_an_agent_over_its_http_a2a_route(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, attachment_case: str
) -> None:
    """Boot the served factory the ``arbora`` entry point runs, then POST A2A."""
    import httpx

    from arbora.security import request_identity
    from arbora.security.actor_context import ActorContext, ActorType

    monkeypatch.setitem(
        runtime_config._overrides, "auth_jwt_audience", "arbora-fixture"
    )
    monkeypatch.setitem(
        runtime_config._overrides, "kg_policy_version", "policy:fixture"
    )

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

    observed: list[Any] = []

    def model(messages: Any, info: Any) -> ModelResponse:
        observed.extend(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        return ModelResponse(parts=[TextPart("received")])

    parts: list[dict[str, Any]] = [{"kind": "text", "text": "say hello"}]
    if attachment_case != "none":
        content = b"attached text: maple"
        if attachment_case in {"combined_limit", "combined_oversize"}:
            parts[0]["text"] = "say hello" + "a" * (65_536 - len("say hello"))
            content = b"b" * (34_463 if attachment_case == "combined_limit" else 34_464)
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
            monkeypatch.setitem(runtime_config._overrides, "max_upload_size", 4)
        if attachment_case == "aggregate":
            file["bytes"] = base64.b64encode(b"a" * 40_000).decode("ascii")
        count = (
            9
            if attachment_case == "too_many"
            else (2 if attachment_case == "aggregate" else 1)
        )
        parts.extend({"kind": "file", "file": file} for _ in range(count))

    bind_route_decision(monkeypatch)
    from arbora.mcp_server.host_context import build_host_context

    host = build_host_context(mcp_runtime._get_engine)
    host.runs.ports.bind(
        HostPortsBinding(
            HostPorts(
                decide=agent_runs.decide_run,
                models=lambda _id: streaming_model(model),
                events=agent_runs.record_run_event,
            ),
            None,
        )
    )
    _args, mcp, _middlewares = mcp_runtime._build_server(bootstrap=False, host=host)
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

    if attachment_case not in {"none", "text", "combined_limit"}:
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
    terminal = json.loads(engine.events[-1]["properties"]["payload"])
    assert body["result"]["metadata"]["arbora"]["usage"] == {
        "runId": body["result"]["metadata"]["arbora"]["runId"],
        "taskId": body["result"]["id"],
        "contextId": body["result"]["contextId"],
        "scope": "turn",
        "counts": terminal["result"]["usage"],
    }
    assert engine.work_items.outcomes == ["succeeded"]
    assert [
        json.loads(event["properties"]["payload"])["type"]
        for event in engine.events
        if event["node_type"] == "RunEvent"
    ] == ["run_started", "node_transition", "graph_complete"]


@pytest.mark.spec("ARBORA-A2A-R001")
@pytest.mark.asyncio
async def test_turn_usage_is_correlated_and_not_reconstructed_on_replay(
    engine: FakeEngine,
) -> None:
    from dataclasses import replace

    from arbora.a2a.models import A2AMessage
    from arbora.security.session import use_session

    authority = WorkItemA2AAuthority(
        lambda: engine, origins=RunOriginRegistry(), ports=PORTS
    )
    service = A2AService(
        authority=authority, router=OrchestratorA2ARouter(lambda: engine)
    )
    message = A2AMessage.model_validate(
        {
            "role": "user",
            "messageId": "usage-turn",
            "parts": [{"kind": "text", "text": "hello"}],
            "metadata": {"agent_name": "helper"},
        }
    )
    with use_session(session(frozenset({"kg:read", "kg:write"}))):
        task = await service.send_message(message=message, idempotency_key="usage-1")
        replay = await service.send_message(message=message, idempotency_key="usage-1")
        read = await authority.get(task.id)
    other = session(frozenset({"kg:read", "kg:write"}))
    other = replace(other, actor=replace(other.actor, actor_id="person:other"))
    with use_session(other):
        assert await authority.get(task.id) is None
    terminal = json.loads(engine.events[-1]["properties"]["payload"])
    assert task.metadata["arbora"]["usage"] == {
        "runId": task.metadata["arbora"]["runId"],
        "taskId": task.id,
        "contextId": task.context_id,
        "scope": "turn",
        "counts": terminal["result"]["usage"],
    }
    assert "usage" not in replay.metadata["arbora"]
    assert read is not None and "usage" not in read.metadata["arbora"]
    assert engine.work_items.outcomes == ["succeeded"]


@pytest.mark.spec("ARBORA-A2A-R001")
@pytest.mark.asyncio
async def test_foreign_run_result_never_projects_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arbora import work_items

    outcomes: list[str] = []

    class ForeignExecutor:
        async def execute_agent(self, request: Any, *, session: Any) -> Any:
            return SimpleNamespace(
                run_id="foreign-run", output="private", usage={"input_tokens": 42}
            )

    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: {"claim": 1})
    monkeypatch.setattr(
        work_items, "commit_result", lambda *a, **k: outcomes.append(k["outcome"])
    )
    authority = WorkItemA2AAuthority(
        lambda: None, executor=ForeignExecutor(), origins=RunOriginRegistry()
    )
    usage = await authority._run(
        object(),
        item_id="native-item",
        session=object(),
        agent_name="helper",
        task="hello",
        task_id="task",
        run_id="admitted-run",
    )
    assert usage is None
    assert outcomes == ["failed"]
