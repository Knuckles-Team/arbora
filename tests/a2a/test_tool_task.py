"""Reference-bound single-tool tasks under one WorkItem lease (ARBORA-A2A-R002.2.2)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.a2a.tool_task import (
    ToolTaskCarrier,
    ToolTaskRequest,
    ToolTaskResult,
    WorkItemLeaseLost,
    run_tool_task,
    tool_task_work_item_id,
)

pytestmark = [
    pytest.mark.spec("ARBORA-A2A-R002.2.2"),
    pytest.mark.spec("ARBORA-A2A-R002.2.2.1"),
]

REQUEST = ToolTaskRequest(
    operation_id="graph.query",
    parameters_json='{"q":"x"}',
    principal="user:alice",
    tenant="tenant-a",
    session_id="session-1",
    authority_ref="authority:alice",
    policy_revision="policy:1",
    registry_digest="a" * 64,
)
CARRIER = ToolTaskCarrier(
    job_id="job-1",
    payload_ref=REQUEST.request_ref,
    tenant="tenant-a",
    session_id="session-1",
    allowed_tool_subset=("graph.query",),
    deadline_unix=100.0,
)
ITEM = tool_task_work_item_id("job-1")


def _result(**changes: Any) -> ToolTaskResult:
    values: dict[str, Any] = {
        "job_id": "job-1",
        "request_ref": REQUEST.request_ref,
        "principal": "user:alice",
        "outcome": "completed",
        "receipt_ref": "receipt:1",
    }
    values.update(changes)
    return ToolTaskResult(**values)


class Fence:
    """An in-memory stand-in for the EG WorkItem coordination methods."""

    def __init__(self, status: str = "ready", *, claimable: bool = True) -> None:
        self.record: dict[str, Any] | None = {
            "payload_ref": REQUEST.request_ref,
            "tenant": "tenant-a",
            "status": status,
        }
        self.claimable = claimable
        self.lost_after: int | None = None
        self.checks = 0
        self.commits: list[tuple[bool, str]] = []

    def get(self, item_id: str) -> dict[str, Any] | None:
        assert item_id == ITEM
        return self.record

    def claim(self, item_id: str) -> str | None:
        return "claim-1" if self.claimable else None

    def require_current(self, item_id: str, claim: str) -> None:
        self.checks += 1
        if self.lost_after is not None and self.checks > self.lost_after:
            raise WorkItemLeaseLost(item_id)

    def commit(self, item_id: str, claim: str, *, succeeded: bool, ref: str) -> None:
        self.commits.append((succeeded, ref))


class Dispatcher:
    def __init__(
        self,
        *,
        request: object = REQUEST,
        execute: Callable[[], object] = _result,
    ) -> None:
        self.request = request
        self._execute = execute
        self.executed = 0

    def resolve(self, request_ref: str, *, tenant: str, session_id: str) -> Any:
        return self.request

    def execute(self, request: Any, *, job_id: str, require_current: Any) -> Any:
        self.executed += 1
        require_current()
        return self._execute()


def _run(
    fence: Fence, dispatcher: Dispatcher | None, *, carrier: ToolTaskCarrier = CARRIER
) -> tuple[str, bool]:
    run = run_tool_task(carrier, fence=fence, dispatcher=dispatcher, now=50.0)
    return run.outcome, run.acknowledge


def _raise(exc: Exception) -> Callable[[], object]:
    def boom() -> object:
        raise exc

    return boom


@pytest.mark.parametrize(
    ("outcome", "succeeded"),
    [("completed", True), ("failed", False), ("refused", False)],
)
def test_bound_result_commits_its_receipt(outcome: str, succeeded: bool) -> None:
    fence = Fence()
    dispatcher = Dispatcher(execute=lambda: _result(outcome=outcome))
    assert _run(fence, dispatcher) == (outcome, True)
    assert fence.commits == [(succeeded, "receipt:1")]
    assert dispatcher.executed == 1


@pytest.mark.parametrize(
    "execute",
    [
        _raise(RuntimeError("lost reply")),
        _raise(ValueError("bad")),
        lambda: _result(outcome="indeterminate"),
        lambda: _result(job_id="job-2"),
        lambda: _result(principal="user:mallory"),
        lambda: {"outcome": "completed"},
    ],
    ids=[
        "error",
        "value-error",
        "indeterminate",
        "wrong-job",
        "wrong-caller",
        "untyped",
    ],
)
def test_uncertain_effect_stays_open_and_unacknowledged(execute) -> None:
    fence = Fence()
    assert _run(fence, Dispatcher(execute=execute)) == ("indeterminate", False)
    assert fence.commits == []


@pytest.mark.parametrize(
    "dispatcher",
    [
        None,
        Dispatcher(request={"operation_id": "graph.query"}),
        Dispatcher(request=REQUEST.model_copy(update={"operation_id": "graph.write"})),
        Dispatcher(request=REQUEST.model_copy(update={"tenant": "tenant-b"})),
        Dispatcher(request=REQUEST.model_copy(update={"parameters_json": '{"q":"y"}'})),
    ],
    ids=["no-dispatcher", "untyped", "other-tool", "other-tenant", "changed-input"],
)
def test_pre_effect_mismatch_refuses_terminally(dispatcher) -> None:
    fence = Fence()
    outcome, acknowledged = _run(fence, dispatcher)
    assert (outcome, acknowledged) == ("refused", True)
    assert [ok for ok, _ in fence.commits] == [False]
    assert getattr(dispatcher, "executed", 0) == 0


@pytest.mark.parametrize(
    ("fence", "carrier", "expected"),
    [
        (Fence(status="succeeded"), CARRIER, ("skipped", True)),
        (Fence(claimable=False), CARRIER, ("fenced", False)),
        (
            Fence(),
            CARRIER.model_copy(update={"deadline_unix": 10.0}),
            ("indeterminate", False),
        ),
        (
            Fence(),
            CARRIER.model_copy(update={"tenant": "tenant-b"}),
            ("rejected", False),
        ),
    ],
    ids=["duplicate-delivery", "claimed-elsewhere", "deadline", "foreign-carrier"],
)
def test_no_execution_without_a_current_claim(fence, carrier, expected) -> None:
    dispatcher = Dispatcher()
    assert _run(fence, dispatcher, carrier=carrier) == expected
    assert dispatcher.executed == 0
    assert fence.commits == []


@pytest.mark.parametrize("lost_after", [0, 2, 3])
def test_lease_loss_is_fenced_and_never_commits(lost_after: int) -> None:
    fence = Fence()
    fence.lost_after = lost_after
    assert _run(fence, Dispatcher()) == ("fenced", False)
    assert fence.commits == []


def test_missing_admitted_item_is_rejected() -> None:
    fence = Fence()
    fence.record = None
    assert _run(fence, Dispatcher()) == ("rejected", False)


@pytest.mark.parametrize(
    "changes",
    [
        {"parameters_json": '{"b":1, "a":2}'},
        {"parameters_json": "[1]"},
        {"registry_digest": "short"},
        {"extra": "field"},
    ],
)
def test_request_must_be_canonical_and_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ToolTaskRequest.model_validate({**REQUEST.model_dump(), **changes})


def test_request_ref_binds_every_field() -> None:
    changed = REQUEST.model_copy(update={"authority_ref": "authority:bob"})
    assert changed.request_ref != REQUEST.request_ref
    assert REQUEST.request_ref.startswith("tool-task:sha256:")
    with pytest.raises(ValidationError):
        CARRIER.model_validate(
            {**CARRIER.model_dump(), "allowed_tool_subset": ("a", "b")}
        )
