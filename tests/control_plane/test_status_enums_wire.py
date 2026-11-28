"""Typed status enums on the strict control-plane models keep their old JSON.

The models are strict, yet a status still validates from its plain text (the
enum field opts out of strictness), so stored rows and wire bodies load as before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum

import pytest

from arbora.control_plane.economics.models import (
    LateEventDecision,
    LateEventStatus,
    ReconciliationStatus,
    SloRollupStatus,
)
from arbora.control_plane.foundation.models import LifecycleState, RecordStatus
from arbora.control_plane.migrations.models import (
    CheckpointState,
    InventoryState,
    ShadowReconciliationState,
)
from arbora.control_plane.projection.models import OutcomeStatus
from arbora.control_plane.projection.projector import BatchStatus
from arbora.control_plane.webui.models import IdentityState, TenantIdentity


@pytest.mark.parametrize(
    ("enum", "texts"),
    [
        (
            SloRollupStatus,
            [
                "met",
                "breached",
                "insufficient_data",
            ],
        ),
        (ReconciliationStatus, ["complete", "gap", "drift"]),
        (
            LateEventStatus,
            [
                "accepted",
                "correction_required",
                "rejected",
            ],
        ),
        (
            ShadowReconciliationState,
            [
                "clean",
                "needs_review",
                "blocked",
            ],
        ),
        (CheckpointState, ["complete", "rebuild_required"]),
        (
            InventoryState,
            [
                "present",
                "missing",
                "duplicate",
                "rejected",
                "malformed",
            ],
        ),
        (
            OutcomeStatus,
            [
                "applied",
                "replayed",
                "rejected",
                "gap",
                "conflict",
                "failed",
                "drift",
                "cleaned",
            ],
        ),
        (
            BatchStatus,
            [
                "complete",
                "applied",
                "replayed",
                "blocked",
                "failed",
                "drift",
            ],
        ),
        (IdentityState, ["active", "deleted"]),
        (RecordStatus, ["active", "retired"]),
    ],
)
def test_enum_texts_are_the_old_literal_texts(
    enum: type[StrEnum], texts: list[str]
) -> None:
    assert [member.value for member in enum] == texts


def test_strict_models_load_the_plain_text_and_dump_it_unchanged() -> None:
    lifecycle = LifecycleState.model_validate(
        {
            "lifecycle_version": "lifecycle.v1",
            "status": "retired",
            "retention_policy_ref": "retention:default",
        }
    )
    assert lifecycle.status is RecordStatus.RETIRED
    assert json.loads(lifecycle.model_dump_json()) == {
        "lifecycle_version": "lifecycle.v1",
        "status": "retired",
        "retention_policy_ref": "retention:default",
    }
    identity = TenantIdentity.model_validate(
        {"version": 1, "digest": "sha256:" + "a" * 64, "tenant_ref": "tenant-1"}
    )
    assert json.loads(identity.model_dump_json())["state"] == "active"
    decision = LateEventDecision.model_validate(
        {
            "fact_id": "f1",
            "status": "correction_required",
            "reason": "within_lateness",
            "watermark_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    assert json.loads(decision.model_dump_json())["status"] == "correction_required"
    with pytest.raises(ValueError):
        LifecycleState.model_validate(
            {
                "lifecycle_version": "lifecycle.v1",
                "status": "paused",
                "retention_policy_ref": "retention:default",
            }
        )
