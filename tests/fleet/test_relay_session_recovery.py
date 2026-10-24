"""A relay read survives a real child restart (ARBORA-FLEET-R040.2, gap G4).

A streamable-http child is a real FastMCP server process on a loopback port.
The multiplexer mounts it and reads a resource and a prompt. The child process
is then killed and started again on the same port, so it no longer knows the
session id and answers 404 "Session not found". The next relay read must
re-establish the session and succeed, without an arbora restart.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from typing import Any, cast

import pytest

from arbora.fleet import child_resilience
from tests.fleet.catalog_fixture import multiplexer_from_fixture

pytestmark = pytest.mark.spec("ARBORA-FLEET-R040.2")

_CHILD = """
import sys
from fastmcp import FastMCP

generation = sys.argv[2]
mcp = FastMCP("relay-child")


@mcp.resource("ontology://relay-child/x.ttl", mime_type="text/turtle")
def ontology() -> str:
    return "generation " + generation


@mcp.tool(annotations={"readOnlyHint": True})
def big_page(size: int) -> dict:
    # One result page larger than the MCP client default SSE event bound.
    return {"result": [{"sys_id": str(i), "text": "x" * 1000} for i in range(size)]}


@mcp.prompt(name="create_branch_prompt")
def create_branch_prompt(project_id: str, branch: str) -> str:
    return "create " + branch + " in " + project_id


@mcp.prompt(name="triage")
def triage() -> str:
    return "triage from generation " + generation


mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[1]), path="/mcp",
        show_banner=False, log_level="error")
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start(port: int, generation: str) -> subprocess.Popen:
    process = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-c", _CHILD, str(port), generation],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return process
        time.sleep(0.1)
    process.kill()
    raise RuntimeError("child did not start")


def _stop(process: subprocess.Popen) -> None:
    process.kill()
    process.wait(timeout=10)


@pytest.fixture
def child_port():
    port = _free_port()
    processes: list[subprocess.Popen] = [_start(port, "1")]
    yield port, processes
    for process in processes:
        if process.poll() is None:
            _stop(process)


async def _mounted(tmp_path, port: int):
    mux = multiplexer_from_fixture(tmp_path / "c.json")
    started = await mux._start_child(
        "relay-child",
        {
            "url": f"http://127.0.0.1:{port}/mcp",
            "connect_timeout": 20,
            "allowed_private_hosts": ["127.0.0.1"],
        },
    )
    assert started is not None
    payload = started[1]
    cfg = {"url": f"http://127.0.0.1:{port}/mcp"}
    mux._register_child_result("relay-child", payload, [], cfg)
    return mux, payload


async def _read(mux):
    result = await mux._relay_read(
        "relay-child",
        lambda session: session.read_resource("ontology://relay-child/x.ttl"),
        10.0,
    )
    return result.contents[0].text


async def _prompt(mux):
    result = await mux._relay_read(
        "relay-child", lambda session: session.get_prompt("triage", {}), 10.0
    )
    return result.messages[0].content.text


def test_unknown_session_is_a_dead_session_signature() -> None:
    error = child_resilience.MCPError(-32600, "Session not found")
    assert child_resilience.is_session_dead(error)
    other = child_resilience.MCPError(-32600, "Invalid params")
    assert not child_resilience.is_session_dead(other)


async def test_relay_reads_reestablish_the_session_after_a_real_child_restart(
    tmp_path, child_port
) -> None:
    port, processes = child_port
    mux, runtime = await _mounted(tmp_path, port)
    try:
        assert await _read(mux) == "generation 1"
        before = runtime.generation

        _stop(processes[0])
        processes.append(await asyncio.to_thread(_start, port, "2"))

        assert await _read(mux) == "generation 2"
        assert runtime.generation == before + 1
        assert await _prompt(mux) == "triage from generation 2"
    finally:
        await runtime.aclose()


async def test_a_child_that_stays_down_fails_after_one_bounded_retry(
    tmp_path, child_port
) -> None:
    port, processes = child_port
    mux, runtime = await _mounted(tmp_path, port)
    runtime.connect_timeout = 2.0
    try:
        assert await _read(mux) == "generation 1"
        _stop(processes[0])
        started = time.monotonic()
        with pytest.raises(BaseException):  # noqa: B017 - any transport failure
            await _read(mux)
        assert time.monotonic() - started < 30
    finally:
        await runtime.aclose()


@pytest.mark.spec("ARBORA-OPS-R009.4")
async def test_a_prompt_called_without_its_arguments_is_a_typed_refusal(
    tmp_path, child_port
) -> None:
    """G10: the child answers -32603 "Missing required arguments"; arbora names them."""
    from arbora.api.errors import to_envelope
    from arbora.api.invoke.pipeline import OperationRefused, _typed_refusal
    from arbora.fleet.gateway_ops import _prompt_refusal

    port, _ = child_port
    mux, runtime = await _mounted(tmp_path, port)
    try:
        with pytest.raises(child_resilience.MCPError) as raised:
            await mux._relay_read(
                "relay-child",
                lambda session: session.get_prompt("create_branch_prompt", {}),
                10.0,
            )
        refusal = _prompt_refusal(
            cast(Exception, raised.value), "relay-child", "create_branch_prompt"
        )
        assert isinstance(refusal, OperationRefused)
        _, envelope = to_envelope(
            cast(Any, _typed_refusal(refusal)),
            op="fleet.prompts.get",
            request_id="r",
            registry_digest="d" * 64,
        )
        assert envelope["error"]["code"] == "INVALID_ARGUMENT"
        assert envelope["error"]["details"]["missing"] == ["branch", "project_id"]
    finally:
        await runtime.aclose()


@pytest.mark.spec("ARBORA-OPS-R009.4")
def test_declared_required_prompt_arguments_are_checked_before_the_child() -> None:
    from types import SimpleNamespace

    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.fleet.gateway_ops import _prompt_refusal, _require_prompt_arguments

    item = SimpleNamespace(schema={"required": ["project_id", "branch"]})
    with pytest.raises(OperationRefused) as refused:
        _require_prompt_arguments(item, {"branch": "b"})
    assert refused.value.details["missing"] == ["project_id"]
    _require_prompt_arguments(item, {"branch": "b", "project_id": "p"})
    other: Any = _prompt_refusal(
        cast(Exception, child_resilience.MCPError(-32603, "renderer exploded")),
        "s",
        "p",
    )
    assert type(other).__name__ == "FleetRefusal" and other.code == "CHILD_REFUSED"
