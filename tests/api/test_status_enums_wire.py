"""Typed status enums keep the wire and stored text of the strings they replaced.

Each converted model dumps to the same JSON it did when its status was a free
string or a ``Literal``: the enum members are the old texts, byte for byte.
"""

from __future__ import annotations

import json

import pytest

from arbora.a2a.admission import ModelProfileUnavailable
from arbora.a2a.work_items import work_item_view
from arbora.api.domain.a2a.models import (
    A2AReplyUnavailable,
    A2ATaskState,
    A2ATaskStatus,
    A2ATransition,
)
from arbora.api.domain.finance.read_models import Freshness, FreshnessState
from arbora.api.domain.fleet.catalog_reader import ComponentLifecycle, ReplicaState
from arbora.api.domain.ingest.service import IngestPackRecord, IngestSourceState
from arbora.api.invoke.audit import ReservationState
from arbora.api.ops.fleet import ReplicaGeneration
from arbora.core.lease_status import LeaseStatus
from arbora.core.work_item_status import WorkItemStatus


def test_a2a_task_state_texts_are_the_protocol_texts() -> None:
    assert [state.value for state in A2ATaskState] == [
        "submitted",
        "working",
        "input-required",
        "completed",
        "canceled",
        "failed",
        "rejected",
    ]
    assert A2ATaskStatus(state=A2ATaskState("working")).model_dump_json(
        by_alias=True
    ) == ('{"state":"working","timestamp":null}')
    with pytest.raises(ValueError):
        A2ATaskStatus(state=A2ATaskState("done"))


def test_a2a_transition_and_reply_availability_dump_the_old_json() -> None:
    transition = A2ATransition(state="dead_letter", at_ms=5, revision=2)
    assert json.loads(transition.model_dump_json(by_alias=True)) == {
        "state": "dead_letter",
        "atMs": 5,
        "revision": 2,
    }
    reply = A2AReplyUnavailable(reason="reply_too_large")
    assert json.loads(reply.model_dump_json()) == {
        "status": "unavailable",
        "reason": "reply_too_large",
    }
    assert json.loads(ModelProfileUnavailable().model_dump_json()) == {
        "status": "unavailable",
        "reason": "AUTHORITY_UNAVAILABLE",
    }


def test_work_item_status_is_the_engines_eight_stored_texts() -> None:
    assert [status.value for status in WorkItemStatus] == [
        "submitted",
        "ready",
        "leased",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "dead_letter",
    ]
    record = {
        "work_item_id": "w",
        "kind": "k",
        "tenant": "t",
        "created_by": "c",
        "state": "leased",
    }
    assert work_item_view(record).state == "leased"
    assert work_item_view({**record, "state": "unheard_of"}).state is None


def test_lease_and_reservation_texts_are_unchanged() -> None:
    assert [s.value for s in LeaseStatus] == [
        "active",
        "consumed",
        "revoked",
        "expired",
    ]
    assert [s.value for s in ReservationState] == [
        "acquired",
        "pending",
        "completed",
        "conflict",
    ]
    assert [s.value for s in ComponentLifecycle] == [
        "published",
        "retired",
        "withdrawn",
    ]


def test_fleet_and_ingest_records_dump_the_old_json() -> None:
    row = ReplicaGeneration(
        replica="r1", state=ReplicaState("converging"), engine_generation=3
    )
    assert json.loads(row.model_dump_json()) == {
        "replica": "r1",
        "state": "converging",
        "reason": None,
        "generation": None,
        "engine_generation": 3,
    }
    assert [s.value for s in ReplicaState] == [
        "converged",
        "converging",
        "degraded",
    ]
    pack = IngestPackRecord(pack_id="p", tenant="t", state=IngestSourceState("error"))
    assert pack.state is IngestSourceState.ERROR
    assert json.loads(pack.model_dump_json()) == {
        "pack_id": "p",
        "tenant": "t",
        "state": "error",
    }
    assert json.loads(
        Freshness(state=FreshnessState("stale"), age_seconds=None).model_dump_json()
    ) == {
        "state": "stale",
        "age_seconds": None,
    }
