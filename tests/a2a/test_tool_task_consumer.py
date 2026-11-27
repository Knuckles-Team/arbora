"""Consumer behavior over controlled ports (ARBORA-A2A-R002.2.3.x).

These tests exercise the consumer, caller recovery, uncertain-effect
reconciliation and duplicate delivery with in-memory ports. The served
bootstrap proof with the real engine queue stays under the parent rows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from arbora.a2a.caller_authority import (
    CALLER_AUTHORITY_EXPIRED,
    CALLER_AUTHORITY_REVOKED,
)
from arbora.a2a.tool_task import (
    ToolTaskCarrier,
    ToolTaskRequest,
    ToolTaskResult,
    WorkItemLeaseLost,
    tool_task_work_item_id,
)
from arbora.a2a.tool_task_consumer import ToolTaskConsumer

REQUEST = ToolTaskRequest(
    operation_id="graph.query",
    parameters_json='{"q":"x"}',
    principal="user:alice",
    tenant="tenant-a",
    session_id="session-1",
    authority_ref="delegation:1",
    policy_revision="p1",
    registry_digest="a" * 64,
)
CARRIER = ToolTaskCarrier(
    job_id="job-1",
    payload_ref=REQUEST.request_ref,
    tenant="tenant-a",
    session_id="session-1",
    allowed_tool_subset=("graph.query",),
)
ITEM = tool_task_work_item_id("job-1")


def delegation(**changes: Any) -> dict[str, Any]:
    state = {
        "standing": "active",
        "principal_active": True,
        "holds_tool_scope": True,
        "record": {
            "tenant": "tenant-a",
            "principal": "user:alice",
            "tool": "graph.query",
            "expires_at": 9_999_999,
            "revoked_at": None,
        },
    }
    state.update(changes)
    return state


class Fence:
    def __init__(self) -> None:
        self.row: dict[str, Any] = {
            "status": "ready",
            "payload_ref": REQUEST.request_ref,
            "tenant": "tenant-a",
            "effect_phase": "unstarted",
        }
        self.held = False
        self.committed: list[tuple[bool, str]] = []
        self.marked = 0
        self.lose_marker = False

    def get(self, item_id: str) -> dict[str, Any]:
        assert item_id == ITEM
        return dict(self.row)

    def claim(self, item_id: str) -> str | None:
        if self.held or self.row["status"] != "ready":
            return None
        self.held = True
        return "claim"

    def require_current(self, item_id: str, claim: str) -> None:
        if not self.held:
            raise WorkItemLeaseLost("lost")

    def mark_effect_started(self, item_id: str, claim: str) -> None:
        if self.lose_marker:
            raise WorkItemLeaseLost("marker refused")
        self.marked += 1
        self.row["effect_phase"] = "started"

    def commit(self, item_id: str, claim: str, *, succeeded: bool, ref: str) -> None:
        self.committed.append((succeeded, ref))
        self.row["status"] = "succeeded" if succeeded else "failed"
        self.row["result_ref" if succeeded else "error_ref"] = ref
        self.row["effect_phase"] = "resolved"
        self.held = False


class Dispatcher:
    def __init__(self) -> None:
        self.executions = 0
        self.crash = False
        self.proven: ToolTaskResult | None = None

    def resolve(self, request_ref: str, **_: Any) -> ToolTaskRequest:
        return REQUEST

    def execute(
        self,
        request: ToolTaskRequest,
        *,
        job_id: str,
        require_current: Callable[[], None],
    ) -> ToolTaskResult:
        self.executions += 1
        if self.crash:
            raise RuntimeError("connector died after the effect")
        return result("completed")

    def lookup(self, job_id: str, *, request: ToolTaskRequest) -> ToolTaskResult | None:
        return self.proven


def result(outcome: str = "completed") -> ToolTaskResult:
    return ToolTaskResult(
        job_id="job-1",
        request_ref=REQUEST.request_ref,
        principal="user:alice",
        outcome=outcome,  # type: ignore[arg-type]
        receipt_ref="receipt:1",
    )


class Queue:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ready(self) -> list[ToolTaskCarrier]:
        return [CARRIER]

    def acknowledge(self, carrier: ToolTaskCarrier) -> None:
        self.acked.append(carrier.job_id)


def build(
    state: dict[str, Any] | None = None, *, fence: Fence | None = None
) -> tuple[ToolTaskConsumer, Fence, Dispatcher, Queue, list[str], list[float]]:
    fence = fence or Fence()
    dispatcher = Dispatcher()
    queue = Queue()
    events: list[str] = []
    now = [1000.0]
    consumer = ToolTaskConsumer(
        queue=queue,
        fence=fence,
        dispatcher=dispatcher,
        read_delegation=lambda ref: state if state is not None else delegation(),
        clock=lambda: now[0],
        emit=lambda event, _fields: events.append(event),
    )
    return consumer, fence, dispatcher, queue, events, now


@pytest.mark.spec("ARBORA-A2A-R002.2.3.3")
def test_active_caller_runs_marks_effect_first_and_acknowledges() -> None:
    consumer, fence, dispatcher, queue, _events, _now = build()
    [delivery] = consumer.run_once()
    assert delivery.run.outcome == "completed"
    assert (fence.marked, dispatcher.executions) == (1, 1)
    assert queue.acked == ["job-1"]


@pytest.mark.spec("ARBORA-A2A-R002.2.3.3")
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (delegation(standing="expired"), CALLER_AUTHORITY_EXPIRED),
        (delegation(standing="revoked"), CALLER_AUTHORITY_REVOKED),
        (delegation(principal_active=False), CALLER_AUTHORITY_REVOKED),
        (delegation(holds_tool_scope=False), CALLER_AUTHORITY_REVOKED),
        (
            delegation(record={**delegation()["record"], "principal": "user:bob"}),
            CALLER_AUTHORITY_REVOKED,
        ),
    ],
)
def test_revoked_or_expired_caller_makes_no_effect_and_is_acknowledged(
    state: dict[str, Any], reason: str
) -> None:
    consumer, fence, dispatcher, queue, _events, _now = build(state)
    [delivery] = consumer.run_once()
    assert delivery.run.outcome == "refused"
    assert delivery.run.detail == reason
    assert (fence.marked, dispatcher.executions) == (0, 0)
    assert fence.committed and fence.committed[0][0] is False
    assert queue.acked == ["job-1"]


@pytest.mark.spec("ARBORA-A2A-R002.2.3.3")
def test_unknown_delegation_refuses_as_revoked() -> None:
    consumer, fence, dispatcher, queue, _e, _n = build()
    consumer._guarded._read = lambda ref: None  # the engine answers DELEGATION_UNKNOWN
    [delivery] = consumer.run_once()
    assert delivery.run.detail == CALLER_AUTHORITY_REVOKED
    assert dispatcher.executions == 0 and queue.acked == ["job-1"]


@pytest.mark.spec("ARBORA-A2A-R002.2.3.4")
def test_marker_refusal_starts_no_effect() -> None:
    fence = Fence()
    fence.lose_marker = True
    consumer, fence, dispatcher, queue, _e, _n = build(fence=fence)
    [delivery] = consumer.run_once()
    assert delivery.run.outcome == "fenced"
    assert dispatcher.executions == 0 and queue.acked == []


@pytest.mark.spec("ARBORA-A2A-R002.2.3.4")
def test_crash_after_effect_reconciles_without_repeat() -> None:
    consumer, fence, dispatcher, queue, events, now = build()
    dispatcher.crash = True
    [first] = consumer.run_once()
    assert first.run.outcome == "indeterminate" and queue.acked == []
    assert fence.row["effect_phase"] == "started"
    fence.held = False  # the lease lapsed
    # Unproven: stays open, announces once per interval, never re-executes.
    [second] = consumer.run_once()
    assert second.run.outcome == "indeterminate"
    fence.held = False
    consumer.run_once()
    assert events == ["tool_task_uncertain"]
    now[0] += 31
    fence.held = False
    consumer.run_once()
    assert events == ["tool_task_uncertain"] * 2
    # Proven: commits the connector's outcome and acknowledges.
    dispatcher.proven = result("completed")
    fence.held = False
    [third] = consumer.run_once()
    assert third.run.outcome == "completed"
    assert dispatcher.executions == 1
    assert fence.committed == [(True, "receipt:1")] and queue.acked == ["job-1"]


@pytest.mark.spec("ARBORA-A2A-R002.2.3.4")
def test_foreign_lookup_result_does_not_settle_an_uncertain_item() -> None:
    consumer, fence, dispatcher, queue, _e, _n = build()
    fence.row["effect_phase"] = "started"
    dispatcher.proven = result("completed").model_copy(update={"principal": "user:eve"})
    [delivery] = consumer.run_once()
    assert delivery.run.outcome == "indeterminate"
    assert fence.committed == [] and queue.acked == []


@pytest.mark.spec("ARBORA-A2A-R002.2.3.5")
def test_duplicate_delivery_executes_once() -> None:
    consumer, fence, dispatcher, queue, _e, _n = build()
    consumer.run_once()
    [again] = consumer.run_once()
    assert dispatcher.executions == 1
    assert again.run.outcome == "skipped" and again.run.detail == "receipt:1"
    assert queue.acked == ["job-1", "job-1"]


@pytest.mark.spec("ARBORA-A2A-R002.2.3.5")
def test_claimed_elsewhere_is_skipped_not_acknowledged() -> None:
    consumer, fence, dispatcher, queue, _e, _n = build()
    fence.held = True
    [delivery] = consumer.run_once()
    assert delivery.run.outcome == "fenced"
    assert dispatcher.executions == 0 and queue.acked == []


def test_intervals_are_bounded() -> None:
    with pytest.raises(ValueError):
        ToolTaskConsumer(
            queue=Queue(),
            fence=Fence(),
            dispatcher=Dispatcher(),
            read_delegation=lambda ref: None,
            clock=lambda: 0.0,
            poll_interval_s=0.01,
        )
