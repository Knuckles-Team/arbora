"""The served engine pool wires the local lifecycle (ARBORA-DEPLOY-R018.9)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.deployment import engine_lock
from arbora.epistemic import ClientContext
from arbora.mcp_server import composition, served_engine
from arbora.mcp_server.composition import LocalEngineConfig
from arbora.security import anonymous


@pytest.mark.spec("ARBORA-DEPLOY-R018.9")
@pytest.mark.spec("ARBORA-DEPLOY-R600.3")
def test_unset_endpoints_start_one_loopback_engine_and_serve_a_call(
    monkeypatch: pytest.MonkeyPatch,
    runtime_dir: Path,
    tmp_path: Path,
    free_port: Callable[[], int],
) -> None:
    port = free_port()
    # The explicit local no-auth mode: the pool then connects without credentials.
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    config = RuntimeConfig()
    config.graph_service_endpoints = None
    monkeypatch.setattr(
        composition,
        "local_engine_config",
        lambda *_, **__: LocalEngineConfig(
            port=port,
            persist_dir=tmp_path / "store",
            extra_args=("--auth-mode", "none"),
            start_timeout_s=90,
            stop_grace_s=20,
        ),
    )
    handles = served_engine.EngineHandles(config=config)
    try:
        pool = handles.pool()
        assert handles.attachments.spawned == 1
        marker = engine_lock.engine_marker_path(f"127.0.0.1:{port}")
        assert json.loads(marker.read_text())["pid"] > 0

        context = ClientContext(
            principal="anonymous",
            tenant="local",
            audience="medulla",
            agent_id="anonymous",
            roles=(),
            scopes=("kg:read",),
            policy_version="no-auth",
            auth_mode="none",
        )

        async def call() -> object:
            client = await pool.connected_client(context, "__commons__")
            return await client.health()

        reply = asyncio.run(call())
        assert isinstance(reply, dict) and reply["status"] == "ok"
    finally:
        handles.reset()
        composition._stand_down_all(handles.attachments)


@pytest.mark.spec("ARBORA-DEPLOY-R018.9")
def test_set_endpoint_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # setattr would restore the frozen live value as a permanent override at
    # teardown; setitem drops the key so later reads stay live.
    config = RuntimeConfig()
    config.graph_service_endpoints = ["127.0.0.1:1"]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a configured endpoint must never start a local engine")

    monkeypatch.setattr(composition, "ensure_local_engine", forbidden)
    handles = served_engine.EngineHandles(config=config)
    try:
        handles.pool()
        assert handles.attachments.spawned == 0
    finally:
        handles.reset()


# These tests boot a real engine: the pull-request subset deselects the marker.
pytestmark = pytest.mark.real_boot
