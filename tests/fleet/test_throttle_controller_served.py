"""Outcome capture, the controller loop and the admission limiter.

ARBORA-CAPACITY-R001.4 and R001.5. Fleet traffic enters ``handle_fleet_call``,
the handler the served invoke dispatches to, with the recorder and limiter the
composition binds; the engine is a recorded port and the clock is controlled.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.fleet.error_budget import (
    BudgetWindow,
    OutcomeClass,
    OutcomeSample,
    ThrottleMode,
)
from arbora.api.domain.fleet.throttle_service import (
    CAPACITY_THROTTLED,
    CapacityThrottled,
    ControllerConfig,
    PartitionLimiter,
    classify_outcome,
)
from arbora.api.errors import ArboraErrorCode, to_envelope
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import fleet
from tests.fleet._capacity_support import (
    OTHER,
    PARTITION,
    FakePort,
    controller,
    fill,
)
from tests.fleet._support import CAPACITY_CONFIG


class Gateway:
    """Answers or fails each child call as the test scripts it."""

    def __init__(self) -> None:
        self.script: list[BaseException | None] = []
        self.calls = 0

    async def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        return {"ok": True}


def _context(registry, recorder, limiter, gateway, partition=PARTITION):
    services = {
        "fleet_gateway": gateway,
        "throttle_registry": registry,
        "outcome_recorder": recorder,
        "partition_limiter": limiter,
    }
    return SimpleNamespace(
        caller=SimpleNamespace(
            tenant=partition.tenant,
            principal="user:one",
            policy_revision=partition.policy_revision,
        ),
        client=object(),
        services=services,
        idempotency_key=None,
        service_identity=False,
        owner="user:one",
        fleet_decision=SimpleNamespace(effect=_effect(partition)),
        registry_digest="d",
    )


def _effect(partition):
    from arbora.api.registry import Effect

    return Effect.READ if partition.operation_class == "read" else Effect.WRITE


async def _call(context, partition=PARTITION) -> Any:
    (op,) = [o for o in fleet.operations() if o.id == "fleet.call"]
    params = {"server": partition.child, "tool": "t", "arguments": {}}
    return await fleet._gateway_call(
        context.services["fleet_gateway"], context, params, op
    )


def _stack(mode: ThrottleMode = ThrottleMode.ENFORCE, **kwargs: Any):
    built, registry, recorder, clock = controller(FakePort(), **kwargs)
    registry.set_mode(PARTITION, mode)
    return built, registry, recorder, clock, PartitionLimiter(registry)


@pytest.mark.spec("ARBORA-CAPACITY-R001.4")
async def test_breach_narrows_recovery_is_bounded_and_replay_is_idempotent() -> None:
    built, registry, recorder, clock, limiter = _stack()
    gateway = Gateway()
    context = _context(registry, recorder, limiter, gateway)
    # 12 failures and 8 successes through the served handler: a breach.
    gateway.script = [RuntimeError("child down")] * 12 + [None] * 8
    for _ in range(20):
        try:
            await _call(context)
        except RuntimeError:
            pass
    clock.advance(60)
    (breach,) = await built.tick()
    assert breach.reason == "budget_breach_decrease" and breach.new_limit == 5
    assert registry.status(PARTITION).current_limit == 5
    # A healthy window recovers by alpha and never above the engine ceiling.
    for _ in range(20):
        await _call(context)
    clock.advance(60)
    (healthy,) = await built.tick()
    assert healthy.new_limit <= 10 and healthy.new_limit >= 5
    # Replaying a window digest returns the stored decision and changes nothing.
    window = BudgetWindow(
        "replay",
        PARTITION,
        tuple(OutcomeSample(OutcomeClass.SUCCESS) for _ in range(30)),
    )
    first = await built.process_window(window, 0, 1)
    again = await built.process_window(window, 0, 1)
    assert again is first
    # Another tenant's partition is never influenced.
    assert registry.status(OTHER).current_limit is None


@pytest.mark.spec("ARBORA-CAPACITY-R001.4")
async def test_caller_errors_and_policy_denials_stay_ineligible_and_small_windows_hold() -> (
    None
):
    assert classify_outcome(OperationRefused("INVALID_ARGUMENT")) is (
        OutcomeClass.PERMANENT_CALLER_ERROR
    )
    assert (
        classify_outcome(OperationRefused("SCOPE_REQUIRED"))
        is OutcomeClass.POLICY_DENIAL
    )
    assert classify_outcome(TimeoutError()) is OutcomeClass.TIMEOUT
    assert classify_outcome(RuntimeError()) is OutcomeClass.RETRYABLE_SERVICE_ERROR
    built, registry, recorder, clock, _ = _stack()
    fill(recorder, PARTITION, caller=30)
    clock.advance(60)
    (decision,) = await built.tick()
    assert decision.reason == "insufficient_samples"
    assert decision.new_limit == decision.prior_limit


@pytest.mark.spec("ARBORA-CAPACITY-R001.4")
def test_aimd_config_applies_documented_defaults_and_bounds() -> None:
    config = ControllerConfig.from_section({})
    assert (config.aimd.alpha, config.aimd.beta, config.aimd.floor) == (1, 0.5, 1)
    assert (config.aimd.min_sample_count, config.window_seconds) == (20, 60)
    assert config.aimd.error_budget_fraction == 0.1
    for bad in (
        {"alpha": 65},
        {"beta": 0.95},
        {"window_seconds": 5},
        {"floor": 101},
        {"min_sample_count": 0},
        {"error_budget_fraction": 0.9},
    ):
        with pytest.raises(ValueError):
            ControllerConfig.from_section(bad)
    assert CAPACITY_CONFIG.alpha == 2


@pytest.mark.spec("ARBORA-CAPACITY-R001.5")
async def test_enforce_refuses_over_limit_and_observe_admits() -> None:
    built, registry, recorder, clock, limiter = _stack()
    gateway = Gateway()
    context = _context(registry, recorder, limiter, gateway)
    fill(recorder, PARTITION, ok=10, bad=10)
    clock.advance(60)
    await built.tick()
    limit = registry.status(PARTITION).current_limit
    assert limit == 5
    # With ``limit`` calls in flight the next one is refused with a 429.
    held = [limiter.acquire(PARTITION) for _ in range(limit)]
    for entered in held:
        await entered.__aenter__()
    with pytest.raises(OperationRefused) as refused:
        await _call(context)
    assert refused.value.code == CAPACITY_THROTTLED and gateway.calls == 0
    status, body = to_envelope(
        refused.value, op="fleet.call", request_id="r", registry_digest="d"
    )
    assert status == 429 and body["error"]["code"] == CAPACITY_THROTTLED
    assert ArboraErrorCode(CAPACITY_THROTTLED)
    # A refusal is not an outcome of the child and teaches the controller nothing.
    assert recorder.partitions() == ()
    for entered in held:
        await entered.__aexit__(None, None, None)
    assert await _call(context) == {"value": {"ok": True}}
    # Observe mode admits every call and counts what enforce would refuse.
    registry.set_mode(PARTITION, ThrottleMode.OBSERVE)
    held = [limiter.acquire(PARTITION) for _ in range(limit)]
    for entered in held:
        await entered.__aenter__()
    assert await _call(context) == {"value": {"ok": True}}
    assert registry.would_refuse_count(PARTITION) == 2
    for entered in held:
        await entered.__aexit__(None, None, None)


@pytest.mark.spec("ARBORA-CAPACITY-R001.5")
async def test_first_refusal_per_window_is_reported_once_and_no_decision_means_no_limit() -> (
    None
):
    _, registry, _, _, _ = _stack()
    seen: list[Any] = []
    limiter = PartitionLimiter(registry, on_first_refusal=seen.append)
    async with limiter.acquire(PARTITION):  # no decision, so no limit
        assert limiter.in_flight(PARTITION) == 1
    registry.record_decision(
        __import__("dataclasses").replace(await _seed_decision(), new_limit=1)
    )
    async with limiter.acquire(PARTITION):
        with pytest.raises(CapacityThrottled) as refused:
            async with limiter.acquire(PARTITION):
                pass
        assert refused.value.code == CAPACITY_THROTTLED
        with pytest.raises(CapacityThrottled):
            async with limiter.acquire(PARTITION):
                pass
    assert seen == [PARTITION]


async def _seed_decision():
    from arbora.api.domain.fleet.error_budget import ThrottleDecision

    return ThrottleDecision(
        window_digest="seed",
        partition=PARTITION,
        config_version="v1",
        prior_limit=1,
        new_limit=1,
        mode=ThrottleMode.ENFORCE,
        reason="seed",
        sample_count=0,
        eligible_failure_count=0,
    )
