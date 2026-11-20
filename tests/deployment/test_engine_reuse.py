"""A reused engine only attaches and survives host shutdown (ARBORA-DEPLOY-R018.7, R018.8)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from arbora.core.settings import runtime_config
from arbora.deployment import engine_lock
from arbora.mcp_server import composition, served_engine
from arbora.mcp_server.composition import (
    EngineEndpoint,
    EngineRefusal,
    LocalEngineConfig,
    attach_reused_engine,
    probe_engine,
    stand_down_engine,
)


@pytest.mark.spec("ARBORA-DEPLOY-R018.7")
def test_two_hosts_reuse_one_engine_and_unavailable_reuse_starts_zero(
    independent_engine: Callable[[], int],
    free_port: Callable[[], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = independent_engine()
    live = EngineEndpoint("tcp", f"127.0.0.1:{port}")
    before = composition.engine_spawn_count()
    # Two hosts reuse the one engine and neither starts anything.
    assert attach_reused_engine(live, 5).state == "running"
    assert attach_reused_engine(live, 5).state == "running"
    assert composition.engine_spawn_count() == before

    # An older engine than this release serves refuses, and starts nothing.
    monkeypatch.setattr(composition, "engine_version_floor", lambda: (99, 0, 0))
    with pytest.raises(EngineRefusal) as incompatible:
        attach_reused_engine(live, 5)
    assert incompatible.value.code == "REUSE_INCOMPATIBLE"
    monkeypatch.undo()

    # A dead reused endpoint refuses and never authorizes a local replacement.
    dead = EngineEndpoint("tcp", f"127.0.0.1:{free_port()}")
    with pytest.raises(EngineRefusal) as unreachable:
        attach_reused_engine(dead, 0.5)
    assert unreachable.value.code == "REUSE_UNREACHABLE"

    # Through the pool: the reuse mark makes a dead configured endpoint refuse at once.
    monkeypatch.setattr(runtime_config, "graph_service_endpoints", [dead.address])
    monkeypatch.setenv("ENGINE_REUSE", "true")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a reused engine never starts a local replacement")

    monkeypatch.setattr(composition, "ensure_local_engine", forbidden)
    served_engine.reset_process_engine()
    try:
        with pytest.raises(EngineRefusal) as refused:
            served_engine.process_engine_pool()
        assert refused.value.code == "REUSE_UNREACHABLE"
    finally:
        served_engine.reset_process_engine()
    assert composition.engine_spawn_count() == before


@pytest.mark.spec("ARBORA-DEPLOY-R018.8")
def test_reused_engine_stays_reachable_after_both_clients_stop(
    independent_engine: Callable[[], int],
    start_child: Callable[[int], object],
) -> None:
    port = independent_engine()
    endpoint = EngineEndpoint("tcp", f"127.0.0.1:{port}")
    first = start_child(port)
    second = start_child(port)
    assert first.report["spawned"] == 0  # type: ignore[attr-defined]
    assert second.report["spawned"] == 0  # type: ignore[attr-defined]
    assert not engine_lock.engine_marker_path(endpoint.address).exists()
    first.finish()  # type: ignore[attr-defined]
    second.finish()  # type: ignore[attr-defined]
    # An independent probe after both hosts exited: the reused engine is still up.
    assert probe_engine(endpoint, 5).state == "running"


@pytest.mark.spec("ARBORA-DEPLOY-R018.8")
def test_unknown_ownership_refuses_cleanup_and_leaves_the_engine(
    independent_engine: Callable[[], int],
    runtime_dir: Path,
    tmp_path: Path,
    clean_attachments: None,
) -> None:
    port = independent_engine()
    config = LocalEngineConfig(port=port, persist_dir=tmp_path / "unused")
    endpoint = config.endpoint
    marker = engine_lock.engine_marker_path(endpoint.address)

    for content in ("not json", json.dumps({"pid": 2_000_000_000})):
        assert composition.ensure_local_engine(config) == endpoint
        marker.write_text(content)
        with pytest.raises(EngineRefusal) as unknown:
            stand_down_engine(endpoint)
        assert unknown.value.code == "OWNERSHIP_UNKNOWN"
        assert probe_engine(endpoint, 5).state == "running"
        marker.unlink()
