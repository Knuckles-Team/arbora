"""Public-surface behaviour of durable, idempotent scale-intent reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arbora.fleet.scaling.scaling_intent_reconciler import (
    AllowScalePolicy,
    BreakGlassAuthorization,
    MemoryScaleIntentLedger,
    ScaleActuationRequest,
    ScaleActuationResult,
    ScaleIntentConflict,
    ScaleIntentReconciler,
    ScaleIntentRecord,
    ScaleIntentState,
    ScaleLeaseUnavailable,
    ScalePolicy,
    ScalePolicyDecision,
)

pytestmark = pytest.mark.spec("GRAPHOS-CAPACITY-R750.752")

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
TARGET = {
    "cluster_id": "cluster-1",
    "runtime_id": "runtime-1",
    "workload_id": "workload-1",
    "target_uid": "target-uid-1",
    "target_resource_version": "rv-1",
}
INTENT: dict[str, Any] = {
    "intent_id": "scale-intent-1",
    "intent_revision": 1,
    "expected_unit_revision": 7,
    "desired_replicas": 3,
    "min_replicas": 1,
    "max_replicas": 5,
    "controller_mode": "native",
    "replica_writer_id": "writer-1",
    "execution_key": "scale-execution-1",
    "fence": {
        "lease_id": "intent-fence-1",
        "lease_epoch": 4,
        "fence_token": 9,
        "expires_at": NOW + timedelta(minutes=10),
    },
    "target": TARGET,
    "reason": "bounded reconciliation fixture",
    "retry_policy": {"max_attempts": 3, "backoff_s": 2},
    "created_at": NOW,
}


def intent(**update: Any) -> ScaleIntentRecord:
    return ScaleIntentRecord.model_validate({**INTENT, **update})


class Actuator:
    """Records every call and replays a queue of outcomes (default success)."""

    name = "fixture-actuator"

    def __init__(self, events: list[str], *outcomes: str) -> None:
        self.events, self.outcomes = events, list(outcomes)
        self.calls: list[ScaleActuationRequest] = []

    def apply(self, request: ScaleActuationRequest) -> ScaleActuationResult:
        self.events.append("actuator:call")
        self.calls.append(request)
        retry = (self.outcomes.pop(0) if self.outcomes else "ok") == "retry"
        return ScaleActuationResult(
            execution_key=request.execution_key,
            target=request.target,
            state=ScaleIntentState.FAILED if retry else ScaleIntentState.SUCCEEDED,
            scaled=not retry,
            retryable=retry,
            error_code="temporarily_unavailable" if retry else None,
            observed_replicas=None if retry else request.desired_replicas,
        )


class DenyPolicy:
    def decide(self, intent: ScaleIntentRecord) -> ScalePolicyDecision:
        return ScalePolicyDecision(
            decision_id="deny:1", allowed=False, reason="needs explicit approval"
        )


ALLOW = AllowScalePolicy()


@pytest.fixture
def ledger() -> MemoryScaleIntentLedger:
    return MemoryScaleIntentLedger()


def run(
    ledger: MemoryScaleIntentLedger,
    *outcomes: str,
    policy: ScalePolicy | None = ALLOW,
) -> tuple[ScaleIntentReconciler, Actuator]:
    actuator = Actuator(ledger.events, *outcomes)
    return ScaleIntentReconciler(
        ledger, actuator, policy=policy, clock=lambda: NOW
    ), actuator


def test_persists_before_actuation_and_replays_duplicates(ledger) -> None:
    reconciler, actuator = run(ledger)
    first = reconciler.reconcile(intent(), controller_id="writer-1")
    second = reconciler.reconcile(intent(), controller_id="writer-1")
    assert first.state == ScaleIntentState.VERIFIED.value
    assert first.observation is not None and first.observation.status == "converged"
    assert ledger.events.index("persist_intent") < ledger.events.index("actuator:call")
    assert (second.replayed, second.actuator_called) == (True, False)
    assert second.observation == first.observation
    assert len(actuator.calls) == 1


@pytest.mark.parametrize("result_recorded", [True, False])
def test_crash_recovery_never_double_actuates(ledger, result_recorded: bool) -> None:
    record = intent()
    persisted = ledger.persist_intent(record)
    lease = ledger.acquire_controller_lease(record, "writer-1", now=NOW)
    assert lease is not None
    started = ledger.mark_started(record, persisted.execution, lease, now=NOW)
    if result_recorded:
        done = ScaleActuationResult(
            execution_key=record.execution_key,
            target=record.target,
            state=ScaleIntentState.SUCCEEDED,
            scaled=True,
            observed_replicas=record.desired_replicas,
        )
        ledger.record_result(record, started, lease, done, now=NOW)
    reconciler, actuator = run(ledger)
    result = reconciler.reconcile(record, controller_id="writer-1")
    assert result.state == ScaleIntentState.VERIFIED.value
    assert len(actuator.calls) == (0 if result_recorded else 1)


def test_retry_reuses_key_under_a_newer_fenced_lease(ledger) -> None:
    reconciler, actuator = run(ledger, "retry")
    first = reconciler.reconcile(intent(), controller_id="writer-1")
    second = reconciler.reconcile(intent(), controller_id="writer-1")
    assert (first.state, first.retry_after_s) == (ScaleIntentState.FAILED.value, 2)
    assert second.state == ScaleIntentState.VERIFIED.value
    assert second.execution.result_attempt == 2
    keys = {call.execution_key for call in actuator.calls}
    leases = {call.lease.lease_instance_id for call in actuator.calls}
    assert (len(actuator.calls), len(keys), len(leases)) == (2, 1, 2)


@pytest.mark.parametrize(
    "update",
    [
        {
            "target": {
                **TARGET,
                "target_uid": "uid-2",
                "target_resource_version": "rv-2",
            }
        },
        {"intent_revision": 2, "expected_unit_revision": 8},
    ],
)
def test_same_key_with_changed_binding_conflicts(ledger, update) -> None:
    reconciler, actuator = run(ledger)
    reconciler.reconcile(intent(), controller_id="writer-1")
    with pytest.raises(ScaleIntentConflict):
        reconciler.reconcile(intent(**update), controller_id="writer-1")
    assert len(actuator.calls) == 1


def test_dry_run_is_simulated_and_never_actuates(ledger) -> None:
    reconciler, actuator = run(ledger)
    first, second = (
        reconciler.reconcile(intent(), controller_id="writer-1", dry_run=True)
        for _ in range(2)
    )
    assert first.observation is not None and first.observation.status == "simulated"
    assert (first.scaled, first.execution.dry_run, second.replayed) == (
        False,
        True,
        True,
    )
    assert actuator.calls == []


@pytest.mark.parametrize("policy", [None, DenyPolicy()])
def test_missing_or_denying_policy_fails_closed(ledger, policy) -> None:
    reconciler, actuator = run(ledger, policy=policy)
    result = reconciler.reconcile(intent(), controller_id="writer-1")
    assert result.state == ScaleIntentState.DENIED.value
    assert actuator.calls == []


def test_break_glass_is_audited_before_actuation(ledger) -> None:
    reconciler, actuator = run(ledger, policy=DenyPolicy())
    approval = BreakGlassAuthorization(
        authorization_id="break-glass-1",
        actor_id="operator-1",
        approval_ref="approval-1",
        reason="bounded emergency recovery",
        target=intent().target,
        authorized_until=NOW + timedelta(minutes=1),
    )
    result = reconciler.reconcile(
        intent(), controller_id="writer-1", break_glass=approval
    )
    assert result.break_glass_audit is not None
    assert result.state == ScaleIntentState.VERIFIED.value
    assert ledger.events.index("break_glass:audit") < ledger.events.index(
        "actuator:call"
    )


def test_only_the_declared_replica_writer_may_actuate(ledger) -> None:
    reconciler, actuator = run(ledger)
    with pytest.raises(ScaleLeaseUnavailable, match="declared replica writer"):
        reconciler.reconcile(intent(), controller_id="writer-2")
    assert actuator.calls == []
