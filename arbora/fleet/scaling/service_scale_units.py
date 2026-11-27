"""Reusable service-surface scale contracts and one fail-closed evaluator.

CONCEPT:MERISTEM-OS.scaling.service-scale-units — one typed scale policy for ingestion,
dispatch, gateways, query readers, and connector/media/RLM worker pools.

The module owns no controller or actuator.  It defines the facts a controller
must present and evaluates one bounded target-tracking decision.  Surface-specific
loops are deliberately not encoded: a surface selects a contract profile, then
the same evaluator applies signal freshness, hard capacity, provider-global
quota, continuity, cooldown, drain, and noisy-neighbor rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

from arbora.fleet.scaling._model import ProtocolModel

SCHEMA_VERSION: Literal["1"] = "1"
MAX_REF_LENGTH = 256
MAX_SIGNALS = 16
MAX_CAPACITY_AXES = 32
MAX_DEMANDS = 32
MAX_QUOTAS = 16
MAX_EVIDENCE = 64
MAX_REASONS = 16
MAX_REPLICAS = 10_000
MAX_STALENESS_SECONDS = 86_400
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_RE = re.compile(
    r"(?:authorization|bearer|cookie|password|passwd|private[_-]?key|secret|"
    r"token(?:=|:)|api[_-]?key|raw[_-]?(?:body|span|log))",
    re.IGNORECASE,
)

SurfaceKind = Literal[
    "ingest_worker",
    "dispatch_worker",
    "arbora_gateway",
    "mcp_gateway",
    "api_gateway",
    "query_reader",
    "connector_pool",
    "media_pool",
    "rlm_pool",
]
SignalKind = Literal[
    "queue_depth",
    "consumer_lag",
    "backlog_age_ms",
    "request_rate",
    "in_flight",
    "p95_latency_ms",
    "error_rate_ppm",
    "active_sessions",
    "continuity_load",
    "provider_in_flight",
    "gpu_utilization_ppm",
]
CapacityAxis = Literal[
    "cpu_milli",
    "memory_mib",
    "gpu_count",
    "gpu_memory_mib",
    "partition_slots",
    "lease_slots",
    "engine_bytes",
    "engine_write_bytes_per_s",
    "fsync_iops",
    "read_admission_slots",
    "session_slots",
    "continuity_slots",
    "upstream_rate",
    "provider_global_quota",
]
QuotaScope = Literal["unit", "tenant", "engine_global", "provider_global"]
ContinuityMode = Literal["stateless", "session_affine", "externalized"]
ActionKind = Literal["scale_up", "scale_down", "hold", "blocked"]
EvidenceKind = Literal["signal", "capacity", "quota", "continuity", "safety"]

DecisionReason = Literal[
    "at_target",
    "cooldown",
    "signal_missing",
    "signal_stale",
    "signal_mismatch",
    "capacity_missing",
    "capacity_stale",
    "capacity_exhausted",
    "engine_authority_saturated",
    "provider_quota_missing",
    "provider_quota_stale",
    "provider_quota_exhausted",
    "provider_quota_not_multiplicative",
    "quota_missing",
    "quota_stale",
    "quota_scope_mismatch",
    "continuity_missing",
    "continuity_stale",
    "continuity_not_ready",
    "active_sessions_block_drain",
    "scale_from_zero_disabled",
    "scale_to_zero_disabled",
    "drain_required",
    "overload_shedding_active",
    "noisy_neighbor",
    "partition_lease_missing",
    "invalid_bounds",
    "target_tracking",
]


class ScaleContractError(ValueError):
    """Raised when a scale contract or observation is unsafe or ambiguous."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _ref(value: str, *, name: str) -> str:
    if len(value) > MAX_REF_LENGTH or not _REF_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque reference")
    if _SENSITIVE_RE.search(value):
        raise ValueError(f"{name} must not contain credentials or raw payloads")
    return value


