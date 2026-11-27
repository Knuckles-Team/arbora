"""The tiny profile cold-boots and serves graph reads and writes (ARBORA-HOST-R001.9.2).

Owner-run: the host starts as its own process with the real embedded engine from the installed
``medulla`` package, so this module carries the ``real_boot`` marker and runs in the
end-of-run validation, never in a lane. The setup refuses a configured external endpoint.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import msgpack
import pytest

from arbora.deployment.config_generator import generate_config
from tests.test_real_boot import _free_port, _listening, _terminate

pytestmark = [
    pytest.mark.real_boot,
    pytest.mark.spec("ARBORA-HOST-R001.9.2"),
    pytest.mark.skipif(
        not (sys.platform == "linux" and platform.machine() in {"x86_64", "AMD64"}),
        reason="the embedded engine wheel ships linux x86_64 only",
    ),
]

#: Settings that would point the host at a broker, relational store or remote graph store.
EXTERNAL_ENDPOINTS = (
    "GRAPH_SERVICE_ENDPOINTS",
    "KAFKA_BOOTSTRAP_SERVERS",
    "GRAPH_DB_CONNECTION_PROFILE_REF",
    "GRAPH_MIRROR_TARGETS",
)
_INHERITED = {"PATH", "LANG", "LD_LIBRARY_PATH", "PYTHONPATH", "SSL_CERT_FILE"}
BOOT_TIMEOUT_S = 240.0


def _tiny_config() -> dict[str, Any]:
    config = generate_config("tiny")
    configured = [
        key for key in EXTERNAL_ENDPOINTS if config.get(key) not in (None, "", "[]", [])
    ]
    if configured:
        pytest.fail(f"tiny profile configures external endpoints: {configured}")
    return config


@pytest.fixture(scope="module")
def tiny_host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("tiny-boot")
    config_dir = root / "config"
    config_dir.mkdir()
    config = {
        **_tiny_config(),
        "ARBORA_AUTH_MODE": "none",
        "ENABLE_WEB_UI": False,
        "MESSAGING_INTAKE_ENABLED": False,
    }
    (config_dir / "config.json").write_text(json.dumps(config))
    (config_dir / "mcp_config.json").write_text(json.dumps({"mcpServers": {}}))
    env = {k: v for k, v in os.environ.items() if k in _INHERITED}
    env.update(
        {
            "ARBORA_CONFIG_DIR": str(config_dir),
            "ARBORA_AUTH_MODE": "none",
            "DEPLOYMENT_PROFILE": "tiny",
            "ENGINE_TOPOLOGY": "unified-in-process",
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
        }
    )
    entry = shutil.which("arbora", path=str(Path(sys.executable).parent))
    assert entry is not None, "the arbora console entry point is not installed"
    port = _free_port()
    log = (root / "arbora.log").open("wb")
    process = subprocess.Popen(
        [
            entry,
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-type",
            "none",
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    "arbora exited during cold boot\n"
                    + (root / "arbora.log").read_text(errors="replace")[-4000:]
                )
            if _listening(port):
                ready = httpx.get(f"{base_url}/health/ready", timeout=10)
                if ready.status_code == 200:
                    break
            time.sleep(2)
        else:
            pytest.fail("the tiny host did not become ready")
        yield base_url
    finally:
        _terminate(process)
        log.close()


def _op(base_url: str, op_id: str, params: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{base_url}/api/v1/ops/{op_id}",
        json=params,
        headers={"Idempotency-Key": f"tiny-{uuid.uuid4().hex[:12]}"},
        timeout=60,
    )


def test_tiny_profile_config_names_no_external_endpoint() -> None:
    config = _tiny_config()
    assert config["DEPLOYMENT_PROFILE"] == "tiny"
    assert config["ENGINE_TOPOLOGY"] == "unified-in-process"


def test_tiny_profile_refuses_a_configured_external_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.deployment import config_generator

    preset = dict(config_generator._PROFILE_PRESETS["tiny"])
    preset["KAFKA_BOOTSTRAP_SERVERS"] = "broker.invalid:9092"
    monkeypatch.setitem(config_generator._PROFILE_PRESETS, "tiny", preset)
    with pytest.raises(pytest.fail.Exception, match="external endpoints"):
        _tiny_config()


def test_tiny_profile_writes_and_reads_one_node(tiny_host: str) -> None:
    node_id = f"tiny-boot:{uuid.uuid4().hex[:12]}"
    properties = {"label": "TinyBootProbe", "tenant_marker": "local"}
    written = _op(
        tiny_host,
        "eg.graph.AddNode",
        {
            "node_id": node_id,
            "properties_msgpack": list(msgpack.packb(properties, use_bin_type=True)),
        },
    )
    assert written.status_code == 200, written.text
    assert written.json()["ok"] is True, written.text
    read = _op(tiny_host, "eg.graph.GetNodeProperties", {"node_id": node_id})
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["ok"] is True, body
    assert "TinyBootProbe" in json.dumps(body), body
    absent = _op(
        tiny_host, "eg.graph.GetNodeProperties", {"node_id": node_id + ":absent"}
    )
    assert "TinyBootProbe" not in absent.text
