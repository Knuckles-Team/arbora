"""A second process attaches to the running engine and spawns none (ARBORA-DEPLOY-R018.4)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from arbora.deployment import engine_lock
from arbora.mcp_server.composition import parse_engine_endpoint, probe_engine


@pytest.mark.spec("ARBORA-DEPLOY-R018.4")
def test_two_processes_one_engine_spawn_count_is_one(
    start_child: Callable[[int], object],
    free_port: Callable[[], int],
) -> None:
    port = free_port()
    first = start_child(port)
    second = start_child(port)
    assert first.report["spawned"] == 1  # type: ignore[attr-defined]
    assert second.report["spawned"] == 0  # type: ignore[attr-defined]
    assert first.report["address"] == second.report["address"]  # type: ignore[attr-defined]

    endpoint = parse_engine_endpoint(first.report["address"])  # type: ignore[attr-defined]
    probe = probe_engine(endpoint, 5)
    assert probe.state == "running"
    # One owner marker: exactly one engine was started on this host.
    assert len(list(engine_lock._runtime_base().glob("engine-*.owner"))) == 1

    second.finish()  # type: ignore[attr-defined]
    first.finish()  # type: ignore[attr-defined]
