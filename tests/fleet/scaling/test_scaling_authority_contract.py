"""Public-surface behaviour of the scaling authority contract."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.fleet.scaling.scaling_authority import (
    MaintenanceWindow,
    ObservedOutcome,
    ScaleAuthority,
    ScaleDecision,
    ScaleExecution,
    ScaleIntent,
    SignalSummaryRef,
    validate_scale_intent,
    validate_scale_lifecycle,
)

pytestmark = pytest.mark.spec("ARBORA-CAPACITY-R750.750")

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
UNIT, WRITER, DOMAIN = "unit:gateway", "controller:main", "host:small-gpu-a"
QUOTA = {"scope": "tenant:acme", "max_units": 12}
RECOVERY = {"max_staleness_s": 120}


def fence(epoch: int = 4) -> dict[str, Any]:
    return {
        "lease_id": "lease:scaling-authority",
        "lease_epoch": epoch,
        "fence_token": 9,
        "expires_at": NOW + timedelta(minutes=30),
    }


def unit(**update: Any) -> dict[str, Any]:
    return {
        "unit_id": UNIT,
        "revision": 7,
        "workload_class_id": "workload:gateway",
        "resource_pool_id": "pool:replica-slots",
        "cadence": "service_replica",
        "controller_mode": "native",
        "controller_id": WRITER,
        "replica_writer_id": WRITER,
        "min_replicas": 1,
        "max_replicas": 8,
        "reserved_headroom_replicas": 2,
        "tenant_quota": {**QUOTA, "max_units": 8},
        "failure_domain_id": DOMAIN,
        "offline_recovery": RECOVERY,
        "maintenance_window": {"window": "02:00-04:00"},
        "rollback_policy": {"on_failure": "automatic", "max_attempts": 2},
        **update,
    }


def writer(
    unit_id: str = UNIT, controller_id: str = WRITER, **kw: Any
) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "controller_id": controller_id,
        "mode": "native",
        "writes_replicas": True,
        "lease_fence": fence(),
        **kw,
    }


def authority(
    units: tuple[dict[str, Any], ...] | None = None,
    controllers: tuple[dict[str, Any], ...] | None = None,
    domain_status: str = "healthy",
) -> ScaleAuthority:
    return ScaleAuthority.model_validate(
        {
            "authority_revision": 11,
            "failure_domains": [
                {
                    "domain_id": DOMAIN,
                    "kind": "host",
                    "status": domain_status,
                    "last_observed_at": NOW,
                    "authority_ref": "authority:inventory",
                }
            ],
            "pools": [
                {
                    "pool_id": "pool:replica-slots",
                    "revision": 2,
                    "resource_kind": "replica_slots",
                    "capacity_units": 16,
                    "allocated_units": 2,
                    "reserved_headroom_units": 2,
                    "failure_domain_id": DOMAIN,
                    "quota": QUOTA,
                    "lease_fence": fence(),
                    "offline_recovery": RECOVERY,
                }
            ],
            "workloads": [
                {
                    "workload_id": "workload:gateway",
                    "revision": 5,
                    "tenant_ref": "tenant:acme",
                    "resource_pool_id": "pool:replica-slots",
                    "cadence": "service_replica",
                    "priority": 50,
                    "max_concurrency": 8,
                    "quota": QUOTA,
                }
            ],
            "units": units or (unit(),),
            "controllers": (writer(),) if controllers is None else controllers,
        }
    )


SUMMARY: dict[str, Any] = {
    "summary_id": "summary:gateway:queue-depth",
    "summary_digest": "a" * 64,
    "source": "telemetry",
    "unit_id": UNIT,
    "signal": "queue_depth",
    "aggregation": "p95",
    "window_start": NOW,
    "window_end": LATER,
    "sample_count": 300,
}
INTENT: dict[str, Any] = {
    "intent_id": "intent:gateway:12",
    "unit_id": UNIT,
    "revision": 12,
    "expected_unit_revision": 7,
    "cadence": "service_replica",
    "desired_replicas": 4,
    "signal_summary_ref": SUMMARY,
    "controller_mode": "native",
    "proposer_id": WRITER,
    "replica_writer_id": WRITER,
    "lease_fence": fence(),
    "idempotency_key": "idempotency:gateway:12",
    "reason": "p95 queue depth crossed the target",
    "created_at": NOW,
}
LINK = {"unit_id": UNIT, "controller_mode": "native", "replica_writer_id": WRITER}
EXECUTION: dict[str, Any] = {
    **LINK,
    "execution_id": "execution:gateway:12",
    "intent_id": INTENT["intent_id"],
    "decision_id": "decision:gateway:12",
    "intent_revision": 12,
    "execution_revision": 1,
    "requested_replicas": 4,
    "lease_fence": fence(),
    "idempotency_key": INTENT["idempotency_key"],
    "operation": "scale",
    "state": "succeeded",
    "started_at": NOW,
    "completed_at": LATER,
}


def lifecycle(
    epoch: int = 4,
) -> tuple[ScaleIntent, ScaleDecision, ScaleExecution, ObservedOutcome]:
    return (
        ScaleIntent.model_validate(INTENT),
        ScaleDecision.model_validate(
            {
                **LINK,
                "decision_id": "decision:gateway:12",
                "intent_id": INTENT["intent_id"],
                "intent_revision": 12,
                "verdict": "accepted",
                "decisioner_id": "policy:scaling",
                "policy_revision": 3,
                "lease_fence": fence(epoch),
                "reason": "within bounds and headroom",
                "decided_at": NOW,
            }
        ),
        ScaleExecution.model_validate({**EXECUTION, "lease_fence": fence(epoch)}),
        ObservedOutcome.model_validate(
            {
                "outcome_id": "outcome:gateway:12",
                "execution_id": EXECUTION["execution_id"],
                "unit_id": UNIT,
                "execution_revision": 1,
                "observed_revision": 1,
                "observed_replicas": 4,
                "status": "converged",
                "observed_by": "observer:gateway",
                "failure_domain_id": DOMAIN,
                "lease_fence": fence(epoch),
                "observed_at": LATER,
            }
        ),
    )


def test_native_authority_and_revisioned_lifecycle_are_accepted() -> None:
    intent, decision, execution, outcome = lifecycle()
    validate_scale_intent(intent, authority())
    validate_scale_lifecycle(intent, decision, execution, outcome)


def test_lease_fence_cannot_change_between_lifecycle_records() -> None:
    intent, decision, execution, _ = lifecycle()
    with pytest.raises(ValueError, match="lease/fence"):
        validate_scale_lifecycle(intent, decision, execution, lifecycle(epoch=5)[3])


def test_signal_reference_never_carries_samples() -> None:
    assert "samples" not in SignalSummaryRef.model_validate(SUMMARY).graph_projection()
    with pytest.raises(ValidationError, match="extra"):
        SignalSummaryRef.model_validate({**SUMMARY, "samples": [0.1]})


def test_delegated_controller_has_one_writer_and_an_observer() -> None:
    delegated = {"mode": "delegated", "delegated_controller": "hpa"}
    built = authority(
        units=(
            unit(
                controller_mode="delegated",
                delegated_controller="hpa",
                controller_id="controller:observer",
                replica_writer_id="controller:hpa",
            ),
        ),
        controllers=(
            writer(controller_id="controller:hpa", **delegated),
            writer(
                controller_id="controller:observer", writes_replicas=False, **delegated
            ),
        ),
    )
    assert built.units[0].controller_mode == "delegated"


WORKER = unit(
    unit_id="unit:worker",
    controller_id="controller:worker",
    replica_writer_id="controller:worker",
    depends_on=(UNIT,),
)


@pytest.mark.parametrize(
    ("units", "controllers", "match"),
    [
        (
            None,
            (writer(), writer(controller_id="controller:b")),
            "exactly one replica writer",
        ),
        (None, (), "exactly one replica writer"),
        ((unit(depends_on=(UNIT,)),), None, "self-dependency"),
        (
            (unit(depends_on=("unit:worker",)), WORKER),
            (writer(), writer("unit:worker", "controller:worker")),
            "dependency cycle",
        ),
        ((unit(reserved_headroom_replicas=7),), None, "headroom"),
    ],
)
def test_invalid_authority_inventory_is_refused(
    units: tuple[dict[str, Any], ...] | None,
    controllers: tuple[dict[str, Any], ...] | None,
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        authority(units=units, controllers=controllers)


@pytest.mark.parametrize(
    ("update", "domain_status", "match"),
    [
        ({"desired_replicas": 9}, "healthy", "min/max"),
        ({"expected_unit_revision": 6}, "healthy", "stale"),
        ({}, "offline", "failure domain is offline"),
    ],
)
def test_scale_intent_is_checked_against_authority(
    update: dict[str, Any], domain_status: str, match: str
) -> None:
    intent = ScaleIntent.model_validate({**INTENT, **update})
    with pytest.raises(ValueError, match=match):
        validate_scale_intent(intent, authority(domain_status=domain_status))


@pytest.mark.parametrize(
    ("model", "payload", "match"),
    [
        (ScaleIntent, {**INTENT, "cadence": "engine_local"}, ""),
        (ScaleExecution, {**EXECUTION, "state": "failed"}, "failure_code"),
        (ScaleExecution, {**EXECUTION, "state": "simulated"}, "simulated"),
        (MaintenanceWindow, {"window": "25:00-26:00"}, "UTC HH:MM-HH:MM"),
    ],
)
def test_invalid_records_fail_closed(
    model: type[ScaleIntent | ScaleExecution | MaintenanceWindow],
    payload: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match or None):
        model.model_validate(payload)
