"""Fixtures for the local engine lifecycle tests (ARBORA-DEPLOY-R018)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from arbora.deployment import engine_lock
from arbora.mcp_server import composition

_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from arbora.core.settings import RuntimeConfig
    from arbora.mcp_server.composition import (
        ensure_local_engine, engine_spawn_count, local_engine_config,
    )
    config = local_engine_config(
        RuntimeConfig(),
        port=int(sys.argv[1]), persist_dir=Path(sys.argv[2]),
        extra_args=("--auth-mode", "none"), start_timeout_s=90, stop_grace_s=20,
    )
    endpoint = ensure_local_engine(config)
    print(json.dumps({"address": endpoint.address, "spawned": engine_spawn_count()}),
          flush=True)
    sys.stdin.readline()
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def free_port() -> Callable[[], int]:
    """A function returning a currently unused loopback port."""
    return _free_port


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated runtime directory for lock and marker files."""
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(directory))
    # The same directory the engine lock module resolves in a child process.
    base = directory / "arbora"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(engine_lock, "_runtime_base", lambda: base)
    return base


@pytest.fixture(autouse=False)
def clean_attachments() -> Iterator[None]:
    yield
    for attachment in list(composition._ATTACHED.values()):
        composition.stand_down_engine(attachment.endpoint)


class Child:
    """A separate process that attaches to (or starts) the local engine."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.report: dict[str, object] = json.loads(process.stdout.readline())  # type: ignore[union-attr]

    def finish(self) -> None:
        """Let the child exit normally so its exit hook runs."""
        self.process.stdin.write("\n")  # type: ignore[union-attr]
        self.process.stdin.flush()  # type: ignore[union-attr]
        self.process.wait(timeout=60)


@pytest.fixture
def start_child(runtime_dir: Path, tmp_path: Path) -> Iterator[Callable[[int], Child]]:
    children: list[Child] = []

    def start(port: int) -> Child:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, the running interpreter
            [sys.executable, "-c", _CHILD, str(port), str(tmp_path / "store")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir.parent)},
        )
        child = Child(process)
        children.append(child)
        return child

    yield start
    for child in children:
        if child.process.poll() is None:
            child.process.kill()
            child.process.wait()
    for marker in runtime_dir.glob("engine-*.owner"):
        try:
            pid = int(json.loads(marker.read_text())["pid"])
            os.kill(pid, 15)
        except (OSError, ValueError, KeyError):
            pass


@pytest.fixture
def independent_engine(
    tmp_path: Path, free_port: Callable[[], int]
) -> Iterator[Callable[[], int]]:
    """Start a real engine that no Arbora process owns; returns its port."""
    from medulla.server_binary import resolve_server_binary

    from arbora.mcp_server.composition import EngineEndpoint, probe_engine

    started: list[subprocess.Popen[bytes]] = []

    def start() -> int:
        port = free_port()
        process = subprocess.Popen(  # noqa: S603 - the packaged engine, a fixed argv
            [
                resolve_server_binary(),
                "--auth-mode",
                "none",
                "--tcp-only",
                "--tcp-addr",
                f"127.0.0.1:{port}",
                "--persist-dir",
                str(tmp_path / f"independent-{port}"),
                "--idle-shutdown-secs",
                "600",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(process)
        endpoint = EngineEndpoint("tcp", f"127.0.0.1:{port}")
        deadline = time.monotonic() + 60
        while probe_engine(endpoint, 1).state != "running":
            assert time.monotonic() < deadline, "the independent engine did not start"
            time.sleep(0.3)
        return port

    yield start
    for process in started:
        process.terminate()
        process.wait(timeout=30)
