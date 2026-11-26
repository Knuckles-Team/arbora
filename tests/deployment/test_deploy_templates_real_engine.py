"""The deployment library is admitted by a real engine (ARBORA-DEPLOY-R601, R017.20).

The loopback library validates against the generated Python models, which accept
nulls and omissions the engine refuses (a null ``attributes`` or ``runtime``, a
zero context window, an agent with no skill or ontology). This test publishes
every target's templates and tool components to the engine in the locked wheel,
so a request the engine would refuse fails here, not at start-up.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("medulla.generated.models")
from medulla import MedullaClient  # noqa: E402
from medulla.generated import storage  # noqa: E402
from medulla.server_binary import resolve_server_binary  # noqa: E402

from arbora.deployment import assembly_library as library  # noqa: E402
from tests.test_real_boot import HEAVY_BOOT_TEST_TIMEOUT_S, _heavy_engine_slot  # noqa: E402

_START_DEADLINE_S = 240
_GRAPH = "tenant__local"
_TENANT = "local"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + _START_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            await asyncio.sleep(0.5)
    raise AssertionError("the pinned engine did not start")


@pytest.mark.spec("ARBORA-DEPLOY-R601")
@pytest.mark.spec("ARBORA-DEPLOY-R017.20")
@pytest.mark.real_boot
@pytest.mark.timeout(HEAVY_BOOT_TEST_TIMEOUT_S)
def test_every_target_publishes_to_a_real_engine(tmp_path: Path) -> None:
    counter = itertools.count(1)
    senders = {
        "AgentComponent": storage.send_agent_component,
        "AgentTemplate": storage.send_agent_template,
        "AgentGraph": storage.send_agent_graph,
    }

    async def run(port: int) -> dict[str, bool]:
        await _wait_for_port(port)
        client = await MedullaClient.connect(
            auth_mode="none", tcp_addr=f"127.0.0.1:{port}", graph_name=_GRAPH
        )

        async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
            return await senders[method](client, params, _GRAPH, idempotency_key=key)

        async def context(tenant: str, key: str) -> dict[str, Any]:
            number = next(counter)
            return {
                "request_id": number,
                "principal": "principal:sha256:" + "a" * 64,
                "caller_principal": "principal:sha256:" + "b" * 64,
                "attempt_nonce": f"{number:064x}",
                "actor_scope": "action-scope:arbora",
                "purpose_id": library.PURPOSE_ID,
                "policy_revision": "policy-v1",
                "policy_digest": "sha256:" + "7" * 64,
                "policy_decision_id": "decision:1",
                "tenant_id": tenant,
                "idempotency_key": key,
                "expected_revision": None,
                "trace_id": None,
                "created_at_ms": int(time.time() * 1000),
            }

        bindings = {
            (role, target): library.ToolBinding(f"{target}-mcp", f"{role}_tool")
            for role in library.ROLES
            for target in library.TARGETS
        }
        return await library.publish_deployment_library(
            send, context, _TENANT, bindings
        )

    with _heavy_engine_slot():
        port = _free_port()
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
            env=dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            published = asyncio.run(run(port))
        finally:
            engine.terminate()
            engine.wait(timeout=60)
    assert published == dict.fromkeys(library.TARGETS, True)
