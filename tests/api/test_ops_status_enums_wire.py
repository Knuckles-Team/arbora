"""Typed status enums on the served operation models keep their old JSON."""

from __future__ import annotations

import json

import pytest

from arbora.api.ops.atlas import AtlasRunState, RunsListParams
from arbora.api.ops.bundle import BundleUploadParams, BundleUploadPhase
from arbora.api.ops.development_effects import EffectReceipt, EffectState
from arbora.api.ops.graph_changes import GraphRecoverResult, GraphRecoverState
from arbora.api.ops.policy import PolicyEntity, PolicyEntityState
from arbora.api.ops.run_graph import ChildRunStatus, ExecutionChildRun
from arbora.api.ops.sessions import SessionCost, SessionCostStatus
from arbora.api.ops.usage import TurnPhase, TurnTiming


def _json(model: object) -> dict[str, object]:
    return json.loads(model.model_dump_json())  # type: ignore[attr-defined]


def test_enum_texts_are_the_old_literal_texts() -> None:
    assert [s.value for s in AtlasRunState] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert [s.value for s in BundleUploadPhase] == ["begin", "chunk", "commit"]
    assert [s.value for s in EffectState] == ["done", "absent"]
    assert [s.value for s in GraphRecoverState] == ["continue", "recover"]
    assert [s.value for s in PolicyEntityState] == ["active", "retired"]
    assert [s.value for s in ChildRunStatus] == ["unobserved"]
    assert [s.value for s in SessionCostStatus] == ["measured", "unknown"]
    assert [s.value for s in TurnPhase] == ["request", "run", "queue", "tool"]


def test_models_dump_the_old_json_and_refuse_other_texts() -> None:
    assert _json(
        GraphRecoverResult.model_validate({"state": "recover", "cursor": "c"})
    ) == {"state": "recover", "cursor": "c", "snapshot": None}
    assert _json(
        EffectReceipt.model_validate({"idempotency_key": "k", "state": "absent"})
    ) == {"idempotency_key": "k", "state": "absent", "receipt": None}
    assert SessionCost.model_fields["status"].annotation is SessionCostStatus
    assert (
        _json(
            TurnTiming.model_validate(
                {"run_id": "r", "turn_index": 0, "phase": "tool", "duration_ms": 1}
            )
        )["phase"]
        == "tool"
    )
    assert _json(RunsListParams.model_validate({"state": "queued"}))["state"] == (
        "queued"
    )
    assert (
        _json(
            ExecutionChildRun.model_validate(
                {"key": "k", "run_id": "r", "task_id": "t", "depth": 1}
            )
        )["status"]
        == "unobserved"
    )
    assert _json(BundleUploadParams.model_validate({"phase": "begin"}))["phase"] == (
        "begin"
    )
    with pytest.raises(ValueError):
        GraphRecoverResult.model_validate({"state": "rebuild", "cursor": "c"})
    with pytest.raises(ValueError):
        PolicyEntity.model_validate(
            {
                "policy_ref": "p",
                "version_digest": "d" * 64,
                "constitution_ref": "c",
                "clause_digest": "e" * 64,
                "state": "paused",
            }
        )
