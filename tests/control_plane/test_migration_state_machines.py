"""The migration record machines (ARBORA-HOST-R041.1).

``MigrationPlan``, ``CohortReadCutover``, ``WriteCutoverFence`` and
``LegacyRetirement`` each move only through their own ``transition``.
"""

from __future__ import annotations

from itertools import product

import pytest

from arbora.control_plane.migrations import (
    CohortReadCutover,
    CutoverEvent,
    CutoverState,
    InMemoryMigrationAuthority,
    LegacyRetirement,
    MigrationCasConflictError,
    MigrationEvent,
    MigrationPlan,
    MigrationPrerequisiteError,
    MigrationRollback,
    MigrationStage,
    MigrationState,
    MigrationTransitionRefused,
    RetirementEvent,
    RetirementState,
    WriteCutoverFence,
    canonical_digest,
)
from tests.architecture.single_writer import field_writers
from tests.control_plane.test_migrations import (
    _digest,
    _prepare_fenced,
    _snapshot,
)

S, E = MigrationState, MigrationEvent


def _plan_in(state: MigrationState, *, version: int = 1) -> MigrationPlan:
    return MigrationPlan(
        migration_ref="migration:one",
        plan_ref="plan:one",
        source_snapshot_ref="snapshot:one",
        source_snapshot_digest=_digest("snapshot"),
        target_authority_ref="authority:target",
        state=state,
        stage=MigrationPlan.STAGE_OF[state],
        version=version,
        digest=_digest("plan"),
    )


def _fence(state: CutoverState = CutoverState.ACTIVE) -> WriteCutoverFence:
    return WriteCutoverFence(
        fence_ref="fence:one",
        migration_ref="migration:one",
        source_snapshot_digest=_digest("snapshot"),
        cohort_refs=("cohort:one",),
        authority_ref="authority:target",
        fence_epoch=1,
        gate_ref="gate:write",
        state=state,
        version=1,
        digest=_digest("fence"),
    )


def _cutover(state: CutoverState = CutoverState.ACTIVE) -> CohortReadCutover:
    return CohortReadCutover(
        cutover_ref="cutover:one",
        migration_ref="migration:one",
        cohort_ref="cohort:one",
        registry_ref="registry:one",
        source_snapshot_digest=_digest("snapshot"),
        gate_ref="gate:read",
        state=state,
        version=1,
        digest=_digest("cutover"),
    )


def _retirement(
    state: RetirementState = RetirementState.READY, *, consumers: int = 0
) -> LegacyRetirement:
    return LegacyRetirement(
        retirement_ref="retirement:one",
        migration_ref="migration:one",
        source_snapshot_digest=_digest("snapshot"),
        write_fence_ref="fence:one",
        checkpoint_ref="checkpoint:one",
        remaining_consumers=consumers,
        remaining_facades=0,
        consumer_inventory_digest=_digest("consumers"),
        facade_inventory_digest=_digest("facades"),
        gate_ref="gate:retire",
        state=state,
        version=1,
        digest=_digest("retirement"),
    )


def _digest_is_fresh(record: MigrationPlan | WriteCutoverFence) -> bool:
    data = record.model_dump(mode="python")
    data.pop("digest")
    return record.digest == canonical_digest(data)


