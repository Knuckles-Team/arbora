"""Public-surface behaviour of the service-surface scale evaluator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arbora.fleet.scaling.service_scale_units import (
    CapacityAxis,
    CapacityDemand,
    CapacityObservation,
    ContinuityContract,
    ContinuityObservation,
    LoadSafetyObservation,
    PartitionLeaseContract,
    QuotaBinding,
    QuotaObservation,
    ScaleDecision,
    ScalePolicy,
    ScaleUnitContract,
    SignalKind,
    SignalObservation,
    SurfaceKind,
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
MCP_AXES: tuple[CapacityAxis, ...] = (
    "cpu_milli",
    "memory_mib",
    "session_slots",
    "continuity_slots",
)
API_AXES: tuple[CapacityAxis, ...] = ("cpu_milli", "memory_mib", "session_slots")
ENGINE_AXES: tuple[CapacityAxis, ...] = (
    "read_admission_slots",
    "engine_bytes",
    "fsync_iops",
)
SIGNALS: tuple[SignalKind, ...] = (
    "request_rate",
    "in_flight",
    "p95_latency_ms",
    "error_rate_ppm",
)
STATELESS = ContinuityContract(
    mode="stateless", drain_required=False, block_scale_down_with_active=False
)
LLM_QUOTA = {
    "quota_ref": "provider:llm",
    "axis": "provider_global_quota",
    "scope": "provider_global",
    "per_replica": 10,
}


def observed(model: Any, ref: str, **fields: Any) -> Any:
    source = {"source_ref": ref, "source_digest": content_digest({"fixture": ref})}
    return model.model_validate({**FRESH, **source, **fields})


def capacity(axis: str, **kw: Any) -> CapacityObservation:
    return observed(
        CapacityObservation,
        axis,
        axis=axis,
        **{"allocatable": 10_000, "reserved": 1_000, "used": 0, **kw},
    )


def quota(**kw: Any) -> QuotaObservation:
    return observed(
        QuotaObservation,
        "quota",
        **{
            "quota_ref": "provider:llm",
            "scope": "provider_global",
            "limit": 100,
            "used": 0,
            **kw,
        },
    )


def continuity(**kw: Any) -> ContinuityObservation:
    return observed(
        ContinuityObservation,
        "continuity",
        **{"ready": True, "active_sessions": 0, "active_streams": 0, **kw},
    )


def safety(**kw: bool) -> LoadSafetyObservation:
    flags = {"overloaded": False, "noisy_neighbor": False, **kw}
    return observed(
        LoadSafetyObservation,
        "safety",
        reason_ref="incident:capacity" if kw else None,
        **flags,
    )


def contract(
    surface: SurfaceKind = "mcp_gateway",
    axes: tuple[CapacityAxis, ...] = MCP_AXES,
    signals: tuple[SignalKind, ...] = (*SIGNALS, "active_sessions", "continuity_load"),
    **policy: Any,
) -> ScaleUnitContract:
    mcp = surface == "mcp_gateway"
    base = {
        "min_replicas": 1,
        "max_replicas": 8,
        "target": 100,
        "target_signal": "request_rate",
        "scale_up_cooldown_s": 0,
        "scale_down_cooldown_s": 0,
        "drain_seconds": 30 if mcp else 0,
    }
    quotas = policy.pop(
        "quotas", (QuotaBinding.model_validate(LLM_QUOTA),) if mcp else ()
    )
    return ScaleUnitContract(
        unit_ref=f"unit:{surface}",
        surface=surface,
        policy=ScalePolicy.model_validate(
            {**base, "scale_up_step": 2 if mcp else 1, **policy}
        ),
        allowed_signals=signals,
        required_capacity_axes=axes,
        replica_demands=tuple(CapacityDemand(axis=a, per_replica=10) for a in axes),
        engine_authority_axes=tuple(a for a in axes if a in ENGINE_AXES),
        quotas=quotas,
        continuity=ContinuityContract(
            mode="externalized",
            external_store_ref="store:sessions",
            drain_required=True,
            block_scale_down_with_active=True,
            max_drain_seconds=300,
        )
        if mcp
        else STATELESS,
    )


def decide(
    unit: ScaleUnitContract, replicas: int = 1, signal: int | None = 250, **over: Any
) -> ScaleDecision:
    args = {
        "signal": None
        if signal is None
        else observed(
            SignalObservation, "metrics", signal="request_rate", value=signal
        ),
        "capacities": tuple(capacity(a) for a in unit.required_capacity_axes),
        "quotas": (quota(),) if unit.quotas else (),
        "continuity": continuity() if unit.surface == "mcp_gateway" else None,
        "safety": safety(),
        **over,
    }
    return evaluate_scale(unit, current_replicas=replicas, now=NOW, **args)


def test_target_tracking_scale_up_is_deterministic() -> None:
    first, second = decide(contract()), decide(contract())
    assert (first.action, first.desired_replicas, first.reasons) == (
        "scale_up",
        3,
        ("target_tracking",),
    )
    assert first == second and first.decision_digest == second.decision_digest


FULL = {"allocatable": 100, "reserved": 100, "used": 100}
QUERY_READER = contract(
    "query_reader", ("cpu_milli", "memory_mib", *ENGINE_AXES), SIGNALS
)


@pytest.mark.parametrize(
    ("unit", "replicas", "signal", "over", "reason"),
    [
        (contract(), 1, None, {}, "signal_missing"),
        (contract(), 1, 250, {"quotas": (quota(**STALE),)}, "provider_quota_stale"),
        (
            contract(),
            1,
            250,
            {"quotas": (quota(limit=10),)},
            "provider_quota_exhausted",
        ),
        (
            contract(),
            1,
            250,
            {"safety": safety(overloaded=True)},
            "overload_shedding_active",
        ),
        (contract(), 1, 250, {"safety": safety(noisy_neighbor=True)}, "noisy_neighbor"),
        (
            contract(),
            3,
            0,
            {"continuity": continuity(active_sessions=1)},
            "active_sessions_block_drain",
        ),
        (contract(), 3, 0, {"continuity": None}, "continuity_missing"),
        (contract(drain_seconds=301), 3, 0, {}, "drain_required"),
        (
            QUERY_READER,
            1,
            250,
            {
                "capacities": (
                    capacity("cpu_milli"),
                    capacity("memory_mib"),
                    *(capacity(a, **FULL) for a in ENGINE_AXES),
                )
            },
            "engine_authority_saturated",
        ),
        (
            contract(
                "api_gateway",
                API_AXES,
                SIGNALS,
                max_replicas=4,
                min_replicas=0,
                allow_scale_from_zero=False,
            ),
            0,
            100,
            {},
            "scale_from_zero_disabled",
        ),
    ],
)
def test_unsafe_or_missing_evidence_blocks_scaling(
    unit: ScaleUnitContract,
    replicas: int,
    signal: int | None,
    over: dict[str, Any],
    reason: str,
) -> None:
    decision = decide(unit, replicas, signal, **over)
    assert decision.action == "blocked"
    assert reason in decision.reasons


@pytest.mark.parametrize(
    ("policy", "replicas", "desired"),
    [
        ({"min_replicas": 1}, 3, 2),
        ({"min_replicas": 0, "allow_scale_to_zero": True}, 1, 0),
    ],
)
def test_scale_down_respects_min_and_zero_bounds(
    policy: dict[str, Any], replicas: int, desired: int
) -> None:
    decision = decide(
        contract("api_gateway", API_AXES, SIGNALS, max_replicas=4, **policy),
        replicas,
        0,
    )
    assert (decision.action, decision.desired_replicas) == ("scale_down", desired)


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda: contract(target_signal="queue_depth"), "target_signal"),
        (lambda: contract(quotas=()), "provider-global quota"),
        (lambda: PartitionLeaseContract(partitioned=True), "partitions_per_replica"),
        (
            lambda: PartitionLeaseContract(
                partitioned=True, partitions_per_replica=1, lease_required=True
            ),
            "TTL and fencing",
        ),
        (
            lambda: QuotaBinding.model_validate({**LLM_QUOTA, "multiplicative": True}),
            "cannot be multiplied",
        ),
    ],
)
def test_invalid_contracts_are_refused(build: Callable[[], object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build()
