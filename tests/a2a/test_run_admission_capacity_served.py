"""Routing and admission publish only through acquired capacity (ARBORA-FLEET-R009.2, R009.3)."""

from __future__ import annotations

import pytest

from arbora.a2a.routing import A2ACapacityRefused, OrchestratorA2ARouter
from arbora.control_plane.runs import (
    AuditChain,
    CapacityLedger,
    InMemoryNativeAdmission,
)
from arbora.control_plane.runs.admission import (
    CapacityAcquisition,
    CapacityDeniedError,
    CapacityRetryError,
    NativeAdmissionRequest,
)
from tests.control_plane.test_policy_runs import (
    EngineDouble,
    _admission_request,
    _digest,
    _engine_policy,
    _resolution,
)


def _request(label: str, key: str = "request:001") -> NativeAdmissionRequest:
    resolution = _resolution(request_digest=_digest(label))
    resolution = resolution.model_copy(update={"idempotency_key": key})
    return _admission_request(resolution)


def _admission(capacity: CapacityAcquisition) -> InMemoryNativeAdmission:
    return InMemoryNativeAdmission(
        audit=AuditChain(max_events=16),
        clock=lambda: 100,
        policy=_engine_policy(EngineDouble()),
        capacity=capacity,
    )


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


@pytest.mark.spec("ARBORA-FLEET-R009.3")
async def test_all_or_nothing_and_release() -> None:
    first = _request("one")
    second = _request("one", "request:002")
    capacity = CapacityAcquisition(capacity={"run": 1})
    admission = _admission(capacity)
    await admission.admit_once(first)
    run_one = first.resolution.run_id
    assert capacity.held(run_one) == {"run": 1}
    with pytest.raises(CapacityRetryError):
        await admission.admit_once(second)
    run_two = second.resolution.run_id
    assert admission.read(run_two) is None and capacity.held(run_two) == {}
    assert admission.audit is not None and len(admission.audit._events) == 2
    with pytest.raises(CapacityDeniedError):
        await admission.admit_once(second)
    admission.stop(run_one)
    assert capacity.held(run_one) == {}
    admission.stop(run_two)
    await admission.admit_once(second)
    assert admission.read(run_two) is not None and capacity.held(run_two) == {"run": 1}


@pytest.mark.spec("ARBORA-FLEET-R009.3")
async def test_replay_does_not_acquire_twice() -> None:
    request = _request("replay")
    capacity = CapacityAcquisition(capacity={"run": 1})
    admission = _admission(capacity)
    assert (await admission.admit_once(request)).created is True
    assert (await admission.admit_once(request)).created is False
    assert capacity.held(request.resolution.run_id) == {"run": 1}
