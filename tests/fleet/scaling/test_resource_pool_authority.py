"""Public-surface behaviour of hardware-aware resource-pool placement."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from arbora.fleet.scaling.resource_pool_authority import (
    CapabilityAttestation,
    CapabilityState,
    CostCapability,
    CpuCapability,
    EnergyCapability,
    GpuCapability,
    NetworkCapability,
    NvmeRequirement,
    PlacementRequirement,
    ResourceAccounting,
    ResourceAmount,
    ResourceCapabilities,
    ResourcePoolSnapshot,
    StorageCapability,
    capability_payload_digest,
    place,
)

pytestmark = pytest.mark.spec("GRAPHOS-CAPACITY-R750.754")

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
known, amount = ResourceAccounting.known, ResourceAmount.known
DEFAULT_MEMORY = known(22 * 1024)
SMALL = PlacementRequirement(cpu_milli=1_000, memory_mib=1_024)


def snapshot(
    pool_ref: str = "pool:small-gpu-a",
    memory: ResourceAccounting = DEFAULT_MEMORY,
    attestation: str = "verified",
    digest: str | None = None,
) -> ResourcePoolSnapshot:
    none: CapabilityState = "absent"
    capabilities = ResourceCapabilities(
        cpu=CpuCapability(
            architecture_state="known",
            architecture="x86_64",
            isa_state="known",
            isa=("avx2",),
            accounting=known(8_000),
        ),
        memory=memory,
        gpu=GpuCapability(
            state="known",
            runtime="cuda",
            device_count=amount(1),
            memory=known(16 * 1024),
        ),
        storage=StorageCapability(
            disk=known(512 * 1024),
            disk_read_iops=amount(20_000),
            disk_write_iops=amount(20_000),
            nvme_state=none,
            nvme_device_count=ResourceAmount.missing(none),
            nvme_read_iops=ResourceAmount.missing(none),
            nvme_write_iops=ResourceAmount.missing(none),
        ),
        network=NetworkCapability(
            state="known",
            ingress_mbps=amount(10_000),
            egress_mbps=amount(10_000),
            latency_us=amount(500),
        ),
        energy=EnergyCapability(
            state="known", max_power_watts=amount(250), observed_power_watts=amount(90)
        ),
        cost=CostCapability(state="known", currency="USD", micros_per_hour=amount(100)),
    )
    window = {
        "observed_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    return ResourcePoolSnapshot(
        pool_ref=pool_ref,
        revision=1,
        capabilities=capabilities,
        attestation=CapabilityAttestation.model_validate(
            {
                "status": attestation,
                "algorithm": "ed25519",
                "signer_ref": "signer:inventory",
                "signature_ref": "signature:small-gpu-a:1",
                "subject_digest": digest or capability_payload_digest(capabilities),
                **window,
            }
        ),
        observed_at=window["observed_at"],
        expires_at=window["expires_at"],
    )


@pytest.mark.parametrize(
    ("requirement", "snap", "now", "reasons"),
    [
        (
            PlacementRequirement(
                architecture="x86_64",
                required_isa=("avx2",),
                cpu_milli=4_000,
                memory_mib=32 * 1024,
                nvme=NvmeRequirement(required=True, device_count=1, read_iops=50_000),
            ),
            snapshot(),
            NOW,
            {"insufficient_memory", "nvme_unavailable"},
        ),
        (
            SMALL,
            snapshot(memory=ResourceAccounting.missing("unknown")),
            NOW,
            {"unknown_memory"},
        ),
        (SMALL, snapshot(attestation="unverified"), NOW, {"unverified_attestation"}),
        (SMALL, snapshot(), NOW + timedelta(hours=2), {"stale_snapshot"}),
    ],
)
def test_unqualified_unknown_or_stale_pools_are_denied(
    requirement: PlacementRequirement,
    snap: ResourcePoolSnapshot,
    now: datetime,
    reasons: set[str],
) -> None:
    decision = place(requirement, (snap,), now=now)
    assert (decision.status, decision.selected_pool_ref) == ("denied", None)
    assert reasons <= set(decision.denial_reasons)


def test_placement_is_order_independent_and_replayable() -> None:
    pools = (snapshot(), snapshot("pool:edge-a", memory=known(24 * 1024)))
    left, right = (place(SMALL, order, now=NOW) for order in (pools, pools[::-1]))
    assert left.status == "placed"
    assert left.selected_pool_ref == right.selected_pool_ref
    assert left.decision_digest == right.decision_digest


def test_forged_subject_and_overdrawn_accounting_are_refused() -> None:
    with pytest.raises(ValidationError, match="subject digest"):
        snapshot(digest="sha256:" + "f" * 64)
    with pytest.raises(ValidationError, match="used <= reserved <= allocatable"):
        known(allocatable=8_000, reserved=7_000, used=7_001)
