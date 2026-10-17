# mypy: disable-error-code="arg-type,assignment,misc,call-arg"
"""Public-surface behaviour of the service-surface scale evaluator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from graph_os.fleet.scaling.service_scale_units import (
    CapacityDemand,
    CapacityObservation,
    ContinuityContract,
    ContinuityObservation,
    LoadSafetyObservation,
    PartitionLeaseContract,
    QuotaBinding,
    QuotaObservation,
    ScalePolicy,
    ScaleUnitContract,
    SignalObservation,
    content_digest,
    evaluate_scale,
)

pytestmark = pytest.mark.spec("GRAPHOS-CAPACITY-R750.753")

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
FRESH = {
    "observed_at": NOW - timedelta(minutes=1),
    "expires_at": NOW + timedelta(minutes=5),
}
STALE = {
    "observed_at": NOW - timedelta(minutes=10),
    "expires_at": NOW - timedelta(minutes=5),
}
MCP_AXES = ("cpu_milli", "memory_mib", "session_slots", "continuity_slots")
API_AXES = ("cpu_milli", "memory_mib", "session_slots")
SIGNALS = ("request_rate", "in_flight", "p95_latency_ms", "error_rate_ppm")


def _src(ref: str) -> dict:
    return {"source_ref": ref, "source_digest": content_digest({"fixture": ref})}


def _signal(value: int, **kw) -> SignalObservation:
    return SignalObservation(
        signal="request_rate", value=value, **{**FRESH, **kw}, **_src("metrics")
    )


def _capacity(axis: str, **kw) -> CapacityObservation:
    fields = {"allocatable": 10_000, "reserved": 1_000, "used": 0, **kw}
    return CapacityObservation(axis=axis, **FRESH, **fields, **_src(axis))


def _quota(limit: int = 100, **kw) -> QuotaObservation:
    return QuotaObservation(
        quota_ref="provider:llm",
        scope="provider_global",
        limit=limit,
        used=0,
        **{**FRESH, **kw},
        **_src("quota"),
    )


def _continuity(**kw) -> ContinuityObservation:
    fields = {"ready": True, "active_sessions": 0, "active_streams": 0, **kw}
    return ContinuityObservation(**FRESH, **fields, **_src("continuity"))


def _safety(**kw) -> LoadSafetyObservation:
    flagged = bool(kw)
    return LoadSafetyObservation(
        overloaded=kw.get("overloaded", False),
        noisy_neighbor=kw.get("noisy_neighbor", False),
        reason_ref="incident:capacity" if flagged else None,
        **FRESH,
        **_src("safety"),
    )


def _demands(axes) -> tuple[CapacityDemand, ...]:
    return tuple(CapacityDemand(axis=a, per_replica=10) for a in axes)


def _policy(**kw) -> ScalePolicy:
    base = {
        "min_replicas": 1,
        "max_replicas": 8,
        "target": 100,
        "target_signal": "request_rate",
        "scale_up_cooldown_s": 0,
        "scale_down_cooldown_s": 0,
    }
    return ScalePolicy(**{**base, **kw})


def _mcp(*, target_signal="request_rate", drain_seconds=30, quotas=None):
    return ScaleUnitContract(
        unit_ref="unit:mcp-gateway",
        surface="mcp_gateway",
        policy=_policy(
            scale_up_step=2, drain_seconds=drain_seconds, target_signal=target_signal
        ),
        allowed_signals=(*SIGNALS, "active_sessions", "continuity_load"),
        required_capacity_axes=MCP_AXES,
        replica_demands=_demands(MCP_AXES),
        quotas=(
            (
                QuotaBinding(
                    quota_ref="provider:llm",
                    axis="provider_global_quota",
                    scope="provider_global",
                    per_replica=10,
                ),
            )
            if quotas is None
            else quotas
        ),
        continuity=ContinuityContract(
            mode="externalized",
            external_store_ref="store:sessions",
            drain_required=True,
            block_scale_down_with_active=True,
            max_drain_seconds=300,
        ),
    )


def _api(**policy):
    return ScaleUnitContract(
        unit_ref="unit:api-gateway",
        surface="api_gateway",
        policy=_policy(max_replicas=4, drain_seconds=0, **policy),
        allowed_signals=SIGNALS,
        required_capacity_axes=API_AXES,
        replica_demands=_demands(API_AXES),
        continuity=ContinuityContract(
            mode="stateless", drain_required=False, block_scale_down_with_active=False
        ),
    )


def _decide(contract, *, replicas=1, signal=250, axes=MCP_AXES, **over):
    args = {
        "signal": _signal(signal) if signal is not None else None,
        "capacities": tuple(_capacity(a) for a in axes),
        "quotas": (_quota(),),
        "continuity": _continuity(),
        "safety": _safety(),
        **over,
    }
    return evaluate_scale(contract, current_replicas=replicas, now=NOW, **args)


def test_target_tracking_scale_up_is_deterministic() -> None:
    first, second = (_decide(_mcp()) for _ in range(2))
    assert (first.action, first.desired_replicas) == ("scale_up", 3)
    assert first.reasons == ("target_tracking",)
    assert first == second
    assert first.decision_digest == second.decision_digest


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"signal": None}, "signal_missing"),
        ({"quotas": (_quota(**STALE),)}, "provider_quota_stale"),
        ({"quotas": (_quota(limit=10),)}, "provider_quota_exhausted"),
        ({"safety": _safety(overloaded=True)}, "overload_shedding_active"),
        ({"safety": _safety(noisy_neighbor=True)}, "noisy_neighbor"),
    ],
)
def test_unsafe_or_missing_evidence_blocks_scale_up(over, reason) -> None:
    decision = _decide(_mcp(), **over)
    assert decision.action == "blocked"
    assert reason in decision.reasons


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"continuity": _continuity(active_sessions=1)}, "active_sessions_block_drain"),
        ({"continuity": None}, "continuity_missing"),
    ],
)
def test_scale_down_waits_for_continuity(over, reason) -> None:
    decision = _decide(_mcp(), replicas=3, signal=0, **over)
    assert decision.action == "blocked"
    assert reason in decision.reasons


def test_drain_longer_than_contract_bound_blocks() -> None:
    decision = _decide(_mcp(drain_seconds=301), replicas=3, signal=0)
    assert decision.action == "blocked"
    assert "drain_required" in decision.reasons


def test_saturated_engine_authority_refuses_to_scale() -> None:
    axes = (
        "cpu_milli",
        "memory_mib",
        "read_admission_slots",
        "engine_bytes",
        "fsync_iops",
    )
    contract = ScaleUnitContract(
        unit_ref="unit:query-reader",
        surface="query_reader",
        policy=_policy(),
        allowed_signals=SIGNALS,
        required_capacity_axes=axes,
        replica_demands=_demands(axes),
        engine_authority_axes=("read_admission_slots", "engine_bytes", "fsync_iops"),
        continuity=ContinuityContract(
            mode="stateless", drain_required=False, block_scale_down_with_active=False
        ),
    )
    full = {"allocatable": 100, "reserved": 100, "used": 100}
    capacities = (
        _capacity("cpu_milli"),
        _capacity("memory_mib"),
        _capacity("read_admission_slots", **full),
        _capacity("engine_bytes", **full),
        _capacity("fsync_iops", **full),
    )
    decision = _decide(contract, capacities=capacities, quotas=(), continuity=None)
    assert decision.action == "blocked"
    assert "engine_authority_saturated" in decision.reasons


@pytest.mark.parametrize(
    ("policy", "replicas", "signal", "action", "desired", "reason"),
    [
        ({"min_replicas": 1}, 3, 0, "scale_down", 2, None),
        (
            {"min_replicas": 0, "allow_scale_from_zero": False},
            0,
            100,
            "blocked",
            None,
            "scale_from_zero_disabled",
        ),
        ({"min_replicas": 0, "allow_scale_to_zero": True}, 1, 0, "scale_down", 0, None),
    ],
)
def test_scale_to_min_and_zero_bounds(
    policy, replicas, signal, action, desired, reason
) -> None:
    decision = _decide(
        _api(**policy),
        replicas=replicas,
        signal=signal,
        axes=API_AXES,
        quotas=(),
        continuity=None,
    )
    assert decision.action == action
    if desired is not None:
        assert decision.desired_replicas == desired
    if reason:
        assert reason in decision.reasons


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda: _mcp(target_signal="queue_depth"), "target_signal"),
        (lambda: _mcp(quotas=()), "provider-global quota"),
        (lambda: PartitionLeaseContract(partitioned=True), "partitions_per_replica"),
        (
            lambda: PartitionLeaseContract(
                partitioned=True, partitions_per_replica=1, lease_required=True
            ),
            "TTL and fencing",
        ),
        (
            lambda: QuotaBinding(
                quota_ref="provider:llm",
                axis="provider_global_quota",
                scope="provider_global",
                per_replica=10,
                multiplicative=True,
            ),
            "cannot be multiplied",
        ),
    ],
)
def test_invalid_contracts_are_refused(build, match) -> None:
    with pytest.raises(ValueError, match=match):
        build()
