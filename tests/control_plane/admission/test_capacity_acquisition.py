"""ARBORA-FLEET-R009.1: all-or-nothing capacity acquisition on the one ledger."""

from __future__ import annotations

import pytest

from arbora.control_plane.runs import CapacityLedger, CapacityReDecisionExhaustedError


@pytest.mark.spec("ARBORA-FLEET-R009.1")
def test_acquire_grants_all_or_nothing() -> None:
    ledger = CapacityLedger(3)

    decision = ledger.acquire("run-1", 3)

    assert decision.granted is True
    assert ledger.held_by("run-1") == 3


@pytest.mark.spec("ARBORA-FLEET-R009.1")
def test_denial_grants_exactly_one_redecision_then_fails_closed() -> None:
    ledger = CapacityLedger(1)

    assert ledger.acquire("run-1", 2).granted is False
    assert ledger.held_by("run-1") == 0

    with pytest.raises(CapacityReDecisionExhaustedError):
        ledger.acquire("run-1", 2)


@pytest.mark.spec("ARBORA-FLEET-R009.1")
def test_partial_unavailability_holds_nothing() -> None:
    ledger = CapacityLedger(2)
    assert ledger.acquire("run-0", 1).granted is True

    assert ledger.acquire("run-1", 2).granted is False
    assert ledger.held_by("run-1") == 0
    assert ledger.available == 1


@pytest.mark.spec("ARBORA-FLEET-R009.1")
def test_stopped_run_releases_held_capacity() -> None:
    ledger = CapacityLedger(1)

    assert ledger.acquire("run-1", 1).granted is True
    assert ledger.acquire("run-2", 1).granted is False

    assert ledger.release("run-1") == 1

    assert ledger.held_by("run-1") == 0
    assert ledger.acquire("run-2", 1).granted is True


@pytest.mark.spec("ARBORA-FLEET-R009.1")
def test_release_resets_the_redecision_budget_even_with_nothing_held() -> None:
    ledger = CapacityLedger(1)

    assert ledger.acquire("run-1", 1).granted is True
    assert ledger.acquire("run-2", 2).granted is False  # consumes run-2's redecision

    assert (
        ledger.release("run-2") == 0
    )  # a stop clears the budget though nothing was held

    assert ledger.acquire("run-2", 2).granted is False
    with pytest.raises(CapacityReDecisionExhaustedError):
        ledger.acquire("run-2", 2)
