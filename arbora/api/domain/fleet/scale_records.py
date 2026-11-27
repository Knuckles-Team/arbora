"""The scale-intent records: the typed wire shapes and errors of one reconcile.

Pure types and their validators, shared by the served operations and the reconciler;
the reconciler's ledger, policy and actuator logic stays in
``arbora.fleet.scaling.scaling_intent_reconciler``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

CONTRACT_VERSION: Final[Literal["1"]] = "1"
MAX_REASON_LENGTH = 512
MAX_DETAIL_LENGTH = 512
MAX_RETRY_ATTEMPTS = 8
MAX_RETRY_BACKOFF_S = 3_600.0
MAX_LEASE_TTL_S = 900.0

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ScaleModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        use_enum_values=True,
    )


def checked_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value.strip()) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value.strip()


def checked_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a 64-character lowercase digest")
    return value


def derived_id(prefix: str, value: str) -> str:
    """Build a bounded deterministic identifier from an external key."""

    candidate = f"{prefix}:{value}"
    if _IDENTIFIER_RE.fullmatch(candidate) is not None:
        return candidate
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


def aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def result_digest(result: ScaleActuationResult) -> str:
    """Canonical digest for the exact typed result being observed."""

    material = json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ScaleControllerMode(StrEnum):
    NATIVE = "native"
    DELEGATED = "delegated"


class ScaleIntentState(StrEnum):
    PERSISTED = "persisted"
    STARTED = "started"
    SIMULATED = "simulated"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OBSERVED = "observed"
    VERIFIED = "verified"
    DENIED = "denied"


class ScaleObservationStatus(StrEnum):
    CONVERGED = "converged"
    FAILED = "failed"
    SIMULATED = "simulated"


class ScaleFence(ScaleModel):
    """Authority fence carried by the intent, lease, actuator, and outcome."""

    lease_id: str
    lease_epoch: StrictInt = Field(ge=1)
    fence_token: StrictInt = Field(ge=1)
    expires_at: datetime

    _validate_lease = field_validator("lease_id")(
        lambda value: checked_identifier(value, "lease_id")
    )
    _validate_expiry = field_validator("expires_at")(
        lambda value: aware_utc(value, "expires_at")
    )


class ScaleTargetBinding(ScaleModel):
    """The complete target identity; names alone are never sufficient."""

    cluster_id: str
    runtime_id: str
    workload_id: str
    target_uid: str
    target_resource_version: str

    _validate_cluster = field_validator("cluster_id")(
        lambda value: checked_identifier(value, "cluster_id")
    )
    _validate_runtime = field_validator("runtime_id")(
        lambda value: checked_identifier(value, "runtime_id")
    )
    _validate_workload = field_validator("workload_id")(
        lambda value: checked_identifier(value, "workload_id")
    )
    _validate_uid = field_validator("target_uid")(
        lambda value: checked_identifier(value, "target_uid")
    )
    _validate_resource_version = field_validator("target_resource_version")(
        lambda value: checked_identifier(value, "target_resource_version")
    )


class ScaleRetryPolicy(ScaleModel):
    """Durable bounded retry policy; retries reuse one execution key."""

    max_attempts: StrictInt = Field(default=3, ge=1, le=MAX_RETRY_ATTEMPTS)
    backoff_s: float = Field(default=5.0, ge=0, le=MAX_RETRY_BACKOFF_S)

    @field_validator("backoff_s")
    @classmethod
    def finite_backoff(cls, value: float) -> float:
        return _finite(value, "retry backoff")


class ScaleIntentRecord(ScaleModel):
    """Immutable desired change with all identity required for replay safety."""

    schema_version: Literal["1"] = CONTRACT_VERSION
    intent_id: str
    intent_revision: StrictInt = Field(ge=1)
    expected_unit_revision: StrictInt = Field(ge=1)
    desired_replicas: StrictInt = Field(ge=0)
    min_replicas: StrictInt = Field(ge=0)
    max_replicas: StrictInt = Field(ge=0)
    controller_mode: ScaleControllerMode
    replica_writer_id: str
    execution_key: str
    fence: ScaleFence
    target: ScaleTargetBinding
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)
    retry_policy: ScaleRetryPolicy = Field(default_factory=ScaleRetryPolicy)
    created_at: datetime

    _validate_intent = field_validator("intent_id")(
        lambda value: checked_identifier(value, "intent_id")
    )
    _validate_writer = field_validator("replica_writer_id")(
        lambda value: checked_identifier(value, "replica_writer_id")
    )
    _validate_execution = field_validator("execution_key")(
        lambda value: checked_identifier(value, "execution_key")
    )
    _validate_created = field_validator("created_at")(
        lambda value: aware_utc(value, "created_at")
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> ScaleIntentRecord:
        if self.max_replicas < self.min_replicas:
            raise ValueError("scale intent max_replicas must cover min_replicas")
        if not self.min_replicas <= self.desired_replicas <= self.max_replicas:
            raise ValueError("scale intent desired_replicas violates bounds")
        return self

    def identity_digest(self) -> str:
        """Digest all request identity, including fence and target version."""

        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ControllerLease(ScaleModel):
    """Controller ownership lease; calls with another lease are fenced."""

    lease_instance_id: str
    controller_id: str
    fence: ScaleFence
    attempt: StrictInt = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime

    _validate_instance = field_validator("lease_instance_id")(
        lambda value: checked_identifier(value, "lease_instance_id")
    )
    _validate_controller = field_validator("controller_id")(
        lambda value: checked_identifier(value, "controller_id")
    )
    _validate_acquired = field_validator("acquired_at")(
        lambda value: aware_utc(value, "acquired_at")
    )
    _validate_expiry = field_validator("expires_at")(
        lambda value: aware_utc(value, "expires_at")
    )

    @model_validator(mode="after")
    def validate_times(self) -> ControllerLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("controller lease expires_at must be after acquired_at")
        return self


class ScaleActuationRequest(ScaleModel):
    """Typed, idempotent actuator input; no runtime-specific dicts."""

    schema_version: Literal["1"] = CONTRACT_VERSION
    intent_id: str
    intent_revision: StrictInt = Field(ge=1)
    expected_unit_revision: StrictInt = Field(ge=1)
    execution_key: str
    desired_replicas: StrictInt = Field(ge=0)
    controller_mode: ScaleControllerMode
    replica_writer_id: str
    target: ScaleTargetBinding
    lease: ControllerLease
    dry_run: StrictBool = False

    _validate_intent = field_validator("intent_id")(
        lambda value: checked_identifier(value, "intent_id")
    )
    _validate_execution = field_validator("execution_key")(
        lambda value: checked_identifier(value, "execution_key")
    )
    _validate_writer = field_validator("replica_writer_id")(
        lambda value: checked_identifier(value, "replica_writer_id")
    )


class ScaleActuationResult(ScaleModel):
    """Typed actuator result; ``simulated`` is never represented as scaled."""

    schema_version: Literal["1"] = CONTRACT_VERSION
    execution_key: str
    target: ScaleTargetBinding
    state: ScaleIntentState
    scaled: StrictBool
    observed_replicas: StrictInt | None = Field(default=None, ge=0)
    retryable: StrictBool = False
    error_code: str | None = None
    detail: str = Field(default="", max_length=MAX_DETAIL_LENGTH)
    actuator_operation_id: str | None = None

    _validate_execution = field_validator("execution_key")(
        lambda value: checked_identifier(value, "execution_key")
    )
    _validate_error = field_validator("error_code")(
        lambda value: None if value is None else checked_identifier(value, "error_code")
    )
    _validate_operation = field_validator("actuator_operation_id")(
        lambda value: (
            None
            if value is None
            else checked_identifier(value, "actuator_operation_id")
        )
    )

    @model_validator(mode="after")
    def validate_result(self) -> ScaleActuationResult:
        if self.state == ScaleIntentState.SIMULATED.value:
            if self.scaled or self.error_code is not None:
                raise ValueError("simulated result must not claim scaled or failed")
        elif self.state == ScaleIntentState.SUCCEEDED.value:
            if not self.scaled or self.observed_replicas is None:
                raise ValueError("succeeded result requires scaled observation")
        elif self.state == ScaleIntentState.FAILED.value:
            if self.scaled or self.error_code is None:
                raise ValueError("failed result requires an error and no scaling")
        else:
            raise ValueError("actuator result must be succeeded, failed, or simulated")
        return self


class ScaleExecutionRecord(ScaleModel):
    """Durable execution row, including the last typed result for crash repair."""

    schema_version: Literal["1"] = CONTRACT_VERSION
    execution_id: str
    intent_id: str
    intent_revision: StrictInt = Field(ge=1)
    execution_key: str
    target: ScaleTargetBinding
    state: ScaleIntentState
    attempt: StrictInt = Field(ge=0)
    dry_run: StrictBool = False
    actuator_called: StrictBool = False
    scaled: StrictBool = False
    # A retry keeps the prior typed result attached for auditability while
    # ``attempt`` advances under a newer fence.  This marker lets the ledger
    # accept only that newer retry result, never an unbound duplicate.
    result_attempt: StrictInt | None = Field(default=None, ge=1)
    lease: ControllerLease | None = None
    result: ScaleActuationResult | None = None
    observation_id: str | None = None
    identity_digest: str
    created_at: datetime
    updated_at: datetime

    _validate_execution_id = field_validator("execution_id")(
        lambda value: checked_identifier(value, "execution_id")
    )
    _validate_intent = field_validator("intent_id")(
        lambda value: checked_identifier(value, "intent_id")
    )
    _validate_execution = field_validator("execution_key")(
        lambda value: checked_identifier(value, "execution_key")
    )
    _validate_identity = field_validator("identity_digest")(
        lambda value: checked_digest(value, "identity_digest")
    )
    _validate_created = field_validator("created_at")(
        lambda value: aware_utc(value, "created_at")
    )
    _validate_updated = field_validator("updated_at")(
        lambda value: aware_utc(value, "updated_at")
    )


class ScaleObservation(ScaleModel):
    """Exactly-once durable convergence or failure observation."""

    schema_version: Literal["1"] = CONTRACT_VERSION
    observation_id: str
    execution_key: str
    intent_id: str
    intent_revision: StrictInt = Field(ge=1)
    target: ScaleTargetBinding
    status: ScaleObservationStatus
    scaled: StrictBool
    observed_replicas: StrictInt | None = Field(default=None, ge=0)
    outcome_digest: str
    observed_at: datetime
    verified: StrictBool = True
    detail: str = Field(default="", max_length=MAX_DETAIL_LENGTH)

    _validate_observation = field_validator("observation_id")(
        lambda value: checked_identifier(value, "observation_id")
    )
    _validate_execution = field_validator("execution_key")(
        lambda value: checked_identifier(value, "execution_key")
    )
    _validate_intent = field_validator("intent_id")(
        lambda value: checked_identifier(value, "intent_id")
    )
    _validate_outcome = field_validator("outcome_digest")(
        lambda value: checked_digest(value, "outcome_digest")
    )
    _validate_observed = field_validator("observed_at")(
        lambda value: aware_utc(value, "observed_at")
    )

    @model_validator(mode="after")
    def validate_status(self) -> ScaleObservation:
        if not self.verified:
            raise ValueError("durable observations must be verified")
        if self.status == ScaleObservationStatus.CONVERGED.value:
            if not self.scaled or self.observed_replicas is None:
                raise ValueError("converged observation requires scaled replicas")
        elif self.status in {
            ScaleObservationStatus.FAILED.value,
            ScaleObservationStatus.SIMULATED.value,
        }:
            if self.scaled:
                raise ValueError("failed or simulated observation cannot be scaled")
        return self


class ScalePolicyDecision(ScaleModel):
    """Policy result; break-glass is a separate authorization, never implicit."""

    decision_id: str
    allowed: StrictBool
    reason: str = Field(default="", max_length=MAX_REASON_LENGTH)

    _validate_decision = field_validator("decision_id")(
        lambda value: checked_identifier(value, "decision_id")
    )


class BreakGlassAuthorization(ScaleModel):
    """Explicit, scoped authorization for an otherwise denied intent."""

    authorization_id: str
    actor_id: str
    approval_ref: str
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)
    target: ScaleTargetBinding
    authorized_until: datetime
    authorized: StrictBool = True

    _validate_authorization = field_validator("authorization_id")(
        lambda value: checked_identifier(value, "authorization_id")
    )
    _validate_actor = field_validator("actor_id")(
        lambda value: checked_identifier(value, "actor_id")
    )
    _validate_approval = field_validator("approval_ref")(
        lambda value: checked_identifier(value, "approval_ref")
    )
    _validate_expiry = field_validator("authorized_until")(
        lambda value: aware_utc(value, "authorized_until")
    )


class BreakGlassAudit(ScaleModel):
    """Durable audit receipt emitted before a break-glass actuator call."""

    audit_id: str
    authorization_id: str
    intent_id: str
    actor_id: str
    approval_ref: str
    target: ScaleTargetBinding
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)
    authorized_until: datetime
    recorded_at: datetime
    audit_digest: str

    _validate_audit = field_validator("audit_id")(
        lambda value: checked_identifier(value, "audit_id")
    )
    _validate_authorization = field_validator("authorization_id")(
        lambda value: checked_identifier(value, "authorization_id")
    )
    _validate_intent = field_validator("intent_id")(
        lambda value: checked_identifier(value, "intent_id")
    )
    _validate_actor = field_validator("actor_id")(
        lambda value: checked_identifier(value, "actor_id")
    )
    _validate_approval = field_validator("approval_ref")(
        lambda value: checked_identifier(value, "approval_ref")
    )
    _validate_recorded = field_validator("recorded_at")(
        lambda value: aware_utc(value, "recorded_at")
    )
    _validate_authorized_until = field_validator("authorized_until")(
        lambda value: aware_utc(value, "authorized_until")
    )
    _validate_digest = field_validator("audit_digest")(
        lambda value: checked_digest(value, "audit_digest")
    )


class ScaleReconcileResult(ScaleModel):
    """Process-local presentation of a durable reconciliation result."""

    state: ScaleIntentState
    replayed: StrictBool = False
    actuator_called: StrictBool = False
    scaled: StrictBool = False
    execution: ScaleExecutionRecord
    observation: ScaleObservation | None = None
    break_glass_audit: BreakGlassAudit | None = None
    retry_after_s: float | None = Field(default=None, ge=0)
    detail: str = Field(default="", max_length=MAX_DETAIL_LENGTH)


class ScaleIntentConflict(RuntimeError):
    """An execution key or intent revision was replayed with another payload."""


class ScaleLeaseUnavailable(RuntimeError):
    """Another controller owns the live lease or the fence is stale."""


class ScaleLeaseLost(RuntimeError):
    """A result or observation arrived under a stale controller lease."""


class ScaleObservationConflict(RuntimeError):
    """The same execution key attempted a different terminal observation."""
