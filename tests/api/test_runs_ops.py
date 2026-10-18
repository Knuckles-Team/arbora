"""``runs.current_plan`` read boundary (ARBORA-OPS-R021.4.5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from agent_connector_sdk.current_plan import (
    CurrentPlanRead,
    CurrentPlanRequest,
    CurrentPlanSnapshot,
    PlanItem,
)
from pydantic import ValidationError

from arbora.api.ops import runs
from arbora.api.ops.runs import (
    UNAVAILABLE,
    project_current_plan,
)
from tests.api._ops_support import tenant_reader_context

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.5.1")

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot(**changes: object) -> CurrentPlanSnapshot:
    values: dict[str, object] = {
        "run_id": "run-one",
        "revision": 0,
        "observed_at": NOW,
        "expires_at": NOW + timedelta(seconds=30),
        "items": (),
    }
    values.update(changes)
    return CurrentPlanSnapshot.model_validate(values)


def _available(**changes: object) -> CurrentPlanRead:
    return CurrentPlanRead(availability="available", snapshot=_snapshot(**changes))


class _Reader:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def read(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.result


async def test_served_read_forwards_caller_and_keeps_order(op_by_id) -> None:
    now = datetime.now(UTC)
    items = tuple(
        PlanItem.model_validate({"text": s, "status": s})
        for s in ("done", "in_progress", "pending")
    )
    reader = _Reader(
        _available(observed_at=now, expires_at=now + timedelta(hours=1), items=items)
    )
    op = op_by_id(runs.operations(), "runs.current_plan")
    context = tenant_reader_context(reader=reader, service_name="current_plan_reader")
    result = await runs.handle_runs_current_plan(context, {"run_id": "run-one"}, op)
    assert reader.calls == [{"run_id": "run-one", "context": context}]
    assert result["value"]["availability"] == "available"
    assert [i["status"] for i in result["value"]["snapshot"]["items"]] == [
        "done",
        "in_progress",
        "pending",
    ]
    assert op.scopes == frozenset({"kg:read"})


async def test_absent_reader_is_unavailable_with_no_items(op_by_id) -> None:
    op = op_by_id(runs.operations(), "runs.current_plan")
    context = tenant_reader_context(reader=None, service_name="current_plan_reader")
    result = await runs.handle_runs_current_plan(context, {"run_id": "run-one"}, op)
    assert result == {"value": {"availability": "unavailable", "snapshot": None}}


@pytest.mark.parametrize(
    "params",
    [
        {"run_id": ""},
        {"run_id": 7},
        {"tenant": "x"},
        {"run_id": "run-one", "tenant": "other"},
        {"run_id": "run-one", "principal": "other"},
    ],
)
async def test_malformed_params_never_reach_the_reader(op_by_id, params) -> None:
    reader = _Reader(UNAVAILABLE)
    op = op_by_id(runs.operations(), "runs.current_plan")
    context = tenant_reader_context(reader=reader, service_name="current_plan_reader")
    with pytest.raises(ValidationError):
        await runs.handle_runs_current_plan(context, params, op)
    assert reader.calls == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_available(), "available"),
        (_available(run_id="other"), "unavailable"),
        (_available(observed_at=NOW + timedelta(seconds=1)), "unavailable"),
        (None, "unavailable"),
        (
            _available(
                observed_at=NOW - timedelta(seconds=60),
                expires_at=NOW - timedelta(seconds=30),
            ),
            "unavailable",
        ),
        ({"availability": "available"}, "unavailable"),
        (CurrentPlanRead.model_construct(availability="available"), "unavailable"),
        (UNAVAILABLE, "unavailable"),
    ],
    ids=[
        "current",
        "wrong-run",
        "future",
        "not-a-read",
        "expired",
        "dict",
        "forged",
        "none",
    ],
)
def test_projection_fails_closed(raw: object, expected: str) -> None:
    read = project_current_plan(raw, run_id="run-one", now=NOW)
    assert read.availability == expected
    assert (read.snapshot is None) == (expected == "unavailable")


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": -1},
        {"revision": True},
        {"run_id": ""},
        {"extra": "rejected"},
        {"observed_at": datetime(2026, 1, 1)},
        {"expires_at": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))},
        {"expires_at": NOW},
        {"items": ({"text": "item", "status": "unknown"},)},
        {"items": ({"text": "", "status": "pending"},)},
    ],
)
def test_malformed_snapshot_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _snapshot(**changes)


@pytest.mark.parametrize(
    "values",
    [
        {"availability": "available"},
        {"availability": "unavailable", "snapshot": _snapshot().model_dump()},
        {"availability": "unknown"},
    ],
)
def test_availability_must_match_snapshot(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CurrentPlanRead.model_validate(values)


def test_registry_and_projection_use_sdk_contract() -> None:
    op = runs.operations()[0]
    assert op.params is CurrentPlanRequest
    value = CurrentPlanRead(availability="available", snapshot=_snapshot())
    assert project_current_plan(value, run_id="run-one", now=NOW) == value
