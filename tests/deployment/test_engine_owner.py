"""The engine starts only when absent, and the starter records ownership (ARBORA-DEPLOY-R018.3)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from arbora.deployment import engine_lock
from arbora.mcp_server import composition
from arbora.mcp_server.composition import (
    EngineRefusal,
    LocalEngineConfig,
    ensure_local_engine,
    stand_down_engine,
)
from arbora.mcp_server.served_engine import EngineAttachments

_FAKE_ENGINE = textwrap.dedent(
    """\
    #!{python}
    import os, socket, sys, time
    if "--help" in sys.argv:
        print("--idle-shutdown-secs <SECS>")
        sys.exit(0)
    with open(os.environ["FAKE_ENGINE_SPAWN_LOG"], "a") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
    host, _, port = sys.argv[sys.argv.index("--tcp-addr") + 1].rpartition(":")
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, int(port)))
    server.listen()
    while True:
        time.sleep(1)
    """
)


@pytest.fixture
def fake_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    binary = tmp_path / "fake-engine"
    binary.write_text(_FAKE_ENGINE.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("FAKE_ENGINE_SPAWN_LOG", str(log))

    async def healthy(_endpoint: object, _timeout: float) -> dict[str, str]:
        return {"status": "ok", "version": "0.0.0"}

    monkeypatch.setattr(composition, "_engine_health", healthy)
    return str(binary), log


def _config(binary: str, tmp_path: Path, port: int) -> LocalEngineConfig:
    return LocalEngineConfig(
        port=port,
        start_timeout_s=20,
        probe_timeout_s=1,
        stop_grace_s=10,
        binary=binary,
        persist_dir=tmp_path / "store",
    )


@pytest.mark.spec("ARBORA-DEPLOY-R018.3")
def test_absent_starts_once_and_writes_marker_present_starts_nothing(
    fake_engine: tuple[str, Path],
    runtime_dir: Path,
    tmp_path: Path,
    attachments: EngineAttachments,
    free_port: Callable[[], int],
) -> None:
    binary, log = fake_engine
    port = free_port()
    config = _config(binary, tmp_path, port)

    endpoint = ensure_local_engine(config, attachments)
    assert endpoint.address == f"127.0.0.1:{port}"
    assert attachments.spawned == 1
    spawns = log.read_text().splitlines()
    assert len(spawns) == 1
    assert f"--tcp-addr 127.0.0.1:{port}" in spawns[0]
    assert "--idle-shutdown-secs 300" in spawns[0]
    marker = engine_lock.engine_marker_path(endpoint.address)
    pid = json.loads(marker.read_text())["pid"]
    assert pid == attachments.attached[endpoint.address].process.pid  # type: ignore[union-attr]

    # A second call in the same process attaches to the same engine and starts nothing.
    assert ensure_local_engine(config, attachments) == endpoint
    assert len(log.read_text().splitlines()) == 1

    # Standing down stops the engine this host started and removes the marker.
    assert stand_down_engine(endpoint, attachments) is True
    assert not marker.exists()

    # A running engine that this process did not start: nothing is started, no marker.
    port2 = free_port()
    import subprocess

    peer = subprocess.Popen(  # noqa: S603 - the fake engine, a fixed argv
        [binary, "--tcp-addr", f"127.0.0.1:{port2}"]
    )
    try:
        config2 = _config(binary, tmp_path, port2)
        import time

        deadline = time.monotonic() + 20
        while composition.probe_engine(config2.endpoint, 1).state != "running":
            assert time.monotonic() < deadline
            time.sleep(0.2)
        before = len(log.read_text().splitlines())
        attached = ensure_local_engine(config2, attachments)
        assert len(log.read_text().splitlines()) == before
        assert not engine_lock.engine_marker_path(attached.address).exists()
        assert stand_down_engine(attached, attachments) is False
    finally:
        peer.terminate()
        peer.wait()


@pytest.mark.spec("ARBORA-DEPLOY-R018.3")
def test_missing_binary_and_old_launch_contract_are_typed_refusals(
    runtime_dir: Path,
    tmp_path: Path,
    attachments: EngineAttachments,
    free_port: Callable[[], int],
) -> None:
    config = _config(str(tmp_path / "absent"), tmp_path, free_port())
    with pytest.raises(EngineRefusal) as missing:
        ensure_local_engine(config, attachments)
    assert missing.value.code == "ENGINE_BINARY_MISSING"

    old = tmp_path / "old-engine"
    old.write_text(f"#!{sys.executable}\nprint('--tcp-addr')\n")
    old.chmod(old.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(EngineRefusal) as contract:
        ensure_local_engine(_config(str(old), tmp_path, free_port()), attachments)
    assert contract.value.code == "ENGINE_LAUNCH_CONTRACT_INVALID"