@pytest.mark.spec("ARBORA-HOST-R041.1")
@pytest.mark.parametrize(
    ("source", "event", "target"),
    [
        pytest.param(
            S.PLANNED,
            E.FREEZE_INVENTORY,
            S.INVENTORY_FROZEN,
            id="planned-freeze_inventory",
        ),
        pytest.param(
            S.INVENTORY_FROZEN,
            E.RECORD_BACKFILL,
            S.BACKFILL_OBSERVING,
            id="inventory_frozen-record_backfill",
        ),
        pytest.param(
            S.BACKFILL_OBSERVING,
            E.RECORD_BACKFILL,
            S.BACKFILL_OBSERVING,
            id="backfill_observing-record_backfill",
        ),
        pytest.param(
            S.BACKFILL_OBSERVING,
            E.RECONCILE_CLEAN,
            S.SHADOW_RECONCILED,
            id="backfill_observing-reconcile_clean",
        ),
        pytest.param(
            S.SHADOW_RECONCILED,
            E.CUTOVER_READS,
            S.READ_CUTOVER,
            id="shadow_reconciled-cutover_reads",
        ),
        pytest.param(
            S.READ_CUTOVER,
            E.CUTOVER_READS,
            S.READ_CUTOVER,
            id="read_cutover-cutover_reads",
        ),
        pytest.param(
            S.READ_CUTOVER,
            E.FENCE_WRITES,
            S.WRITE_FENCED,
            id="read_cutover-fence_writes",
        ),
        pytest.param(
            S.WRITE_FENCED, E.CHECKPOINT, S.CHECKPOINTED, id="write_fenced-checkpoint"
        ),
        pytest.param(S.CHECKPOINTED, E.RETIRE, S.RETIRED, id="checkpointed-retire"),
        pytest.param(S.PLANNED, E.ROLLBACK, S.ROLLED_BACK, id="planned-rollback"),
        pytest.param(
            S.INVENTORY_FROZEN,
            E.ROLLBACK,
            S.ROLLED_BACK,
            id="inventory_frozen-rollback",
        ),
        pytest.param(
            S.BACKFILL_OBSERVING,
            E.ROLLBACK,
            S.ROLLED_BACK,
            id="backfill_observing-rollback",
        ),
        pytest.param(
            S.SHADOW_RECONCILED,
            E.ROLLBACK,
            S.ROLLED_BACK,
            id="shadow_reconciled-rollback",
        ),
        pytest.param(
            S.READ_CUTOVER, E.ROLLBACK, S.ROLLED_BACK, id="read_cutover-rollback"
        ),
        pytest.param(
            S.WRITE_FENCED, E.ROLLBACK, S.ROLLED_BACK, id="write_fenced-rollback"
        ),
        pytest.param(
            S.CHECKPOINTED, E.ROLLBACK, S.ROLLED_BACK, id="checkpointed-rollback"
        ),
        pytest.param(
            S.ROLLED_BACK, E.ROLLBACK, S.ROLLED_BACK, id="rolled_back-rollback"
        ),
    ],
)
def test_plan_legal_edge_succeeds(
    source: MigrationState, event: MigrationEvent, target: MigrationState
) -> None:
    plan = _plan_in(source)
    after = plan.transition(event, expected_version=1)
    assert after.state is target
    assert after.stage is MigrationPlan.STAGE_OF[target]
    assert after.version == 2
    assert _digest_is_fresh(after)
    assert plan.state is source and plan.version == 1


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_plan_every_illegal_pair_is_refused_and_the_plan_is_unchanged() -> None:
    legal = set(MigrationPlan.EDGES)
    refused = 0
    for state, event in product(MigrationState, MigrationEvent):
        if (state, event) in legal:
            continue
        plan = _plan_in(state)
        with pytest.raises(MigrationTransitionRefused) as raised:
            plan.transition(event, expected_version=1)
        assert raised.value.machine == "migration_plan"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        assert str(raised.value) == MigrationPlan.REFUSAL_CODES[event]
        assert isinstance(raised.value, MigrationPrerequisiteError)
        assert plan.state is state and plan.version == 1
        refused += 1
    assert refused == len(MigrationState) * len(MigrationEvent) - len(legal) == 55


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_plan_retired_is_terminal_and_never_rolls_back() -> None:
    assert MigrationPlan.TERMINALS == (MigrationState.RETIRED,)
    assert not [pair for pair in MigrationPlan.EDGES if pair[0] is S.RETIRED]
    assert MigrationPlan.INITIAL is MigrationState.PLANNED


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_plan_transition_checks_the_expected_version_after_the_edge() -> None:
    plan = _plan_in(S.PLANNED)
    with pytest.raises(MigrationCasConflictError):
        plan.transition(E.FREEZE_INVENTORY, expected_version=2)
    with pytest.raises(MigrationCasConflictError):
        plan.transition(E.FREEZE_INVENTORY, expected_version=None)
    with pytest.raises(MigrationTransitionRefused):
        plan.transition(E.CHECKPOINT, expected_version=99)
    exhausted = _plan_in(S.PLANNED, version=2_147_483_647)
    with pytest.raises(MigrationCasConflictError, match="version_exhausted"):
        exhausted.transition(E.FREEZE_INVENTORY, expected_version=2_147_483_647)


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_stored_values_are_the_wire_values() -> None:
    assert [state.value for state in MigrationState] == [
        "planned",
        "inventory_frozen",
        "backfill_observing",
        "shadow_reconciled",
        "read_cutover",
        "write_fenced",
        "checkpointed",
        "retired",
        "rolled_back",
    ]
    assert [stage.value for stage in MigrationStage] == [
        "stage_0_inventory_freeze",
        "stage_1_read_only_backfill",
        "stage_2_shadow_reconciliation",
        "stage_3_cohort_read_cutover",
        "stage_4_write_cutover",
        "stage_5_projection_checkpoint",
        "legacy_retirement",
        "rollback",
    ]
    stored = _plan_in(S.READ_CUTOVER).model_dump(mode="json")
    assert stored["state"] == "read_cutover"
    assert stored["stage"] == "stage_3_cohort_read_cutover"
    assert [state.value for state in CutoverState] == ["active", "rolled_back"]
    assert [state.value for state in RetirementState] == [
        "ready",
        "retired",
        "rolled_back",
    ]


