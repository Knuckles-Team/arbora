"""A real HTTP ACP server whose scripted model writes and runs, and a client that answers."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import acp
import httpx
import pytest
from acp import schema
from meristem.graph import RunPlan, RunRequest
from meristem.host_ports import HostPorts
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from starlette.applications import Starlette

from arbora.acp.adapter import HostedACPAgent
from arbora.acp.http_transport import AcpHttpLimits, register_acp_routes
from arbora.acp.session_policy import AcpSessionPolicy
from tests.acp.test_http_transport import (
    _Client,
    _Server,
    _SyntheticIdentity,
    http_writer,
)


class CodingClient(_Client):
    """A client that records every call in order and answers permission requests."""

    def __init__(self, root, answer: str | None) -> None:
        super().__init__(root)
        self.answer = answer
        self.events: list[str] = []
        self.writes: list[tuple[str, str]] = []

    async def read_text_file(self, session_id, path, **kwargs):
        self.events.append("read")
        return await super().read_text_file(session_id, path, **kwargs)

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        self.events.append("permission")
        if self.answer is None:
            outcome = schema.DeniedOutcome(outcome="cancelled")
        else:
            outcome = schema.AllowedOutcome(outcome="selected", option_id=self.answer)
        return schema.RequestPermissionResponse(outcome=outcome)

    async def write_text_file(self, content, path, session_id, **kwargs):
        self.events.append("write")
        self.writes.append((path, content))


@pytest.fixture
async def coding_server(tmp_path):
    root = tmp_path
    (root / "sample.txt").write_text("actual temporary file")

    async def model(messages, info):
        part = messages[-1].parts[-1]
        if part.part_kind == "tool-return":
            yield "done"
        elif part.content == "write":
            yield {
                0: DeltaToolCall(
                    name="write_file",
                    json_args='{"path":"out.txt","content":"written"}',
                )
            }
        else:
            yield {
                0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')
            }

    def plan(request):
        value = RunPlan(
            model=FunctionModel(stream_function=model), steps=(request.task,)
        )
        return replace(
            value,
            harness=replace(value.harness, client_tools=("read_file", "write_file")),
        )

    ports = HostPorts(decide=plan)
    app = Starlette()
    app.add_middleware(_SyntheticIdentity)
    app.state.acp_agent_factory = lambda provider, **kwargs: HostedACPAgent(
        provider,
        policy=AcpSessionPolicy(timedelta(minutes=1), timedelta(minutes=5)),
        trusted_roots=frozenset({str(root)}),
        request_factory=lambda principal, session_id, text: RunRequest(
            task=text, context=session_id, ports=ports
        ),
    )
    register_acp_routes(
        app, limits=AcpHttpLimits(connections=2, frame_bytes=8192, messages=32)
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    import uvicorn

    server = _Server(uvicorn.Config(app, lifespan="on", log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(3):
        await server.ready.wait()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        async with httpx.AsyncClient(
            base_url=base, headers={"x-test-user": "alice"}, timeout=3
        ) as client:
            yield client, root
    finally:
        await app.state.acp_close_connections()
        server.should_exit = True
        async with asyncio.timeout(3):
            await task
        sock.close()


@asynccontextmanager
async def coding_connection(http, root, answer: str | None):
    """An initialized connection whose client may read and write."""
    created = await http.post("/api/acp/connections")
    assert created.status_code == 201, created.text
    path = "/api/acp/connections/" + created.json()["connectionId"]
    async with http.stream("GET", path) as events:
        reader = asyncio.StreamReader()

        async def receive():
            try:
                async for line in events.aiter_lines():
                    if line.startswith("data: "):
                        reader.feed_data(line[6:].encode() + b"\n")
            finally:
                reader.feed_eof()

        feeding = asyncio.create_task(receive())
        client = CodingClient(root, answer)
        conn = acp.connect_to_agent(client, http_writer(http, path), reader)
        try:
            await conn.initialize(
                protocol_version=1,
                client_capabilities=schema.ClientCapabilities(
                    fs=schema.FileSystemCapabilities(
                        read_text_file=True, write_text_file=True
                    )
                ),
            )
            yield conn, client
        finally:
            await http.delete(path)
            await conn.close()
            await feeding
