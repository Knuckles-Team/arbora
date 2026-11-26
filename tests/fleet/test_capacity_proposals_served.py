"""The closed capacity loop: verdict, apply and receipt (ARBORA-CAPACITY-R001.3, R751).

The controller runs on a controlled clock against a recorded engine port; the
engine verdict is a recorded ``Decide`` answer. Nothing sleeps.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from arbora.fleet.capacity_proposals import (
    CAPACITY_APPLY_MISMATCH,
    CAPACITY_EPOCH_CONFLICT,
    VERDICT_UNAVAILABLE,
)
from arbora.fleet.error_budget import ThrottleDecision, ThrottleMode
from tests.fleet._capacity_support import (
    CELL,
    PARTITION,
    FakePort,
    controller,
    fill,
)

HEALTHY = {"ok": 20}


class Verdicts:
    """A recorded engine answer for every capacity question."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer, self.asked = answer, []

    async def __call__(self, proposal: dict[str, Any]) -> str:
        self.asked.append(proposal)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _seed(registry, limit: int = 2, mode=ThrottleMode.ENFORCE) -> None:
    registry.set_mode(PARTITION, mode)
    registry.record_decision(
        ThrottleDecision(
            window_digest="seed",
            partition=PARTITION,
            config_version="v1",
            prior_limit=limit,
            new_limit=limit,
            mode=mode,
            reason="seed",
            sample_count=0,
            eligible_failure_count=0,
        )
    )


async def _window(built, recorder, clock, **counts: int) -> ThrottleDecision:
    fill(recorder, PARTITION, **counts)
    clock.advance(60)
    (decision,) = await built.tick()
    return decision


@pytest.mark.spec("ARBORA-CAPACITY-R001.3")
@pytest.mark.spec("ARBORA-CAPACITY-R751")
async def test_allowed_proposal_is_applied_and_receipted() -> None:
    port, ask = FakePort(), Verdicts("allow")
    built, registry, recorder, clock = controller(port, ask=ask)
    _seed(registry, limit=2)
    decision = await _window(built, recorder, clock, **HEALTHY)
    assert decision.new_limit == 4 and decision.reason == "healthy_window_increase"
    assert registry.status(PARTITION).current_limit == 4
    (proposal,) = ask.asked
    assert proposal["proposed_capacity"] == 4 and proposal["current_capacity"] == 2
    receipt = registry.latest_receipt(PARTITION)
    assert receipt is not None and receipt.outcome == "raised"
    assert receipt.proposal_digest == proposal["proposal_digest"]
    assert receipt.applied_capacity == 4 and receipt.cell_id == CELL.cell_id
    # The receipt came from a status read taken after the apply.
    assert port.names[-1] == "cell"


