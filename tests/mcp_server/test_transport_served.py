"""One entrypoint serves stdio and streamable HTTP (ARBORA-HOST-R006.2)."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from arbora.mcp_server.serving_options import (
    EXIT_INVALID_REQUEST,
    ServingOptionsRefused,
    check_serving_options,
    startup_record,
)

ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = {"graph_task_progress_app", "graph_trace_waterfall_app"}


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), *sys.path])}


async def _initialize_and_call(client: object) -> str:
    async with client:  # type: ignore[attr-defined]
        names = {tool.name for tool in await client.list_tools()}  # type: ignore[attr-defined]
        assert LAUNCHERS <= names
        result = await client.call_tool("graph_task_progress_app", {"job_id": "job-1"})  # type: ignore[attr-defined]
        return str(result.content[0].text)


@pytest.mark.spec("ARBORA-HOST-R006.2")
def test_stdio_and_http_initialize_and_call_one_op() -> None:
    import uvicorn
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    from tests.mcp_server._stdio_surface import build

    stdio = Client(
        StdioTransport(
            sys.executable,
            [str(ROOT / "tests/mcp_server/_stdio_surface.py")],
            env=_env(),
        )
    )
    over_stdio = asyncio.run(_initialize_and_call(stdio))

    mcp = build()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(mcp.http_app(), log_level="critical", lifespan="on")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            started = threading.Event()
            while not server.started:
                started.wait(0.01)
            over_http = asyncio.run(
                _initialize_and_call(Client(f"http://127.0.0.1:{port}/mcp"))
            )
        finally:
            server.should_exit = True
            thread.join(10)
    assert over_stdio and over_http


@pytest.mark.spec("ARBORA-HOST-R006.2")
@pytest.mark.parametrize(
    "flags",
    [
        ["--transport", "carrier-pigeon"],
        ["--transport", "streamable-http", "--port", "70000"],
        ["--transport", "streamable-http", "--port", "0"],
        ["--port", "not-a-port"],
    ],
)
def test_bad_options_and_unauthenticated_http_refuse(flags: list[str]) -> None:
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "from arbora.mcp_server.server import mcp_server; mcp_server()",
            *flags,
        ],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == EXIT_INVALID_REQUEST
    assert run.stdout == ""
    assert "INVALID_REQUEST" in run.stderr


@pytest.mark.spec("ARBORA-HOST-R006.2")
def test_servable_options_and_the_startup_record() -> None:
    from argparse import Namespace

    check_serving_options(Namespace(transport="stdio", port=8000))
    check_serving_options(Namespace(transport="streamable-http", port=65535))
    for bad in (
        Namespace(transport="sse", port=8000),
        Namespace(transport="stdio", port=0),
        Namespace(transport="stdio", port=65536),
    ):
        with pytest.raises(ServingOptionsRefused):
            check_serving_options(bad)
    record = startup_record(
        transport="stdio", tool_digest="d1", multiplexer_instances=1
    )
    assert '"multiplexer_instances": 1' in record and '"tool_digest": "d1"' in record


@pytest.mark.spec("ARBORA-HOST-R006.2")
def test_network_transport_without_validated_identity_exits_before_serving() -> None:
    from arbora.core.settings import RuntimeConfig
    from arbora.security.request_identity import apply_served_security_profile

    with pytest.raises(Exception):  # noqa: B017 - the profile refuses with its own error
        apply_served_security_profile(
            "streamable-http", RuntimeConfig(), transport_auth_configured=False
        )