def _digest(value: str, *, name: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a sha256:<64 lowercase hex> digest")
    return value


def utc_instant(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    normalized = value.astimezone(UTC)
    if normalized.year < 2000 or normalized.year > 2300:
        raise ValueError(f"{name} is outside the supported time range")
    return normalized


def _bounded_signal(signal: SignalKind, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("scale signal values must be finite non-negative integers")
    if signal in {"error_rate_ppm", "gpu_utilization_ppm"} and value > 1_000_000:
        raise ValueError(f"{signal} must be bounded to one million ppm")
    return value


class ScalePolicy(ProtocolModel):
    """Bounded target-tracking policy shared by every surface."""

    min_replicas: Annotated[StrictInt, Field(ge=0, le=MAX_REPLICAS)] = 1
    max_replicas: Annotated[StrictInt, Field(ge=1, le=MAX_REPLICAS)] = 1
    target: Annotated[StrictInt, Field(gt=0, le=10**12)]
    target_signal: SignalKind
    scale_up_step: Annotated[StrictInt, Field(gt=0, le=MAX_REPLICAS)] = 1
    scale_down_step: Annotated[StrictInt, Field(gt=0, le=MAX_REPLICAS)] = 1
    scale_up_cooldown_s: Annotated[StrictInt, Field(ge=0, le=MAX_STALENESS_SECONDS)] = (
        30
    )
    scale_down_cooldown_s: Annotated[
        StrictInt, Field(ge=0, le=MAX_STALENESS_SECONDS)
    ] = 300
    drain_seconds: Annotated[StrictInt, Field(ge=0, le=MAX_STALENESS_SECONDS)] = 60
    allow_scale_to_zero: StrictBool = False
    allow_scale_from_zero: StrictBool = True

    @model_validator(mode="after")
    def validate_bounds(self) -> ScalePolicy:
        if self.min_replicas > self.max_replicas:
            raise ScaleContractError("min_replicas cannot exceed max_replicas")
        if self.allow_scale_to_zero and self.min_replicas != 0:
            raise ScaleContractError("scale-to-zero requires min_replicas=0")
        if self.scale_up_step > self.max_replicas:
            raise ScaleContractError("scale_up_step cannot exceed max_replicas")
        if self.scale_down_step > self.max_replicas:
            raise ScaleContractError("scale_down_step cannot exceed max_replicas")
        return self


class CapacityDemand(ProtocolModel):
    """Hard capacity consumed by one replica of a scale unit."""

    axis: CapacityAxis
    per_replica: Annotated[StrictInt, Field(ge=0, le=10**15)]


class QuotaBinding(ProtocolModel):
    """Quota dependency; provider-global budgets never multiply with replicas."""

    quota_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    axis: CapacityAxis
    scope: QuotaScope
    per_replica: Annotated[StrictInt, Field(ge=0, le=10**15)]
    multiplicative: StrictBool = False

    @field_validator("quota_ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        return _ref(value, name="quota_ref")

    @model_validator(mode="after")
    def validate_global_quota(self) -> QuotaBinding:
        if self.scope == "provider_global" and self.multiplicative:
            raise ScaleContractError(
                "provider-global quota cannot be multiplied per replica"
            )
        if self.scope == "provider_global" and self.per_replica == 0:
            raise ScaleContractError(
                "provider-global quota requires a declared per-replica demand"
            )
        return self


class PartitionLeaseContract(ProtocolModel):
    """Partition/lease prerequisites for queue and stateful worker surfaces."""

    partitioned: StrictBool = False
    partitions_per_replica: Annotated[StrictInt, Field(ge=0, le=10**6)] = 0
    lease_required: StrictBool = False
    lease_ttl_s: Annotated[StrictInt, Field(ge=0, le=MAX_STALENESS_SECONDS)] = 0
    fencing_required: StrictBool = False

    @model_validator(mode="after")
    def validate_contract(self) -> PartitionLeaseContract:
        if self.partitioned and self.partitions_per_replica < 1:
            raise ScaleContractError(
                "partitioned surfaces require partitions_per_replica"
            )
        if self.lease_required and (self.lease_ttl_s < 1 or not self.fencing_required):
            raise ScaleContractError("leased surfaces require a TTL and fencing")
        if self.fencing_required and not self.lease_required:
            raise ScaleContractError("fencing cannot be declared without leases")
        return self


class ContinuityContract(ProtocolModel):
    """Session/stream continuity prerequisites for scaling and draining."""

    mode: ContinuityMode
    external_store_ref: Annotated[str, Field(max_length=MAX_REF_LENGTH)] | None = None
    drain_required: StrictBool = True
    block_scale_down_with_active: StrictBool = True
    max_drain_seconds: Annotated[StrictInt, Field(ge=0, le=MAX_STALENESS_SECONDS)] = 300

    @field_validator("external_store_ref")
    @classmethod
    def validate_store_ref(cls, value: str | None) -> str | None:
        return _ref(value, name="external_store_ref") if value is not None else None

    @model_validator(mode="after")
    def validate_mode(self) -> ContinuityContract:
        if self.mode == "externalized" and self.external_store_ref is None:
            raise ScaleContractError(
                "externalized continuity requires a store reference"
            )
        if self.mode != "externalized" and self.external_store_ref is not None:
            raise ScaleContractError(
                "only externalized continuity may declare a store reference"
            )
        if self.mode == "stateless" and self.block_scale_down_with_active:
            raise ScaleContractError(
                "stateless continuity cannot block scale-down on active sessions"
            )
        return self


class SignalObservation(ProtocolModel):
    """Fresh bounded signal sample used by target tracking."""

    signal: SignalKind
    value: StrictInt
    observed_at: datetime
    expires_at: datetime
    source_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: int, info: ValidationInfo) -> int:
        signal = info.data.get("signal")
        return _bounded_signal(signal, value) if signal is not None else value

    @field_validator("observed_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime, info: object) -> datetime:
        return utc_instant(value, name=str(getattr(info, "field_name", "signal_time")))

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        return _ref(value, name="signal_source_ref")

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _digest(value, name="signal_source_digest")

    @model_validator(mode="after")
    def validate_window(self) -> SignalObservation:
        if self.expires_at <= self.observed_at:
            raise ValueError("signal expires_at must follow observed_at")
        return self


class CapacityObservation(ProtocolModel):
    """Fresh allocatable/reserved/used hard-capacity observation."""

    axis: CapacityAxis
    allocatable: Annotated[StrictInt, Field(ge=0, le=10**15)]
    reserved: Annotated[StrictInt, Field(ge=0, le=10**15)]
    used: Annotated[StrictInt, Field(ge=0, le=10**15)]
    observed_at: datetime
    expires_at: datetime
    source_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @field_validator("observed_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime, info: object) -> datetime:
        return utc_instant(
            value, name=str(getattr(info, "field_name", "capacity_time"))
        )

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        return _ref(value, name="capacity_source_ref")

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _digest(value, name="capacity_source_digest")

    @model_validator(mode="after")
    def validate_capacity(self) -> CapacityObservation:
        if not self.used <= self.reserved <= self.allocatable:
            raise ScaleContractError(
                "capacity must satisfy used <= reserved <= allocatable"
            )
        if self.expires_at <= self.observed_at:
            raise ValueError("capacity expires_at must follow observed_at")
        return self

    @property
    def available(self) -> int:
        return self.allocatable - self.reserved


class QuotaObservation(ProtocolModel):
    """Fresh global or local quota usage; missing values never mean unlimited."""

    quota_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    scope: QuotaScope
    limit: Annotated[StrictInt, Field(ge=0, le=10**15)]
    used: Annotated[StrictInt, Field(ge=0, le=10**15)]
    observed_at: datetime
    expires_at: datetime
    source_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @field_validator("quota_ref", "source_ref")
    @classmethod
    def validate_refs(cls, value: str, info: object) -> str:
        return _ref(value, name=str(getattr(info, "field_name", "quota_ref")))

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _digest(value, name="quota_source_digest")

    @field_validator("observed_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime, info: object) -> datetime:
        return utc_instant(value, name=str(getattr(info, "field_name", "quota_time")))

    @model_validator(mode="after")
    def validate_quota(self) -> QuotaObservation:
        if self.used > self.limit:
            raise ScaleContractError("quota usage cannot exceed its limit")
        if self.expires_at <= self.observed_at:
            raise ValueError("quota expires_at must follow observed_at")
        return self


class ContinuityObservation(ProtocolModel):
    """Fresh continuity state used to gate drain and scale-to-zero."""

    ready: StrictBool
    active_sessions: Annotated[StrictInt, Field(ge=0, le=10**12)]
    active_streams: Annotated[StrictInt, Field(ge=0, le=10**12)]
    observed_at: datetime
    expires_at: datetime
    source_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        return _ref(value, name="continuity_source_ref")

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _digest(value, name="continuity_source_digest")

    @field_validator("observed_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime, info: object) -> datetime:
        return utc_instant(
            value, name=str(getattr(info, "field_name", "continuity_time"))
        )

    @model_validator(mode="after")
    def validate_window(self) -> ContinuityObservation:
        if self.expires_at <= self.observed_at:
            raise ValueError("continuity expires_at must follow observed_at")
        return self


class LoadSafetyObservation(ProtocolModel):
    """Overload/noisy-neighbor evidence that keeps scaling policy honest."""

    overloaded: StrictBool
    noisy_neighbor: StrictBool
    reason_ref: Annotated[str, Field(max_length=MAX_REF_LENGTH)] | None = None
    observed_at: datetime
    expires_at: datetime
    source_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    source_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @field_validator("reason_ref", "source_ref")
    @classmethod
    def validate_refs(cls, value: str | None, info: object) -> str | None:
        return (
            _ref(value, name=str(getattr(info, "field_name", "safety_ref")))
            if value is not None
            else None
        )

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _digest(value, name="safety_source_digest")

    @field_validator("observed_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime, info: object) -> datetime:
        return utc_instant(value, name=str(getattr(info, "field_name", "safety_time")))

    @model_validator(mode="after")
    def validate_window(self) -> LoadSafetyObservation:
        if self.expires_at <= self.observed_at:
            raise ValueError("safety expires_at must follow observed_at")
        if (self.overloaded or self.noisy_neighbor) and self.reason_ref is None:
            raise ScaleContractError(
                "overload and noisy-neighbor evidence require a reason reference"
            )
        return self


class SurfaceProfile(ProtocolModel):
    """Static surface vocabulary used to validate contracts uniformly."""

    allowed_signals: Annotated[tuple[SignalKind, ...], Field(max_length=MAX_SIGNALS)]
    required_capacity_axes: Annotated[
        tuple[CapacityAxis, ...], Field(max_length=MAX_CAPACITY_AXES)
    ]
    engine_authority_axes: Annotated[
        tuple[CapacityAxis, ...], Field(max_length=MAX_CAPACITY_AXES)
    ] = ()
    continuity_modes: Annotated[tuple[ContinuityMode, ...], Field(max_length=3)]
    partition_lease_required: StrictBool = False
    provider_quota_required: StrictBool = False
    provider_quota_axes: Annotated[tuple[CapacityAxis, ...], Field(max_length=4)] = ()


_SURFACE_PROFILES: Mapping[SurfaceKind, SurfaceProfile] = MappingProxyType(
    {
        "ingest_worker": SurfaceProfile(
            allowed_signals=(
                "queue_depth",
                "consumer_lag",
                "backlog_age_ms",
                "p95_latency_ms",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "partition_slots",
                "lease_slots",
                "engine_write_bytes_per_s",
                "fsync_iops",
            ),
            engine_authority_axes=("engine_write_bytes_per_s", "fsync_iops"),
            continuity_modes=("stateless", "externalized"),
            partition_lease_required=True,
        ),
        "dispatch_worker": SurfaceProfile(
            allowed_signals=(
                "queue_depth",
                "consumer_lag",
                "active_sessions",
                "p95_latency_ms",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "partition_slots",
                "lease_slots",
            ),
            continuity_modes=("stateless", "externalized"),
            partition_lease_required=True,
        ),
        "arbora_gateway": SurfaceProfile(
            allowed_signals=(
                "request_rate",
                "in_flight",
                "p95_latency_ms",
                "error_rate_ppm",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "session_slots",
                "read_admission_slots",
                "engine_bytes",
                "fsync_iops",
            ),
            engine_authority_axes=(
                "read_admission_slots",
                "engine_bytes",
                "fsync_iops",
            ),
            continuity_modes=("session_affine", "externalized"),
        ),
        "mcp_gateway": SurfaceProfile(
            allowed_signals=(
                "request_rate",
                "in_flight",
                "p95_latency_ms",
                "active_sessions",
                "continuity_load",
                "provider_in_flight",
                "error_rate_ppm",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "session_slots",
                "continuity_slots",
            ),
            continuity_modes=("session_affine", "externalized"),
            provider_quota_required=True,
            provider_quota_axes=("provider_global_quota",),
        ),
        "api_gateway": SurfaceProfile(
            allowed_signals=(
                "request_rate",
                "in_flight",
                "p95_latency_ms",
                "error_rate_ppm",
            ),
            required_capacity_axes=("cpu_milli", "memory_mib", "session_slots"),
            continuity_modes=("stateless", "externalized"),
        ),
        "query_reader": SurfaceProfile(
            allowed_signals=(
                "request_rate",
                "in_flight",
                "p95_latency_ms",
                "error_rate_ppm",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "read_admission_slots",
                "engine_bytes",
                "fsync_iops",
            ),
            engine_authority_axes=(
                "read_admission_slots",
                "engine_bytes",
                "fsync_iops",
            ),
            continuity_modes=("stateless", "externalized"),
        ),
        "connector_pool": SurfaceProfile(
            allowed_signals=(
                "queue_depth",
                "consumer_lag",
                "request_rate",
                "provider_in_flight",
                "p95_latency_ms",
                "error_rate_ppm",
            ),
            required_capacity_axes=("cpu_milli", "memory_mib", "upstream_rate"),
            continuity_modes=("stateless", "externalized"),
            provider_quota_required=True,
            provider_quota_axes=("upstream_rate", "provider_global_quota"),
        ),
        "media_pool": SurfaceProfile(
            allowed_signals=(
                "queue_depth",
                "request_rate",
                "gpu_utilization_ppm",
                "p95_latency_ms",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "gpu_count",
                "gpu_memory_mib",
            ),
            continuity_modes=("stateless", "externalized"),
        ),
        "rlm_pool": SurfaceProfile(
            allowed_signals=(
                "queue_depth",
                "request_rate",
                "gpu_utilization_ppm",
                "active_sessions",
            ),
            required_capacity_axes=(
                "cpu_milli",
                "memory_mib",
                "gpu_count",
                "gpu_memory_mib",
            ),
            continuity_modes=("stateless", "externalized"),
        ),
    }
)


class ScaleUnitContract(ProtocolModel):
    """Reusable typed surface contract; no surface-specific evaluator exists."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    unit_ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    surface: SurfaceKind
    version: Annotated[StrictInt, Field(gt=0, le=2**31 - 1)] = 1
    policy: ScalePolicy
    allowed_signals: Annotated[tuple[SignalKind, ...], Field(max_length=MAX_SIGNALS)]
    required_capacity_axes: Annotated[
        tuple[CapacityAxis, ...], Field(max_length=MAX_CAPACITY_AXES)
    ]
    replica_demands: Annotated[
        tuple[CapacityDemand, ...], Field(max_length=MAX_DEMANDS)
    ]
    quotas: Annotated[tuple[QuotaBinding, ...], Field(max_length=MAX_QUOTAS)] = ()
    partition_lease: PartitionLeaseContract = Field(
        default_factory=PartitionLeaseContract
    )
    continuity: ContinuityContract
    safety_required: StrictBool = True
    engine_authority_axes: Annotated[
        tuple[CapacityAxis, ...], Field(max_length=MAX_CAPACITY_AXES)
    ] = ()
    contract_id: Annotated[str, Field(min_length=1, max_length=100)] = ""
    contract_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] = ""

    @field_validator("unit_ref")
    @classmethod
    def validate_unit_ref(cls, value: str) -> str:
        return _ref(value, name="unit_ref")

    @model_validator(mode="after")
    def validate_surface_and_identity(self) -> ScaleUnitContract:
        profile = _SURFACE_PROFILES[self.surface]
        self._validate_signal_vocabulary(profile)
        required_axes = self._validate_capacity_axes(profile)
        self._validate_continuity_and_lease(profile)
        self._validate_replica_demands(required_axes)
        self._validate_quota_bindings(profile)
        self._stamp_contract_identity()
        return self

    def _validate_signal_vocabulary(self, profile: SurfaceProfile) -> set[SignalKind]:
        """Uniqueness + vocabulary checks on ``allowed_signals``/``target_signal``.

        Extracted from :meth:`validate_surface_and_identity`.
        """
        allowed = set(self.allowed_signals)
        if len(allowed) != len(self.allowed_signals):
            raise ScaleContractError("allowed signal names must be unique")
        if not allowed or not allowed.issubset(profile.allowed_signals):
            raise ScaleContractError(
                f"{self.surface} contract declares a signal outside its allowed vocabulary"
            )
        if self.policy.target_signal not in allowed:
            raise ScaleContractError("target_signal must be in allowed_signals")
        return allowed

    def _validate_capacity_axes(self, profile: SurfaceProfile) -> set[CapacityAxis]:
        """Uniqueness + coverage checks on capacity/engine-authority axes.

        Extracted from :meth:`validate_surface_and_identity`. Returns the
        validated ``required_axes`` set for reuse by
        :meth:`_validate_replica_demands`.
        """
        required_axes = set(self.required_capacity_axes)
        if len(required_axes) != len(self.required_capacity_axes):
            raise ScaleContractError("required capacity axes must be unique")
        if not set(profile.required_capacity_axes).issubset(required_axes):
            raise ScaleContractError(
                f"{self.surface} contract omitted a required hard capacity axis"
            )
        if not set(profile.engine_authority_axes).issubset(required_axes):
            raise ScaleContractError(
                f"{self.surface} engine authority axes must be hard capacity axes"
            )
        declared_engine_axes = set(self.engine_authority_axes)
        if len(declared_engine_axes) != len(self.engine_authority_axes):
            raise ScaleContractError("engine authority axes must be unique")
        if declared_engine_axes != set(profile.engine_authority_axes):
            raise ScaleContractError(
                "engine_authority_axes must match the surface profile"
            )
        return required_axes

    def _validate_continuity_and_lease(self, profile: SurfaceProfile) -> None:
        """Continuity-mode + partition/lease checks.

        Extracted from :meth:`validate_surface_and_identity`.
        """
        if self.continuity.mode not in profile.continuity_modes:
            raise ScaleContractError(
                f"{self.surface} does not support continuity mode {self.continuity.mode}"
            )
        if profile.partition_lease_required and (
            not self.partition_lease.partitioned
            or not self.partition_lease.lease_required
            or not self.partition_lease.fencing_required
        ):
            raise ScaleContractError(
                f"{self.surface} requires partition and fenced lease contracts"
            )

    def _validate_replica_demands(self, required_axes: set[CapacityAxis]) -> None:
        """Uniqueness + coverage checks on ``replica_demands``.

        Extracted from :meth:`validate_surface_and_identity`.
        """
        demand_axes = {demand.axis for demand in self.replica_demands}
        if len(demand_axes) != len(self.replica_demands):
            raise ScaleContractError("replica demand axes must be unique")
        if not required_axes.issubset(demand_axes):
            raise ScaleContractError(
                "every required capacity axis needs a per-replica demand"
            )

    def _validate_quota_bindings(self, profile: SurfaceProfile) -> None:
        """Uniqueness + provider-quota checks on ``quotas``.

        Extracted from :meth:`validate_surface_and_identity`.
        """
        quota_refs = [quota.quota_ref for quota in self.quotas]
        if len(quota_refs) != len(set(quota_refs)):
            raise ScaleContractError("quota bindings must be unique")
        provider_bindings = tuple(
            quota for quota in self.quotas if quota.scope == "provider_global"
        )
        if profile.provider_quota_required:
            if not provider_bindings:
                raise ScaleContractError(
                    f"{self.surface} requires one provider-global quota binding"
                )
            if not any(
                quota.axis in profile.provider_quota_axes for quota in provider_bindings
            ):
                raise ScaleContractError(
                    f"{self.surface} provider quota binding has an unsupported axis"
                )

    def _stamp_contract_identity(self) -> None:
        """Compute + verify + stamp ``contract_id``/``contract_digest``.

        Extracted from :meth:`validate_surface_and_identity`.
        """
        identity = {
            "schema_version": self.schema_version,
            "unit_ref": self.unit_ref,
            "surface": self.surface,
            "version": self.version,
            "policy": self.policy.model_dump(mode="json"),
            "allowed_signals": sorted(self.allowed_signals),
            "required_capacity_axes": sorted(self.required_capacity_axes),
            "replica_demands": [
                item.model_dump(mode="json") for item in self.replica_demands
            ],
            "quotas": [item.model_dump(mode="json") for item in self.quotas],
            "partition_lease": self.partition_lease.model_dump(mode="json"),
            "continuity": self.continuity.model_dump(mode="json"),
            "safety_required": self.safety_required,
            "engine_authority_axes": sorted(self.engine_authority_axes),
        }
        expected_id = (
            "scale-unit:" + hashlib.sha256(_canonical(identity)).hexdigest()[:32]
        )
        expected_digest = content_digest(
            {"kind": "scale_unit_contract", "identity": identity}
        )
        if self.contract_id and self.contract_id != expected_id:
            raise ScaleContractError("contract_id does not match immutable identity")
        if self.contract_digest and self.contract_digest != expected_digest:
            raise ScaleContractError(
                "contract_digest does not match immutable identity"
            )
        object.__setattr__(self, "contract_id", expected_id)
        object.__setattr__(self, "contract_digest", expected_digest)


class DecisionEvidence(ProtocolModel):
    """Opaque bounded evidence attached to a scale decision."""

    kind: EvidenceKind
    ref: Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    status: Literal["accepted", "missing", "stale", "blocked"]
    code: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        return _ref(value, name="evidence_ref")

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, name="evidence_digest")


class ScaleDecision(ProtocolModel):
    """Deterministic scale result; actuation is deliberately outside this module."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    decision_id: Annotated[str, Field(min_length=1, max_length=100)] = ""
    decision_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] = ""
    contract_id: Annotated[str, Field(min_length=1, max_length=100)]
    evaluated_at: datetime
    current_replicas: Annotated[StrictInt, Field(ge=0, le=MAX_REPLICAS)]
    desired_replicas: Annotated[StrictInt, Field(ge=0, le=MAX_REPLICAS)]
    action: ActionKind
    reasons: Annotated[tuple[DecisionReason, ...], Field(max_length=MAX_REASONS)]
    drain_required: StrictBool = False
    evidence: Annotated[
        tuple[DecisionEvidence, ...], Field(max_length=MAX_EVIDENCE)
    ] = ()

    @field_validator("contract_id")
    @classmethod
    def validate_contract_ref(cls, value: str) -> str:
        return _ref(value, name="contract_id")

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return utc_instant(value, name="evaluated_at")

    @model_validator(mode="after")
    def validate_decision(self) -> ScaleDecision:
        self._validate_action_transition()
        self._stamp_decision_identity()
        return self

    def _validate_action_transition(self) -> None:
        """Action-vs-replica-count + reasons-required checks.

        Extracted from :meth:`validate_decision`.
        """
        self._validate_replica_delta()
        self._validate_reasons_required()

    def _validate_replica_delta(self) -> None:
        """scale_up/scale_down/hold vs current<->desired replica-count checks.

        Extracted from :meth:`_validate_action_transition`.
        """
        if self.action == "scale_up" and self.desired_replicas <= self.current_replicas:
            raise ScaleContractError("scale_up must increase replicas")
        if (
            self.action == "scale_down"
            and self.desired_replicas >= self.current_replicas
        ):
            raise ScaleContractError("scale_down must decrease replicas")
        if self.action == "hold" and self.desired_replicas != self.current_replicas:
            raise ScaleContractError("hold must preserve replica count")

    def _validate_reasons_required(self) -> None:
        """``blocked``/scale_* actions require at least one reason.

        Extracted from :meth:`_validate_action_transition`.
        """
        if self.action == "blocked" and not self.reasons:
            raise ScaleContractError("blocked decisions require reasons")
        if self.action in {"scale_up", "scale_down"} and not self.reasons:
            raise ScaleContractError("scale decisions require a reason")

    def _stamp_decision_identity(self) -> None:
        """Compute + verify + stamp ``decision_id``/``decision_digest``.

        Extracted from :meth:`validate_decision`.
        """
        identity = {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "current_replicas": self.current_replicas,
            "desired_replicas": self.desired_replicas,
            "action": self.action,
            "reasons": sorted(self.reasons),
            "drain_required": self.drain_required,
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
        }
        expected_id = (
            "scale-decision:" + hashlib.sha256(_canonical(identity)).hexdigest()[:32]
        )
        expected_digest = content_digest(
            {"kind": "scale_decision", "identity": identity}
        )
        if self.decision_id and self.decision_id != expected_id:
            raise ScaleContractError("decision_id does not match immutable identity")
        if self.decision_digest and self.decision_digest != expected_digest:
            raise ScaleContractError(
                "decision_digest does not match immutable identity"
            )
        object.__setattr__(self, "decision_id", expected_id)
        object.__setattr__(self, "decision_digest", expected_digest)


def surface_profile(surface: SurfaceKind) -> SurfaceProfile:
    """Return the immutable vocabulary and hard dependencies for a surface."""

    return _SURFACE_PROFILES[surface]


__all__ = [
    "ActionKind",
    "CapacityAxis",
    "CapacityDemand",
    "CapacityObservation",
    "ContinuityContract",
    "ContinuityMode",
    "ContinuityObservation",
    "DecisionEvidence",
    "DecisionReason",
    "EvidenceKind",
    "LoadSafetyObservation",
    "PartitionLeaseContract",
    "QuotaBinding",
    "QuotaObservation",
    "QuotaScope",
    "ScaleContractError",
    "ScaleDecision",
    "ScalePolicy",
    "ScaleUnitContract",
    "SignalKind",
    "SignalObservation",
    "SurfaceKind",
    "SurfaceProfile",
    "content_digest",
    "surface_profile",
]
