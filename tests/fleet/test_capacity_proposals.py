"""The capacity proposal builder (ARBORA-CAPACITY-R001.3.1)."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from arbora.api.domain.fleet.error_budget import ThrottleDecision, ThrottleMode
from arbora.fleet.capacity_proposals import (
    PROPOSAL_FIELD_INVALID,
    CapacityProposalError,
    build_proposal,
)
from tests.fleet._capacity_support import CELL, PARTITION


def _decision(prior: int = 4, new: int = 6) -> ThrottleDecision:
    return ThrottleDecision(
        window_digest="a" * 64,
        partition=PARTITION,
        config_version="v1",
        prior_limit=prior,
        new_limit=new,
        mode=ThrottleMode.ENFORCE,
        reason="healthy_window_increase",
        sample_count=20,
        eligible_failure_count=0,
    )


def _build(decision: ThrottleDecision, cell=CELL):
    return build_proposal(
        decision, cell, window_start_ms=1000, window_end_ms=61_000, policy_revision=3
    )


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.1")
def test_builder_fills_fields_and_digest_is_stable() -> None:
    proposal = _build(_decision())
    assert proposal is not None
    assert (proposal.current_capacity, proposal.proposed_capacity) == (4, 6)
    assert (proposal.min_capacity, proposal.max_capacity) == (1, 10)
    assert (proposal.measured_load, proposal.available_headroom) == (3, 7)
    assert (proposal.window_start_ms, proposal.window_end_ms) == (1000, 61_000)
    assert (proposal.premise_revision, proposal.policy_revision) == (4, 3)
    assert proposal.tenant_id == PARTITION.tenant and proposal.unit == "slots"
    assert re.fullmatch(r"[0-9a-f]{64}", proposal.proposal_digest)
    assert _build(_decision()) == proposal
    other = _build(_decision(new=7))
    assert other is not None and other.proposal_digest != proposal.proposal_digest
    assert set(proposal.wire()) >= {"proposal_digest", "proposal_id", "tenant_id"}


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.1")
def test_a_decision_that_keeps_the_limit_builds_no_proposal() -> None:
    assert _build(_decision(prior=4, new=4)) is None


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.1")
@pytest.mark.parametrize(
    "cell",
    [
        replace(CELL, measured_load=-1),
        replace(CELL, available_headroom=-1),
        replace(CELL, unit=""),
        replace(CELL, premise_revision=-2),
    ],
)
def test_an_invalid_field_refuses_before_any_engine_call(cell) -> None:
    with pytest.raises(CapacityProposalError) as refused:
        _build(_decision(), cell)
    assert refused.value.code == PROPOSAL_FIELD_INVALID


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.1")
def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(CapacityProposalError):
        build_proposal(
            _decision(), CELL, window_start_ms=9, window_end_ms=1, policy_revision=1
        )
