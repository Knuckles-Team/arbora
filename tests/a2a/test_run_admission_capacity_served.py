"""Routing and admission publish only through acquired capacity (ARBORA-FLEET-R009.2, R009.3)."""

from __future__ import annotations

import pytest

from arbora.a2a.routing import A2ACapacityRefused, OrchestratorA2ARouter
from arbora.control_plane.runs import CapacityLedger


@pytest.mark.spec("ARBORA-FLEET-R009.2")
def test_evaluate_commit_publish_with_capacity() -> None:
    ledger = CapacityLedger(2)
    router = OrchestratorA2ARouter(lambda: None, capacity=ledger)
    router.acquire_capacity("run-a", 2)
    assert ledger.held_by("run-a") == 2 and ledger.available == 0
    with pytest.raises(A2ACapacityRefused) as first:
        router.acquire_capacity("run-b", 1)
    assert first.value.code == "CAPACITY_REDECISION_OFFERED"
    with pytest.raises(A2ACapacityRefused) as second:
        router.acquire_capacity("run-b", 1)
    assert second.value.code == "CAPACITY_REDECISION_EXHAUSTED"
    assert router.stop("run-a") == 2
    assert ledger.available == 2 and ledger.held_by("run-b") == 0


@pytest.mark.spec("ARBORA-FLEET-R009.2")
def test_redecision_after_release_publishes() -> None:
    ledger = CapacityLedger(1)
    router = OrchestratorA2ARouter(lambda: None, capacity=ledger)
    router.acquire_capacity("holder", 1)
    with pytest.raises(A2ACapacityRefused):
        router.acquire_capacity("late", 1)
    router.stop("holder")
    router.acquire_capacity("late", 1)  # the one re-decision, now granted
    assert ledger.held_by("late") == 1
    assert OrchestratorA2ARouter(lambda: None).stop("none") == 0
