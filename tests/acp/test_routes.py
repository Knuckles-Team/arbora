"""Real loopback HTTP/SSE with an isolated client filesystem and fixture identity."""

import asyncio
import json
import socket
from datetime import timedelta

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from arbora.acp import AcpSessionPolicy, CodingSessionAdmission
from arbora.acp.routes import SESSION_HEADER, register_acp_routes
from arbora.api.contracts.session import GraphSession
from arbora.api.invoke.steps import VerifiedCaller
from arbora.security.actor_context import ActorContext


def caller():
    actor = ActorContext(
        actor_id="alice", tenant_id="tenant", authenticated=True, roles=("kg:write",)
    )
    session = GraphSession(actor=actor, tenant="tenant", scopes=frozenset({"kg:write"}))
    return VerifiedCaller(
        "alice",
        "tenant",
        frozenset({"kg:write"}),
        {
            "principal": "alice",
            "tenant": "tenant",
            "scopes": ["kg:write"],
            "policy_version": "v1",
            "delegation": False,
        },
        "human",
        True,
        False,
        "bearer",
        "v1",
        session=session,
    )


class FixtureAuthenticator:
    async def authenticate(self, request):
        if request.headers.get("authorization") != "Bearer fixture":
            raise PermissionError("fixture caller required")
        return caller()


async def consume_client_events(client, lines, document, received):
    sequences = []
    async for line in lines:
        if line.startswith("id: "):
            sequences.append(int(line[4:]))
        if not line.startswith("data: "):
            continue
        message = json.loads(line[6:])
        received.append(message)
        if message["method"] == "fs/read_text_file":
            assert message["params"]["path"] == str(document)
            reply = await client.post(
                "/api/acp",
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"content": document.read_text()},
                },
            )
            assert reply.status_code == 200
        elif message["method"] == "arbora/run":
            if message["params"]["event"].get("type") == "graph_complete":
                break
    return sequences


@pytest.mark.spec("ARBORA-ACP-R001.9")
async def test_real_http_client_reads_and_previews_without_effects(
    tmp_path, monkeypatch
):
    from arbora.acp import routes as route_module

    async def refresh(value):
        return value

    monkeypatch.setattr(route_module, "verify_current_from_session", refresh)
    document = tmp_path / "example.txt"
    document.write_text("original\n")
    actions = [
        ("read_file", {"path": str(document)}),
        ("write_file", {"path": str(document), "content": "replacement\n"}),
        (
            "run_command",
            {"command": "touch", "args": [str(tmp_path / "must-not-exist")]},
        ),
    ]
    from arbora.acp import run_adapter
    from arbora.acp.session_policy import AcpRefusal

    async def fixture_runner(channel, text):
        transport = run_adapter.PreviewTransport(channel)
        for tool, params in actions:
            method = {
                "read_file": "fs/read_text_file",
                "write_file": "fs/write_text_file",
                "run_command": "terminal/create",
            }[tool]
            try:
                await transport.call(method, params)
            except AcpRefusal as exc:
                assert exc.reason.value == "permission_required"
        run_adapter.emit_run_event(
            channel, {"type": "node_transition", "output": "previews complete"}
        )
        run_adapter.emit_run_event(channel, {"type": "graph_complete"})
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(run_adapter, "available", lambda: True)
    monkeypatch.setattr(run_adapter, "run_prompt", fixture_runner)
    app = FastAPI()
    admission = CodingSessionAdmission(
        AcpSessionPolicy(timedelta(minutes=1), timedelta(minutes=5)),
        trusted_roots=frozenset({str(tmp_path)}),
    )
    register_acp_routes(app, admission=admission, authenticator=FixtureAuthenticator())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    received = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer fixture"},
            timeout=5,
        ) as client:
            opened = await client.post(
                "/api/acp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/new",
                    "params": {
                        "cwd": str(tmp_path),
                        "clientCapabilities": [
                            "fs/read_text_file",
                            "fs/write_text_file",
                            "terminal/create",
                        ],
                    },
                },
            )
            session_id = opened.json()["result"]["sessionId"]
            client.headers[SESSION_HEADER] = session_id
            async with client.stream("GET", "/api/acp/events") as stream:
                lines = stream.aiter_lines()
                assert await anext(lines) == ": connected"
                prompt = asyncio.create_task(
                    client.post(
                        "/api/acp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/prompt",
                            "params": {"sessionId": session_id, "prompt": "inspect"},
                        },
                    )
                )
                sequences = await consume_client_events(
                    client, lines, document, received
                )
                assert (await prompt).json()["result"]["stopReason"] == "end_turn"
                assert sequences == list(range(1, len(sequences) + 1))
            assert document.read_text() == "original\n"
            assert not (tmp_path / "must-not-exist").exists()
            assert all(
                item["method"] in {"fs/read_text_file", "session/update", "arbora/run"}
                for item in received
            )
            previews = [
                item["params"]["update"]
                for item in received
                if item["method"] == "session/update"
                and item["params"]["update"].get("_meta")
            ]
            assert len(previews) == 2
            assert previews[0]["content"][0]["oldText"] == "original\n"
            assert previews[0]["content"][0]["newText"] == "replacement\n"
            assert previews[1]["rawInput"]["command"] == "touch"
    finally:
        server.should_exit = True
        await serving
        listener.close()


@pytest.mark.spec("ARBORA-ACP-R001.9")
async def test_http_refuses_unavailable_adapter_and_malformed_parameters(monkeypatch):
    from arbora.acp import routes as route_module
    from arbora.acp import run_adapter

    async def refresh(value):
        return value

    monkeypatch.setattr(route_module, "verify_current_from_session", refresh)
    monkeypatch.setattr(run_adapter, "available", lambda: False)
    app = FastAPI()
    admission = CodingSessionAdmission(
        AcpSessionPolicy(timedelta(minutes=1), timedelta(minutes=5)),
        trusted_roots=frozenset({"/project"}),
    )
    routes = register_acp_routes(
        app, admission=admission, authenticator=FixtureAuthenticator()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": "/project", "clientCapabilities": []},
        }
        assert (await client.post("/api/acp", json=message)).status_code == 400
        client.headers["Authorization"] = "Bearer fixture"
        response = await client.post("/api/acp", json=message)
        assert response.json()["error"]["message"] == "chat_adapter_unavailable"
        message["params"] = []
        assert (await client.post("/api/acp", json=message)).status_code == 400
        message["id"] = {"invalid": "id"}
        assert (await client.post("/api/acp", json=message)).json()["id"] is None
    assert routes.channels == {}