@pytest.mark.spec("ARBORA-HOST-R041.1")
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_cutover, id="active-rollback-cohort_read_cutover"),
        pytest.param(_fence, id="active-rollback-write_cutover_fence"),
    ],
)
def test_cutover_active_rollback_edge_succeeds(build) -> None:  # type: ignore[no-untyped-def]
    record = build()
    after = record.transition(CutoverEvent.ROLLBACK)
    assert after.state is CutoverState.ROLLED_BACK
    assert after.version == 2
    assert after.digest != record.digest
    assert record.state is CutoverState.ACTIVE


@pytest.mark.spec("ARBORA-HOST-R041.1")
@pytest.mark.parametrize("build", [_cutover, _fence])
def test_cutover_rolled_back_is_refused_and_unchanged(build) -> None:  # type: ignore[no-untyped-def]
    record = build(CutoverState.ROLLED_BACK)
    with pytest.raises(MigrationTransitionRefused) as raised:
        record.transition(CutoverEvent.ROLLBACK)
    assert raised.value.from_state == CutoverState.ROLLED_BACK
    assert record.state is CutoverState.ROLLED_BACK and record.version == 1


@pytest.mark.spec("ARBORA-HOST-R041.1")
@pytest.mark.parametrize(
    ("event", "target"),
    [
        pytest.param(
            RetirementEvent.RETIRE, RetirementState.RETIRED, id="ready-retire"
        ),
        pytest.param(
            RetirementEvent.ROLLBACK, RetirementState.ROLLED_BACK, id="ready-rollback"
        ),
    ],
)
def test_retirement_ready_edges_succeed(
    event: RetirementEvent, target: RetirementState
) -> None:
    after = _retirement().transition(event)
    assert after.state is target and after.version == 2


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_retirement_every_illegal_pair_is_refused() -> None:
    legal = set(LegacyRetirement.EDGES)
    for state, event in product(RetirementState, RetirementEvent):
        if (state, event) in legal:
            continue
        record = _retirement(state)
        with pytest.raises(MigrationTransitionRefused) as raised:
            record.transition(event)
        assert raised.value.machine == "legacy_retirement"
        assert record.state is state


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_retirement_with_remaining_consumers_cannot_retire() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _retirement(consumers=1).transition(RetirementEvent.RETIRE)


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_plan_rollback_rolls_back_the_fence_and_every_active_read_cutover() -> None:
    authority, snapshot, plan, fence = _prepare_fenced()
    assert authority._read_cutovers["cutover:one"].state is CutoverState.ACTIVE
    rollback = MigrationRollback(
        rollback_ref="rollback:one",
        migration_ref=plan.migration_ref,
        from_stage=MigrationStage.WRITE_CUTOVER,
        to_stage=MigrationStage.COHORT_READ_CUTOVER,
        source_snapshot_digest=snapshot.snapshot_digest,
        reason_ref="reason:verification-failed",
        restored_authority_ref="authority:legacy",
        fence_ref=fence.fence_ref,
        gate_ref="gate:rollback",
        decision="requested",
        version=1,
        digest=_digest("rollback-one"),
    )
    rolled = authority.rollback(
        plan.plan_ref, rollback, expected_plan_version=plan.version
    )
    assert rolled.state is MigrationState.ROLLED_BACK
    assert authority.get_write_fence(fence.fence_ref).state is CutoverState.ROLLED_BACK
    assert authority._read_cutovers["cutover:one"].state is CutoverState.ROLLED_BACK
    assert authority._read_cutovers["cutover:one"].version == 2


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_a_retired_plan_refuses_rollback_through_the_authority() -> None:
    authority = InMemoryMigrationAuthority()
    snapshot = _snapshot()
    plan = _plan_in(S.RETIRED)
    authority._plans[plan.plan_ref] = plan
    rollback = MigrationRollback(
        rollback_ref="rollback:late",
        migration_ref=plan.migration_ref,
        from_stage=MigrationStage.LEGACY_RETIREMENT,
        to_stage=MigrationStage.PROJECTION_CHECKPOINT,
        source_snapshot_digest=plan.source_snapshot_digest,
        reason_ref="reason:late",
        restored_authority_ref="authority:legacy",
        gate_ref="gate:rollback",
        decision="requested",
        version=1,
        digest=_digest("rollback-late"),
    )
    assert snapshot is not None
    with pytest.raises(
        MigrationTransitionRefused, match="retired_migration_not_rollbackable"
    ):
        authority.rollback(plan.plan_ref, rollback, expected_plan_version=1)


@pytest.mark.spec("ARBORA-HOST-R041.1")
def test_only_the_transition_functions_write_the_migration_state_fields() -> None:
    writers = field_writers("arbora/control_plane/migrations", {"state", "stage"})
    assert writers == {("arbora/control_plane/migrations/models.py", "transition")}
