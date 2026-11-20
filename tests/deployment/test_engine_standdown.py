"""The last client out stops the engine this host started (ARBORA-DEPLOY-R018.5)."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from arbora.deployment import engine_lock
from arbora.mcp_server.composition import parse_engine_endpoint, probe_engine


@pytest.mark.spec("ARBORA-DEPLOY-R018.5")
def test_last_client_exit_stops_engine_and_early_exit_does_not(
    start_child: Callable[[int], object],
    free_port: Callable[[], int],
) -> None:
    port = free_port()
    owner = start_child(port)
    peer = start_child(port)
    endpoint = parse_engine_endpoint(owner.report["address"])  # type: ignore[attr-defined]
    marker = engine_lock.engine_marker_path(endpoint.address)
    assert marker.exists()

    # The owner leaves first: a client remains, so the engine keeps running.
    owner.finish()  # type: ignore[attr-defined]
    assert probe_engine(endpoint, 5).state == "running"
    assert marker.exists()

    # The last client leaves: the engine this host started is stopped.
    peer.finish()  # type: ignore[attr-defined]
    deadline = time.monotonic() + 30
    while probe_engine(endpoint, 1).state != "absent":
        assert time.monotonic() < deadline, "the engine was not stopped"
        time.sleep(0.5)
    assert not marker.exists()
