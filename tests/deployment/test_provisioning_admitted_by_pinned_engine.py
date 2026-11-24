"""The pinned engine admits everything Arbora provisions (ARBORA-DEPLOY-R020, R020.3)."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from medulla.server_binary import resolve_server_binary

from tests.test_real_boot import _heavy_engine_slot

# Loaded hosts start the engine slowly; the wait is bounded, never unbounded.
_START_DEADLINE_S = 240
_PROVISION_DEADLINE_S = 900


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + _START_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise AssertionError("the pinned engine did not start")


@pytest.mark.spec("ARBORA-DEPLOY-R020")
@pytest.mark.spec("ARBORA-DEPLOY-R020.3")
def test_fresh_store_accepts_all_arbora_content(tmp_path: Path) -> None:
    # The engine boots several GB into memory, the same as the real-boot
    # stack; the shared host-wide slot keeps the suite's peak at one engine.
    with _heavy_engine_slot():
        port = _free_port()
        importer = "principal:sha256:" + hashlib.sha256(b"anonymous").hexdigest()
        engine = subprocess.Popen(  # noqa: S603 - the engine shipped in the locked wheel
            [
                resolve_server_binary(),
                "--auth-mode",
                "none",
                "--tcp-only",
                "--tcp-addr",
                f"127.0.0.1:{port}",
                "--persist-dir",
                str(tmp_path / "store"),
                "--idle-shutdown-secs",
                "1800",
            ],
            env={
                **os.environ,
                "MEDULLA_CONNECTOR_PACK_IMPORTERS": f"arbora={importer},universal-skills={importer}",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_port(port)
            config = tmp_path / "config"
            config.mkdir()
            result = subprocess.run(  # noqa: S603 - the running interpreter, a fixed module
                [
                    sys.executable,
                    "-m",
                    "arbora.deployment.production_ops",
                    "provision-semantic-content",
                    "--served-url",
                    "http://127.0.0.1:18765/mcp",
                ],
                env={
                    **os.environ,
                    "ARBORA_AUTH_MODE": "none",
                    "ARBORA_CONFIG_DIR": str(config),
                    "GRAPH_SERVICE_ENDPOINTS": f"127.0.0.1:{port}",
                    # The pack import is a non-heavy engine RPC under the
                    # client's 60 s default, and on a loaded CI pod it can
                    # outrun that while the other workers hold the CPU.
                    # 240 s stays inside the step's own deadline.
                    "GRAPH_SERVICE_RPC_TIMEOUT": "240",
                },
                capture_output=True,
                text=True,
                timeout=_PROVISION_DEADLINE_S,
                check=False,
            )
            report = json.loads(result.stdout.strip().splitlines()[-1])
            assert result.returncode == 0 and report.get("ok") is True, (
                result.stdout[-2000:],
                result.stderr[-2000:],
            )
            # The skills library declares itself as a content connector, so the
            # provision imports its pack alongside arbora's own.
            assert report["imports"] == {
                "arbora": "imported",
                "universal-skills": "imported",
            }
            assert report["verified"] is True
        finally:
            engine.terminate()
            engine.wait(timeout=60)
