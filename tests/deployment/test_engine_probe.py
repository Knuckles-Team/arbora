"""The engine probe reports a running engine without starting one (ARBORA-DEPLOY-R018.2)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from arbora.mcp_server.composition import (
    EngineEndpoint,
    EngineProbe,
    EngineState,
    probe_engine,
)


@pytest.fixture
def listener() -> Iterator[EngineEndpoint]:
    """A live TCP listener standing in for an engine transport."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                accepted.append(server.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    yield EngineEndpoint("tcp", f"127.0.0.1:{server.getsockname()[1]}")
    server.close()
    for conn in accepted:
        conn.close()


async def _healthy(_endpoint: EngineEndpoint, _timeout: float) -> Mapping[str, Any]:
    return {"status": "ok", "version": "3.3.3"}


async def _refusing(_endpoint: EngineEndpoint, _timeout: float) -> Mapping[str, Any]:
    raise RuntimeError("authentication required")


async def _silent(_endpoint: EngineEndpoint, _timeout: float) -> Mapping[str, Any]:
    import asyncio

    await asyncio.sleep(30)
    return {}


@pytest.mark.spec("ARBORA-DEPLOY-R018.2")
def test_live_fake_engine_is_running_and_dead_socket_is_absent(
    listener: EngineEndpoint, tmp_path: Any
) -> None:
    assert probe_engine(listener, 1.0, health=_healthy) == EngineProbe(
        EngineState.RUNNING, "3.3.3"
    )
    # An engine that answers with a refusal is still running.
    assert probe_engine(listener, 1.0, health=_refusing).state == "running"
    # A transport that accepts but never answers Health is absent after the timeout.
    assert probe_engine(listener, 0.3, health=_silent).state == "absent"

    dead_unix = EngineEndpoint("unix", str(tmp_path / "dead.sock"))
    assert probe_engine(dead_unix, 0.5, health=_healthy) == EngineProbe(
        EngineState.ABSENT
    )
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    dead_tcp = EngineEndpoint("tcp", f"127.0.0.1:{port}")
    assert probe_engine(dead_tcp, 0.5, health=_healthy).state == "absent"
