"""The served process mounts the operation-invoke API the web UI calls.

The test composes the served invocation the ``graph-os`` entry point composes,
installs it as the gateway application, mounts the graph routes the web UI
co-service mounts, and then calls over HTTP each operation shape the web UI
host parses: ``POST /api/v1/ops/<op_id>`` returning ``{"ok": true, "result":
{"value": ...}}``. The engine is a fake client: WorkItems, typed batches and
the generated method transport only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_utilities import host_ports
from fastapi import FastAPI

from graph_os import agent_runs
from graph_os.a2a import mcp as a2a_mcp
from graph_os.a2a.authority import WorkItemA2AAuthority
from graph_os.a2a.routing import OrchestratorA2ARouter
from graph_os.a2a.service import A2AService
from graph_os.core.settings import runtime_config
from graph_os.mcp_server import runtime as mcp_runtime
from graph_os.mcp_server.composition import install_gateway_application
from graph_os.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from graph_os.security import request_identity
from graph_os.security.actor_context import ActorContext, ActorType
from tests.test_agents_run_end_to_end import FakeEngine

SCOPES = ("kg:read", "kg:write", "memory:read", "node:read", "query:unified")


class FakeAudit:
    """The engine audit ledger: a reservation, then its linked outcome."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def audit_append(self, **fields: Any) -> dict[str, Any]:
        seq = len(self.entries)
        self.entries.append(fields)
        reserved = fields["status"] == "reserved"
        return {
            "graph": fields["tenant"] if "tenant" in fields else "tenant:one",
            "seq": seq,
            "reservation_seq": seq if reserved else seq - 1,
            "outcome_seq": None if reserved else seq,
            "replayed": False,
            "entry_sha256": "0" * 64,
        }


class FakeClient(FakeEngine):
    """The fake engine client: audit, the generated method transport, WorkItems."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, Any, str | None]] = []
        self.admin = FakeAudit()

    @contextlib.contextmanager
    def use_verified_context(self, _claims: Any) -> Iterator[None]:
        yield

    async def _send(
        self, method: str, params: Any, graph: str | None, **_kwargs: Any
    ) -> Any:
        self.sent.append((method, params, graph))
        if method == "Uql":
            return {"rows": [{"id": "node:one"}]}
        if method == "GetNodesByLabel":
            return [{"id": "node:one", "labels": ["Agent"]}]
        raise AssertionError(f"unexpected engine method {method}")


class FakePool:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    async def connected_client(self, _context: Any, _graph: str) -> FakeClient:
        return self.client


def _actor() -> ActorContext:
    return ActorContext(
        actor_id="person:one",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        roles=SCOPES,
        credential_expires_at=int(time.time()) + 3600,
    )


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    registry = tmp_path / "models.json"
    registry.write_text(json.dumps({"default": "test"}), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "model_registry_path", str(registry))
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "graph-os-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")
    # The identity gate needs a configured verifier; the verification is faked.
    monkeypatch.setattr(
        runtime_config, "auth_jwt_jwks_uri", "https://identity.invalid/jwks"
    )

    async def credential_owner(credential: str) -> ActorContext:
        assert credential == "fixture-verified"  # sanitizer:ignore - synthetic
        return _actor()

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    client = FakeClient()
    monkeypatch.setattr(mcp_runtime, "_get_engine", lambda: client)
    agent_runs.bind_run_ports()
    process = request_identity.mint_graph_session(_actor())
    a2a = A2AService(
        authority=WorkItemA2AAuthority(lambda: client),
        router=OrchestratorA2ARouter(lambda: client),
    )
    monkeypatch.setattr(a2a_mcp, "_SERVICE", a2a)
    invocation = compose_served_invocation(
        pool=FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        bindings={"a2a": a2a},
        fleet_effect=refused_fleet_effect,
    )
    install_gateway_application(invocation)
    from graph_os.gateway.graph_api import register_graph_routes

    app = FastAPI()
    register_graph_routes(app)
    yield app, client
    install_gateway_application()
    host_ports.register_run_decision_provider(None)
    host_ports.register_model_provider(None)
    host_ports.register_run_event_sink(None)


def _call(app: Any, op_id: str, params: dict[str, Any], **headers: str) -> Any:
    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as http:
            return await http.post(
                f"/api/v1/ops/{op_id}",
                json=params,
                headers={"Authorization": "Bearer fixture-verified", **headers},
            )

    return asyncio.run(call())


def _value(response: httpx.Response) -> Any:
    """What the web UI host's ``operation_value`` returns for this response."""
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("ok") is not False, payload
    result = payload.get("result", payload)
    return result["value"] if isinstance(result, dict) and "value" in result else result


@pytest.mark.spec("GRAPHOS-HOST-R001.2.3")
def test_served_api_answers_the_web_ui_operations(served: Any) -> None:
    app, client = served

    task = _value(
        _call(
            app,
            "agents.run",
            {
                "message": {
                    "role": "user",
                    "messageId": "message-ui",
                    "parts": [{"kind": "text", "text": "say hello"}],
                    "metadata": {"agent_name": "helper"},
                }
            },
            **{"Idempotency-Key": "ui-run-1"},
        )
    )
    assert task["status"]["state"] == "completed"

    # EG serves no memory method yet, so the served surface has no memory.*.
    absent = _call(app, "memory.read", {"key": "note"})
    assert absent.status_code in {404, 405}, absent.text

    rows = _value(_call(app, "query.uql", {"text": "MATCH (n) RETURN n", "params": {}}))
    assert rows == {"rows": [{"id": "node:one"}]}

    nodes = _value(
        _call(
            app,
            "eg.graph.GetNodesByLabel",
            {"label": "Agent", "after": None, "limit": 100},
        )
    )
    assert nodes == [{"id": "node:one", "labels": ["Agent"]}]
    assert [method for method, _params, _graph in client.sent] == [
        "Uql",
        "GetNodesByLabel",
    ]


@pytest.mark.spec("GRAPHOS-HOST-R001.2.3")
@pytest.mark.asyncio
async def test_served_mcp_surface_lists_the_intent_verbs_and_finds(
    served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = request_identity.mint_graph_session(_actor())
    monkeypatch.setattr(mcp_runtime, "_PROCESS_SESSION", process)
    _args, mcp, _middlewares = mcp_runtime._build_server(bootstrap=False)

    listed = {tool.name for tool in await mcp.list_tools()}
    found = await mcp.call_tool("find", {"intent": "list graph nodes with a label"})

    assert {"find", "ask", "act"} <= listed
    text = "".join(getattr(part, "text", "") for part in found.content)
    assert text and '"error"' not in text, text
