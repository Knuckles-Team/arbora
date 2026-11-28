"""Frozen, provenance-first contracts for controlled data migrations.

These models describe migration evidence and authority transitions only.  They
never carry source payloads, credentials, or approval authority implicitly.
Every snapshot, plan, observation, and stage record is identified by an opaque
reference, a source revision, and a digest so restart/replay can be idempotent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import MigrationCasConflictError, MigrationTransitionRefused

__all__ = [
    "MAX_COHORTS",
    "MAX_EVIDENCE_ITEMS",
    "MAX_SOURCE_FILES",
    "ApprovalState",
    "BackfillBatch",
    "BackfillObservation",
    "CanonicalRecord",
    "MAX_VERSION",
    "CohortReadCutover",
    "Count",
    "CutoverEvent",
    "CutoverState",
    "Digest",
    "GateCheck",
    "InventoryState",
    "InventoryItem",
    "LegacyRetirement",
    "MigrationEvent",
    "MigrationInventory",
    "MigrationPlan",
    "MigrationRollback",
    "MigrationStage",
    "MigrationState",
    "OpaqueRef",
    "PrerequisiteGate",
    "ProjectionCheckpoint",
    "RetirementEvent",
    "RetirementState",
    "ShadowDelta",
    "ShadowReconciliation",
    "SourceDisposition",
    "SourceFileSnapshot",
    "SourceSnapshot",
    "Timestamp",
    "Version",
    "WriteCutoverFence",
    "canonical_digest",
]


MAX_SOURCE_FILES = 4_096
MAX_EVIDENCE_ITEMS = 10_000
MAX_COHORTS = 256
MAX_VERSION = 2_147_483_647

_REF_RE = r"^[A-Za-z][A-Za-z0-9:_./-]{0,255}$"
_DIGEST_RE = r"^sha256:[0-9a-f]{64}$"

type OpaqueRef = Annotated[str, Field(pattern=_REF_RE, min_length=1)]
type Digest = Annotated[str, Field(pattern=_DIGEST_RE)]
type Version = Annotated[int, Field(ge=1, le=MAX_VERSION)]
type Count = Annotated[int, Field(ge=0, le=2_147_483_647)]
type Timestamp = Annotated[int, Field(ge=0)]


class MigrationStage(StrEnum):
    """The stage a migration plan sits in; derived from its state."""

    INVENTORY_FREEZE = "stage_0_inventory_freeze"
    READ_ONLY_BACKFILL = "stage_1_read_only_backfill"
    SHADOW_RECONCILIATION = "stage_2_shadow_reconciliation"
    COHORT_READ_CUTOVER = "stage_3_cohort_read_cutover"
    WRITE_CUTOVER = "stage_4_write_cutover"
    PROJECTION_CHECKPOINT = "stage_5_projection_checkpoint"
    LEGACY_RETIREMENT = "legacy_retirement"
    ROLLBACK = "rollback"


class MigrationState(StrEnum):
    """The lifecycle state of a migration plan (ARBORA-HOST-R041.1)."""

    PLANNED = "planned"
    INVENTORY_FROZEN = "inventory_frozen"
    BACKFILL_OBSERVING = "backfill_observing"
    SHADOW_RECONCILED = "shadow_reconciled"
    READ_CUTOVER = "read_cutover"
    WRITE_FENCED = "write_fenced"
    CHECKPOINTED = "checkpointed"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


class MigrationEvent(StrEnum):
    """What moves a migration plan from one state to the next."""

    FREEZE_INVENTORY = "freeze_inventory"
    RECORD_BACKFILL = "record_backfill"
    RECONCILE_CLEAN = "reconcile_clean"
    CUTOVER_READS = "cutover_reads"
    FENCE_WRITES = "fence_writes"
    CHECKPOINT = "checkpoint"
    RETIRE = "retire"
    ROLLBACK = "rollback"


_PS, _PE, _PG = MigrationState, MigrationEvent, MigrationStage


class CutoverState(StrEnum):
    """Lifecycle of a cohort read cutover and of a write cutover fence."""

    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"


class CutoverEvent(StrEnum):
    ROLLBACK = "rollback"


class RetirementState(StrEnum):
    """Lifecycle of a legacy retirement proof."""

    READY = "ready"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


class RetirementEvent(StrEnum):
    RETIRE = "retire"
    ROLLBACK = "rollback"


type SourceDisposition = Literal["accepted", "rejected", "malformed"]
type ApprovalState = Literal["unreviewed", "approved", "rejected"]


class InventoryState(StrEnum):
    """Whether one inventory item was found as expected, or how it was not."""

    PRESENT = "present"
    MISSING = "missing"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    MALFORMED = "malformed"


_FORBIDDEN_INLINE_KEYS = frozenset(
    {
        "body",
        "bytes",
        "content",
        "credentials",
        "data",
        "password",
        "payload",
        "private_key",
        "result",
        "secret",
        "text",
        "token",
        "values",
    }
)


def _canonical(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def canonical_digest(value: object) -> str:
    """Return the stable digest used for replay and successor identity."""

    payload = json.dumps(
        _canonical(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=False,
        strict=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_inline_payloads(cls, value: object) -> object:
        if isinstance(value, Mapping):
            keys = {
                str(key).casefold().replace("-", "_")
                for key in value
                if str(key).casefold().replace("-", "_") in _FORBIDDEN_INLINE_KEYS
            }
            if keys:
                raise ValueError("migration_inline_payload_forbidden")
        return value


class _VersionedModel(_FrozenModel):
    version: Version
    digest: Digest

    @property
    def identity_digest(self) -> str:
        return canonical_digest(self)

    def _successor(self, **changes: object) -> Self:
        """The next version of this record with ``changes`` applied and a fresh digest."""
        if self.version >= MAX_VERSION:
            raise MigrationCasConflictError("migration_version_exhausted")
        data = {**self.model_dump(mode="python"), **changes}
        data["version"] = self.version + 1
        data["digest"] = canonical_digest(
            {key: value for key, value in data.items() if key != "digest"}
        )
        return type(self)(**data)


class SourceFileSnapshot(_VersionedModel):
    """Repository-relative file metadata; file bytes never cross this boundary."""

    source_file_ref: OpaqueRef
    relative_path: OpaqueRef
    source_ref: OpaqueRef
    revision: OpaqueRef
    content_digest: Digest
    metadata_digest: Digest
    size_bytes: Count


class SourceSnapshot(_VersionedModel):
    """Canonical source revision used by every stage and checkpoint."""

    snapshot_ref: OpaqueRef
    source_ref: OpaqueRef
    revision: OpaqueRef
    files: tuple[SourceFileSnapshot, ...] = Field(
        min_length=1,
        max_length=MAX_SOURCE_FILES,
    )
    snapshot_digest: Digest

    @model_validator(mode="after")
    def _files_are_unique_and_bound(self) -> SourceSnapshot:
        refs = [item.source_file_ref for item in self.files]
        if len(refs) != len(set(refs)):
            raise ValueError("source_file_duplicate")
        if any(
            item.source_ref != self.source_ref or item.revision != self.revision
            for item in self.files
        ):
            raise ValueError("source_file_revision_drift")
        expected = canonical_digest(
            {
                "source_ref": self.source_ref,
                "revision": self.revision,
                "files": self.files,
            }
        )
        if self.snapshot_digest != expected:
            raise ValueError("source_snapshot_digest_mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        snapshot_ref: str,
        source_ref: str,
        revision: str,
        files: tuple[SourceFileSnapshot, ...],
        version: int = 1,
    ) -> SourceSnapshot:
        snapshot_digest = canonical_digest(
            {
                "source_ref": source_ref,
                "revision": revision,
                "files": files,
            }
        )
        return cls(
            snapshot_ref=snapshot_ref,
            source_ref=source_ref,
            revision=revision,
            files=files,
            snapshot_digest=snapshot_digest,
            version=version,
            digest=canonical_digest(
                {
                    "snapshot_ref": snapshot_ref,
                    "snapshot_digest": snapshot_digest,
                    "version": version,
                }
            ),
        )


class MigrationPlan(_VersionedModel):
    """The only mutable migration authority; each transition is a new version.

    ``state`` is the explicit machine of ARBORA-HOST-R041.1; ``stage`` follows
    from it.  ``transition`` is the only function that moves either field.
    """

    EDGES: ClassVar[Mapping[tuple[MigrationState, MigrationEvent], MigrationState]] = (
        MappingProxyType(
            {
                (_PS.PLANNED, _PE.FREEZE_INVENTORY): _PS.INVENTORY_FROZEN,
                (_PS.INVENTORY_FROZEN, _PE.RECORD_BACKFILL): _PS.BACKFILL_OBSERVING,
                (_PS.BACKFILL_OBSERVING, _PE.RECORD_BACKFILL): _PS.BACKFILL_OBSERVING,
                (_PS.BACKFILL_OBSERVING, _PE.RECONCILE_CLEAN): _PS.SHADOW_RECONCILED,
                (_PS.SHADOW_RECONCILED, _PE.CUTOVER_READS): _PS.READ_CUTOVER,
                (_PS.READ_CUTOVER, _PE.CUTOVER_READS): _PS.READ_CUTOVER,
                (_PS.READ_CUTOVER, _PE.FENCE_WRITES): _PS.WRITE_FENCED,
                (_PS.WRITE_FENCED, _PE.CHECKPOINT): _PS.CHECKPOINTED,
                (_PS.CHECKPOINTED, _PE.RETIRE): _PS.RETIRED,
                (_PS.PLANNED, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.INVENTORY_FROZEN, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.BACKFILL_OBSERVING, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.SHADOW_RECONCILED, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.READ_CUTOVER, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.WRITE_FENCED, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.CHECKPOINTED, _PE.ROLLBACK): _PS.ROLLED_BACK,
                (_PS.ROLLED_BACK, _PE.ROLLBACK): _PS.ROLLED_BACK,
            }
        )
    )
    INITIAL: ClassVar[MigrationState] = MigrationState.PLANNED
    TERMINALS: ClassVar[tuple[MigrationState, ...]] = (MigrationState.RETIRED,)
    STAGE_OF: ClassVar[Mapping[MigrationState, MigrationStage]] = MappingProxyType(
        {
            _PS.PLANNED: _PG.INVENTORY_FREEZE,
            _PS.INVENTORY_FROZEN: _PG.INVENTORY_FREEZE,
            _PS.BACKFILL_OBSERVING: _PG.READ_ONLY_BACKFILL,
            _PS.SHADOW_RECONCILED: _PG.SHADOW_RECONCILIATION,
            _PS.READ_CUTOVER: _PG.COHORT_READ_CUTOVER,
            _PS.WRITE_FENCED: _PG.WRITE_CUTOVER,
            _PS.CHECKPOINTED: _PG.PROJECTION_CHECKPOINT,
            _PS.RETIRED: _PG.LEGACY_RETIREMENT,
            _PS.ROLLED_BACK: _PG.ROLLBACK,
        }
    )
    REFUSAL_CODES: ClassVar[Mapping[MigrationEvent, str]] = MappingProxyType(
        {
            _PE.FREEZE_INVENTORY: "inventory_freeze_stage_invalid",
            _PE.RECORD_BACKFILL: "backfill_stage_invalid",
            _PE.RECONCILE_CLEAN: "shadow_stage_invalid",
            _PE.CUTOVER_READS: "read_cutover_stage_invalid",
            _PE.FENCE_WRITES: "write_cutover_stage_invalid",
            _PE.CHECKPOINT: "projection_checkpoint_stage_invalid",
            _PE.RETIRE: "legacy_retirement_stage_invalid",
            _PE.ROLLBACK: "retired_migration_not_rollbackable",
        }
    )

    migration_ref: OpaqueRef
    plan_ref: OpaqueRef
    source_snapshot_ref: OpaqueRef
    source_snapshot_digest: Digest
    target_authority_ref: OpaqueRef
    state: MigrationState = MigrationState.PLANNED
    stage: MigrationStage = MigrationStage.INVENTORY_FREEZE
    inventory_ref: OpaqueRef | None = None
    reconciliation_ref: OpaqueRef | None = None
    cohort_refs: tuple[OpaqueRef, ...] = Field(default=(), max_length=MAX_COHORTS)
    write_fence_ref: OpaqueRef | None = None
    checkpoint_ref: OpaqueRef | None = None
    retirement_ref: OpaqueRef | None = None

    @model_validator(mode="after")
    def _stage_state_is_consistent(self) -> MigrationPlan:
        if self.stage != self.STAGE_OF[self.state]:
            raise ValueError("migration_stage_state_mismatch")
        if len(self.cohort_refs) != len(set(self.cohort_refs)):
            raise ValueError("migration_cohort_duplicate")
        return self

    def transition(
        self,
        event: MigrationEvent,
        *,
        expected_version: int | None,
        **updates: object,
    ) -> MigrationPlan:
        """The next version of the plan after ``event``; the only writer of the state.

        Raises ``MigrationTransitionRefused`` for an event the table does not
        list from the current state, and ``MigrationCasConflictError`` when
        ``expected_version`` is not the current version.
        """
        target = self.EDGES.get((self.state, event))
        if target is None:
            raise MigrationTransitionRefused(
                "migration_plan",
                self.state,
                event,
                self.REFUSAL_CODES[event],
            )
        if expected_version is None or expected_version != self.version:
            raise MigrationCasConflictError()
        return self._successor(state=target, stage=self.STAGE_OF[target], **updates)

    @classmethod
    def initial(
        cls,
        *,
        migration_ref: str,
        plan_ref: str,
        source_snapshot_ref: str,
        source_snapshot_digest: str,
        target_authority_ref: str,
    ) -> MigrationPlan:
        return cls(
            migration_ref=migration_ref,
            plan_ref=plan_ref,
            source_snapshot_ref=source_snapshot_ref,
            source_snapshot_digest=source_snapshot_digest,
            target_authority_ref=target_authority_ref,
            version=1,
            digest=canonical_digest(
                {
                    "migration_ref": migration_ref,
                    "plan_ref": plan_ref,
                    "source_snapshot_digest": source_snapshot_digest,
                    "target_authority_ref": target_authority_ref,
                    "version": 1,
                }
            ),
        )


class InventoryItem(_VersionedModel):
    """Stable source-to-target inventory observation, including rejects."""

    canonical_ref: OpaqueRef
    source_ref: OpaqueRef
    source_file_ref: OpaqueRef | None = None
    source_record_ref: OpaqueRef | None = None
    source_revision: OpaqueRef
    source_digest: Digest
    metadata_digest: Digest
    state: Annotated[InventoryState, Field(strict=False)]


class MigrationInventory(_VersionedModel):
    """Frozen stage-0 inventory; a later stage cannot rewrite its source view."""

    inventory_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_ref: OpaqueRef
    source_snapshot_digest: Digest
    items: tuple[InventoryItem, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_ITEMS,
    )
    frozen: Literal[True] = True
    inventory_digest: Digest

    @model_validator(mode="after")
    def _inventory_is_deterministic(self) -> MigrationInventory:
        refs = [item.canonical_ref for item in self.items]
        if len(refs) != len(set(refs)):
            raise ValueError("inventory_identity_duplicate")
        expected = canonical_digest(self.items)
        if self.inventory_digest != expected:
            raise ValueError("inventory_digest_mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        inventory_ref: str,
        migration_ref: str,
        source_snapshot_ref: str,
        source_snapshot_digest: str,
        items: tuple[InventoryItem, ...],
        version: int = 1,
    ) -> MigrationInventory:
        inventory_digest = canonical_digest(items)
        return cls(
            inventory_ref=inventory_ref,
            migration_ref=migration_ref,
            source_snapshot_ref=source_snapshot_ref,
            source_snapshot_digest=source_snapshot_digest,
            items=items,
            inventory_digest=inventory_digest,
            version=version,
            digest=canonical_digest(
                {
                    "inventory_ref": inventory_ref,
                    "inventory_digest": inventory_digest,
                    "version": version,
                }
            ),
        )


class BackfillObservation(_VersionedModel):
    """Read-only observation that cannot imply target approval."""

    observation_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_digest: Digest
    canonical_ref: OpaqueRef
    source_ref: OpaqueRef
    source_file_ref: OpaqueRef | None = None
    source_record_ref: OpaqueRef | None = None
    source_revision: OpaqueRef
    source_digest: Digest
    metadata_digest: Digest
    disposition: SourceDisposition
    approval_state: ApprovalState = "unreviewed"
    approval_ref: OpaqueRef | None = None
    approver_ref: OpaqueRef | None = None
    observed_at: Timestamp

    @model_validator(mode="after")
    def _approval_is_explicit(self) -> BackfillObservation:
        if self.approval_state == "approved":
            if self.disposition != "accepted":
                raise ValueError("nonaccepted_backfill_cannot_approve")
            if self.approval_ref is None or self.approver_ref is None:
                raise ValueError("backfill_approval_evidence_missing")
        elif self.approval_ref is not None or self.approver_ref is not None:
            raise ValueError("backfill_approval_evidence_unexpected")
        return self


class BackfillBatch(_VersionedModel):
    """Bounded read-only evidence batch, replayable by its stable batch ref."""

    batch_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_digest: Digest
    observations: tuple[BackfillObservation, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_ITEMS,
    )
    read_only: Literal[True] = True
    batch_digest: Digest

    @model_validator(mode="after")
    def _observations_are_bound(self) -> BackfillBatch:
        if any(
            item.migration_ref != self.migration_ref
            or item.source_snapshot_digest != self.source_snapshot_digest
            or item.approval_state != "unreviewed"
            for item in self.observations
        ):
            raise ValueError("backfill_observation_binding_invalid")
        refs = [item.observation_ref for item in self.observations]
        if len(refs) != len(set(refs)):
            raise ValueError("backfill_observation_duplicate")
        if self.batch_digest != canonical_digest(self.observations):
            raise ValueError("backfill_batch_digest_mismatch")
        return self

    @classmethod
    def build(
        cls,
        *,
        batch_ref: str,
        migration_ref: str,
        source_snapshot_digest: str,
        observations: tuple[BackfillObservation, ...],
        version: int = 1,
    ) -> BackfillBatch:
        batch_digest = canonical_digest(observations)
        return cls(
            batch_ref=batch_ref,
            migration_ref=migration_ref,
            source_snapshot_digest=source_snapshot_digest,
            observations=observations,
            batch_digest=batch_digest,
            version=version,
            digest=canonical_digest(
                {
                    "batch_ref": batch_ref,
                    "batch_digest": batch_digest,
                    "version": version,
                }
            ),
        )


class CanonicalRecord(_VersionedModel):
    """Digest-only record identity used by shadow reconciliation."""

    canonical_ref: OpaqueRef
    record_digest: Digest
    source_ref: OpaqueRef
    source_revision: OpaqueRef
    source_file_ref: OpaqueRef | None = None
    source_record_ref: OpaqueRef | None = None
    metadata_digest: Digest


class ShadowDelta(_VersionedModel):
    """One deterministic difference; unchanged records are deliberately omitted."""

    canonical_ref: OpaqueRef
    kind: Literal["add", "update", "delete"]
    source_digest: Digest | None = None
    target_digest: Digest | None = None
    evidence_digest: Digest

    @model_validator(mode="after")
    def _delta_has_expected_digests(self) -> ShadowDelta:
        if self.kind == "add" and self.source_digest is None:
            raise ValueError("shadow_add_source_missing")
        if self.kind == "delete" and self.target_digest is None:
            raise ValueError("shadow_delete_target_missing")
        if self.kind == "update" and (
            self.source_digest is None or self.target_digest is None
        ):
            raise ValueError("shadow_update_digest_missing")
        return self


class ShadowReconciliationState(StrEnum):
    """How a shadow comparison of source and target ended."""

    CLEAN = "clean"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class ShadowReconciliation(_VersionedModel):
    """Deterministic source/target comparison bound to both snapshot digests."""

    reconciliation_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_digest: Digest
    target_snapshot_digest: Digest
    deltas: tuple[ShadowDelta, ...] = Field(max_length=MAX_EVIDENCE_ITEMS)
    delta_count: Count
    delta_digest: Digest
    state: Annotated[ShadowReconciliationState, Field(strict=False)]
    deterministic: Literal[True] = True

    @model_validator(mode="after")
    def _delta_summary_is_exact(self) -> ShadowReconciliation:
        if self.delta_count != len(self.deltas):
            raise ValueError("shadow_delta_count_mismatch")
        if self.delta_digest != canonical_digest(self.deltas):
            raise ValueError("shadow_delta_digest_mismatch")
        if self.state == ShadowReconciliationState.CLEAN and self.deltas:
            raise ValueError("shadow_clean_with_deltas")
        if self.state == ShadowReconciliationState.NEEDS_REVIEW and not self.deltas:
            raise ValueError("shadow_nonclean_without_deltas")
        return self


class GateCheck(_FrozenModel):
    """Bounded prerequisite evidence; a missing check never passes a gate."""

    check_ref: OpaqueRef
    passed: bool
    evidence_digest: Digest
    failure_code: OpaqueRef | None = None

    @model_validator(mode="after")
    def _failure_is_consistent(self) -> GateCheck:
        if self.passed and self.failure_code is not None:
            raise ValueError("passed_gate_check_has_failure")
        if not self.passed and self.failure_code is None:
            raise ValueError("failed_gate_check_missing_reason")
        return self


class PrerequisiteGate(_VersionedModel):
    """Explicit fail-closed gate for a stage transition."""

    gate_ref: OpaqueRef
    migration_ref: OpaqueRef
    stage: MigrationStage
    source_snapshot_digest: Digest
    checks: tuple[GateCheck, ...] = Field(min_length=1, max_length=64)
    decision: Literal["passed", "blocked"]

    @model_validator(mode="after")
    def _decision_is_fail_closed(self) -> PrerequisiteGate:
        refs = [check.check_ref for check in self.checks]
        if len(refs) != len(set(refs)):
            raise ValueError("gate_check_duplicate")
        if self.decision == "passed" and not all(check.passed for check in self.checks):
            raise ValueError("gate_passed_with_failed_check")
        if self.decision == "blocked" and all(check.passed for check in self.checks):
            raise ValueError("gate_blocked_without_failure")
        return self


class CohortReadCutover(_VersionedModel):
    """One cohort-scoped registry read cutover, never a fleet-wide flip."""

    EDGES: ClassVar[Mapping[tuple[CutoverState, CutoverEvent], CutoverState]] = (
        MappingProxyType(
            {(CutoverState.ACTIVE, CutoverEvent.ROLLBACK): CutoverState.ROLLED_BACK}
        )
    )
    INITIAL: ClassVar[CutoverState] = CutoverState.ACTIVE
    TERMINALS: ClassVar[tuple[CutoverState, ...]] = (CutoverState.ROLLED_BACK,)

    cutover_ref: OpaqueRef
    migration_ref: OpaqueRef
    cohort_ref: OpaqueRef
    registry_ref: OpaqueRef
    source_snapshot_digest: Digest
    gate_ref: OpaqueRef
    state: CutoverState = CutoverState.ACTIVE

    def transition(self, event: CutoverEvent) -> CohortReadCutover:
        """The next version of the cutover after ``event``; the only writer of the state."""
        target = self.EDGES.get((self.state, event))
        if target is None:
            raise MigrationTransitionRefused("cohort_read_cutover", self.state, event)
        return self._successor(state=target)


class WriteCutoverFence(_VersionedModel):
    """Single-authority write fence with legacy writes explicitly denied."""

    EDGES: ClassVar[Mapping[tuple[CutoverState, CutoverEvent], CutoverState]] = (
        MappingProxyType(
            {(CutoverState.ACTIVE, CutoverEvent.ROLLBACK): CutoverState.ROLLED_BACK}
        )
    )
    INITIAL: ClassVar[CutoverState] = CutoverState.ACTIVE
    TERMINALS: ClassVar[tuple[CutoverState, ...]] = (CutoverState.ROLLED_BACK,)

    fence_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_digest: Digest
    cohort_refs: tuple[OpaqueRef, ...] = Field(
        min_length=1,
        max_length=MAX_COHORTS,
    )
    authority_ref: OpaqueRef
    fence_epoch: Count
    gate_ref: OpaqueRef
    single_authority: Literal[True] = True
    legacy_writes_denied: Literal[True] = True
    state: CutoverState = CutoverState.ACTIVE

    @model_validator(mode="after")
    def _cohorts_are_unique(self) -> WriteCutoverFence:
        if len(self.cohort_refs) != len(set(self.cohort_refs)):
            raise ValueError("write_fence_cohort_duplicate")
        return self

    def transition(self, event: CutoverEvent) -> WriteCutoverFence:
        """The next version of the fence after ``event``; the only writer of the state."""
        target = self.EDGES.get((self.state, event))
        if target is None:
            raise MigrationTransitionRefused("write_cutover_fence", self.state, event)
        return self._successor(state=target)


class CheckpointState(StrEnum):
    """Whether a projection checkpoint is whole or the projection must be rebuilt."""

    COMPLETE = "complete"
    REBUILD_REQUIRED = "rebuild_required"


class ProjectionCheckpoint(_VersionedModel):
    """Restart-safe projection checkpoint/rebuild evidence."""

    checkpoint_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_digest: Digest
    projection_digest: Digest
    rebuild_plan_ref: OpaqueRef
    record_count: Count
    sequence: Version
    gate_ref: OpaqueRef
    state: Annotated[CheckpointState, Field(strict=False)]


class LegacyRetirement(_VersionedModel):
    """Retirement proof for temporary compatibility paths and old consumers."""

    EDGES: ClassVar[
        Mapping[tuple[RetirementState, RetirementEvent], RetirementState]
    ] = MappingProxyType(
        {
            (RetirementState.READY, RetirementEvent.RETIRE): RetirementState.RETIRED,
            (
                RetirementState.READY,
                RetirementEvent.ROLLBACK,
            ): RetirementState.ROLLED_BACK,
        }
    )
    INITIAL: ClassVar[RetirementState] = RetirementState.READY
    TERMINALS: ClassVar[tuple[RetirementState, ...]] = (
        RetirementState.RETIRED,
        RetirementState.ROLLED_BACK,
    )

    retirement_ref: OpaqueRef
    migration_ref: OpaqueRef
    source_snapshot_digest: Digest
    write_fence_ref: OpaqueRef
    checkpoint_ref: OpaqueRef
    remaining_consumers: Count
    remaining_facades: Count
    consumer_inventory_digest: Digest
    facade_inventory_digest: Digest
    gate_ref: OpaqueRef
    state: RetirementState

    @model_validator(mode="after")
    def _retirement_is_proven(self) -> LegacyRetirement:
        if self.state == RetirementState.RETIRED and (
            self.remaining_consumers != 0 or self.remaining_facades != 0
        ):
            raise ValueError("legacy_retirement_with_remaining_consumers")
        return self

    def transition(self, event: RetirementEvent) -> LegacyRetirement:
        """The next version of the proof after ``event``; the only writer of the state."""
        target = self.EDGES.get((self.state, event))
        if target is None:
            raise MigrationTransitionRefused("legacy_retirement", self.state, event)
        return self._successor(state=target)


class MigrationRollback(_VersionedModel):
    """Explicit, recorded rollback; no stage silently rolls back itself."""

    rollback_ref: OpaqueRef
    migration_ref: OpaqueRef
    from_stage: MigrationStage
    to_stage: MigrationStage
    source_snapshot_digest: Digest
    reason_ref: OpaqueRef
    restored_authority_ref: OpaqueRef
    fence_ref: OpaqueRef | None = None
    gate_ref: OpaqueRef
    decision: Literal["requested", "applied", "failed"]

    @model_validator(mode="after")
    def _rollback_direction_is_explicit(self) -> MigrationRollback:
        if self.from_stage == self.to_stage:
            raise ValueError("rollback_stage_unchanged")
        return self