@pytest.mark.spec("ARBORA-CAPACITY-R751")
async def test_raise_requires_allow_verdict() -> None:
    for answer in (
        "refuse:CAPACITY_HEADROOM_EXCEEDED",
        RuntimeError("engine down"),
        None,
    ):
        port = FakePort()
        built, registry, recorder, clock = controller(
            port, ask=None if answer is None else Verdicts(answer)
        )
        _seed(registry, limit=2)
        decision = await _window(built, recorder, clock, **HEALTHY)
        assert registry.status(PARTITION).current_limit == 2
        assert decision.reason.startswith("raise_held:")
        assert "update_cell" not in port.names
    # A narrowing needs no verdict at all.
    port, ask = FakePort(), Verdicts("allow")
    built, registry, recorder, clock = controller(port, ask=ask)
    _seed(registry, limit=8)
    narrowed = await _window(built, recorder, clock, ok=10, bad=10)
    assert narrowed.new_limit == 4 and ask.asked == []
    assert registry.status(PARTITION).current_limit == 4


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.2")
async def test_refused_and_unavailable_verdicts_hold_the_limit() -> None:
    codes = {}
    for label, answer in {
        "refused": "refuse:CAPACITY_PREMISE_STALE",
        "failed": RuntimeError("engine down"),
        "absent": None,
    }.items():
        built, registry, recorder, clock = controller(
            FakePort(), ask=None if answer is None else Verdicts(answer)
        )
        _seed(registry, limit=2)
        await _window(built, recorder, clock, **HEALTHY)
        receipt = registry.latest_receipt(PARTITION)
        assert registry.status(PARTITION).current_limit == 2
        assert receipt is not None and receipt.outcome == "held"
        codes[label] = registry.status(PARTITION).reason
    assert codes["refused"] == "raise_held:CAPACITY_PREMISE_STALE"
    assert codes["failed"] == codes["absent"] == f"raise_held:{VERDICT_UNAVAILABLE}"


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.3")
async def test_apply_modes_and_epoch_conflict() -> None:
    # observe: nothing is applied and the receipt says so.
    observed = FakePort()
    built, registry, recorder, clock = controller(observed, ask=Verdicts("allow"))
    _seed(registry, limit=2, mode=ThrottleMode.OBSERVE)
    await _window(built, recorder, clock, **HEALTHY)
    receipt = registry.latest_receipt(PARTITION)
    assert receipt is not None and receipt.outcome == "observed_only"
    assert not {"update_cell", "throttle_cell"} & set(observed.names)
    # enforce, narrowing: the engine learns the window through the throttle call.
    narrowed = FakePort()
    built, registry, recorder, clock = controller(narrowed, ask=Verdicts("allow"))
    _seed(registry, limit=8)
    await _window(built, recorder, clock, ok=10, bad=10)
    assert ("throttle_cell", (10, 20, clock.now_ms)) in narrowed.calls
    # enforce, a raise past the cell ceiling calls the engine update once.
    ceiling = FakePort(replace(CELL, capacity=4, max_capacity=4))
    built, registry, recorder, clock = controller(ceiling, ask=Verdicts("allow"))
    _seed(registry, limit=4)
    registry_cell = replace(CELL, capacity=4, max_capacity=4)
    ceiling.default = registry_cell
    proposal_limit = 6
    from arbora.fleet.capacity_proposals import (
        CapacityVerdict,
        VerdictKind,
        apply,
        build_proposal,
    )

    decision = ThrottleDecision(
        window_digest="b" * 64,
        partition=PARTITION,
        config_version="v1",
        prior_limit=4,
        new_limit=proposal_limit,
        mode=ThrottleMode.ENFORCE,
        reason="healthy_window_increase",
        sample_count=20,
        eligible_failure_count=0,
    )
    proposal = build_proposal(
        decision, registry_cell, window_start_ms=0, window_end_ms=1, policy_revision=1
    )
    assert proposal is not None
    verdict = CapacityVerdict(
        VerdictKind.ALLOW, "allow", proposal.proposal_digest, 1, 4
    )
    done = await apply(
        ceiling, proposal, verdict, mode=ThrottleMode.ENFORCE, cell=registry_cell
    )
    assert done.outcome == "ceiling_raised" and ceiling.names.count("update_cell") == 1
    # a stale epoch holds and re-proposes at the next window
    ceiling.update_outcome = "stale_epoch"
    stale = await apply(
        ceiling, proposal, verdict, mode=ThrottleMode.ENFORCE, cell=registry_cell
    )
    assert stale.outcome == "held" and stale.code == CAPACITY_EPOCH_CONFLICT


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.3")
async def test_a_replica_cell_submits_a_scaling_intent_not_a_cell_update() -> None:
    from arbora.fleet.capacity_proposals import (
        CapacityVerdict,
        VerdictKind,
        apply,
        build_proposal,
    )

    cell = replace(CELL, capacity=4, max_capacity=4, replicas=True)
    decision = ThrottleDecision(
        window_digest="c" * 64,
        partition=PARTITION,
        config_version="v1",
        prior_limit=4,
        new_limit=6,
        mode=ThrottleMode.ENFORCE,
        reason="healthy_window_increase",
        sample_count=20,
        eligible_failure_count=0,
    )
    proposal = build_proposal(
        decision, cell, window_start_ms=0, window_end_ms=1, policy_revision=1
    )
    assert proposal is not None

    class Intents:
        submitted: list[Any] = []

        async def submit(self, item: Any) -> None:
            self.submitted.append(item)

    port, intents = FakePort(cell), Intents()
    verdict = CapacityVerdict(
        VerdictKind.ALLOW, "allow", proposal.proposal_digest, 1, 4
    )
    done = await apply(
        port, proposal, verdict, mode=ThrottleMode.ENFORCE, cell=cell, intents=intents
    )
    assert done.outcome == "intent_submitted" and intents.submitted == [proposal]
    assert "update_cell" not in port.names


@pytest.mark.spec("ARBORA-CAPACITY-R001.3.4")
async def test_receipt_records_a_mismatch_between_request_and_fresh_state() -> None:
    class ShrinksAfterTheDecision(FakePort):
        """The first read feeds the decision; later reads see a smaller cell."""

        reads = 0

        async def cell(self, partition):
            self.reads += 1
            fresh = await super().cell(partition)
            if self.reads == 1:
                return fresh
            return replace(fresh, capacity=1, max_capacity=1)

    built, registry, recorder, clock = controller(
        ShrinksAfterTheDecision(), ask=Verdicts("allow")
    )
    _seed(registry, limit=2)
    await _window(built, recorder, clock, **HEALTHY)
    receipt = registry.latest_receipt(PARTITION)
    assert receipt is not None and receipt.outcome == CAPACITY_APPLY_MISMATCH
    assert receipt.applied_capacity == 1
