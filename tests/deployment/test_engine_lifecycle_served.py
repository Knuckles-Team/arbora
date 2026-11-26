"""The served engine pool wires the local lifecycle (ARBORA-DEPLOY-R018.9)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from arbora.core.settings import runtime_config
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
    clean_attachments: None,
) -> None:
    port = free_port()
    # The explicit local no-auth mode: the pool then connects without credentials.
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    # setattr would restore the frozen live value as a permanent override at
    # teardown; setitem drops the key so later reads stay live.
    monkeypatch.setitem(runtime_config._overrides, "graph_service_endpoints", None)
    monkeypatch.setattr(
        composition,
        "local_engine_config",
        lambda **_: LocalEngineConfig(
            port=port,
            persist_dir=tmp_path / "store",
            extra_args=("--auth-mode", "none"),
            start_timeout_s=90,
            stop_grace_s=20,
        ),
    )
    served_engine.reset_process_engine()
    before = composition.engine_spawn_count()
    try:
        pool = served_engine.process_engine_pool()
        assert composition.engine_spawn_count() == before + 1
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
        served_engine.reset_process_engine()


@pytest.mark.spec("ARBORA-DEPLOY-R018.9")
def test_set_endpoint_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # setattr would restore the frozen live value as a permanent override at
    # teardown; setitem drops the key so later reads stay live.
    monkeypatch.setitem(
        runtime_config._overrides, "graph_service_endpoints", ["127.0.0.1:1"]
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a configured endpoint must never start a local engine")

    monkeypatch.setattr(composition, "ensure_local_engine", forbidden)
    served_engine.reset_process_engine()
    before = composition.engine_spawn_count()
    try:
        served_engine.process_engine_pool()
        assert composition.engine_spawn_count() == before
    finally:
        served_engine.reset_process_engine()


# These tests boot a real engine: the pull-request subset deselects the marker.
pytestmark = pytest.mark.real_boot
