"""ARBORA-HOST-R009 / MERISTEM-BOUNDARY-R015: connector agent host wiring tests.

Exercises the real ``build_agent_host_app`` entry point a connector's
``agent_server.py`` reaches through ``create_agent_server`` -- health,
agent-card discovery, and a bounded A2A ``message/send`` exchange -- with a
stub executor standing in for a live model/MCP call.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from arbora.agent_host import build_agent_host_app


def _stub_executor(graph: Any, graph_config: dict, query: str) -> dict:
    assert graph == "stub-graph"
    assert graph_config == {"valid_domains": []}
    return {"output": f"echo: {query}"}


def _client() -> TestClient:
    app = build_agent_host_app(
        graph="stub-graph",
        graph_config={"valid_domains": []},
        name="Stub Connector Agent",
        description="A stub connector agent for wiring tests.",
        executor=_stub_executor,
    )
    return TestClient(app)


def test_health() -> None:
    response = _client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_card() -> None:
    response = _client().get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Stub Connector Agent"
    assert card["protocolVersion"] == "0.3.0"


def test_message_send_runs_the_executor_and_returns_a_completed_task() -> None:
    response = _client().post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "method": "message/send",
            "id": 1,
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                    "messageId": "m-1",
                }
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1
    result = body["result"]
    assert result["status"]["state"] == "completed"
    agent_message = result["history"][-1]
    assert agent_message["role"] == "agent"
    assert agent_message["parts"][0]["text"] == "echo: hello"


def test_message_send_failure_is_reported_as_an_a2a_error_not_an_http_error() -> None:
    def _raising_executor(graph: Any, graph_config: dict, query: str) -> dict:
        raise RuntimeError("boom")

    app = build_agent_host_app(
        graph="stub-graph",
        graph_config={"valid_domains": []},
        executor=_raising_executor,
    )
    response = TestClient(app).post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "method": "message/send",
            "id": 2,
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                }
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 2
    assert body["error"]["code"] == -32000


def test_unknown_method_is_rejected() -> None:
    response = _client().post(
        "/a2a",
        json={"jsonrpc": "2.0", "method": "tasks/get", "id": 3, "params": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32601


# ---- terminal-mode HTTP in a supervised thread (ARBORA-HOST-R009.2) ----

import threading  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from arbora.agent_host import create_agent_server  # noqa: E402
from arbora.agent_host.terminal_http import (  # noqa: E402
    HTTP_THREAD_NAME,
    HostLaunchRefused,
    TerminalHttpHosts,
)
from arbora.mcp_server.composition import CoServiceSupervisor  # noqa: E402
from tests.agent_host._terminal_owner import (  # noqa: E402
    launch_arguments,
    terminal_owner,
)


@pytest.mark.spec("ARBORA-HOST-R009.2")
def test_terminal_mode_serves_http_in_distinct_thread(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def client(host: Any, admitted: Any) -> None:
        seen["host"] = host
        seen["authority"] = admitted.runtime.engine
        seen["foreground"] = threading.current_thread().name
        seen["health"] = httpx.get(f"{host.endpoint}/health").json()
        seen["threads"] = sorted(
            t.name for t in threading.enumerate() if t.name == HTTP_THREAD_NAME
        )

    owner = terminal_owner(tmp_path, client)
    create_agent_server(**launch_arguments(owner))
    host = seen["host"]
    assert seen["health"] == {"status": "ok"}
    assert host.ready is True and host.thread_name == HTTP_THREAD_NAME
    assert host.thread_id != threading.get_ident()
    assert seen["foreground"] != HTTP_THREAD_NAME
    assert seen["threads"] == [HTTP_THREAD_NAME]
    assert host.authority == f"authority:{id(seen['authority']):x}"
    assert not any(t.name == HTTP_THREAD_NAME for t in threading.enumerate())


@pytest.mark.spec("ARBORA-HOST-R009.2")
def test_terminal_launch_refusals_carry_their_codes() -> None:
    supervisor = CoServiceSupervisor()
    hosts = TerminalHttpHosts(supervisor)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

    common = dict(app=app, session=object(), bind_host="127.0.0.1", port=0)
    with pytest.raises(HostLaunchRefused) as absent:
        hosts.start(host_instance="h0", authority=None, **common)
    assert absent.value.code == "HOST_AUTHORITY_ABSENT"
    first = hosts.start(host_instance="h1", authority=object(), **common)
    try:
        with pytest.raises(HostLaunchRefused) as duplicate:
            hosts.start(host_instance="h1", authority=object(), **common)
        assert duplicate.value.code == "HOST_DUPLICATE_INSTANCE"
        taken = int(first.endpoint.rsplit(":", 1)[1])
        with pytest.raises(HostLaunchRefused) as bind:
            hosts.start(
                host_instance="h2",
                authority=object(),
                **{**common, "port": taken},
            )
        assert bind.value.code == "HOST_BIND_FAILED"
        assert supervisor.running() == (f"{HTTP_THREAD_NAME}:h1",)
    finally:
        assert hosts.stop("h1") is True
