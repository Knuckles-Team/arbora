"""Run admission decided through the engine policy port, over the served app.

Row ARBORA-OPS-R050.3: ``agents.plan_run`` reaches ``_admit_native_plan_run``
through the real HTTP app and invoke pipeline, and the composed
``RunPolicyDecision`` port decides it. The port is the recorded engine answer;
the served app, authentication, invoke pipeline, ``policy_guard`` and the
native admission are real. The native WorkItem layer is recorded.
"""

# ruff: noqa: F811

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.api._loopback_engine import Loopback, loopback  # noqa: E402,F401
from tests.api.test_plan_run_ops import (  # noqa: E402
    BINDING_PLAN,
    TOOL_PIN,
    WriteGateway,
    _json_model,
)
from tests.api.test_plan_run_served import ServedToolEngine  # noqa: E402
from tests.test_served_operation_api import served  # noqa: E402,F401

OP = "agents.plan_run"
ITEM = "work-item:" + "a" * 64


class RecordingPolicy:
    """The composed run-policy port: records each subject and answers or denies."""

    def __init__(self) -> None:
        self.subjects: list[Any] = []
        self.raises: Exception | None = None

    async def decide(self, subject: Any, *, now: int) -> Any:
        self.subjects.append(subject)
        if self.raises is not None:
            raise self.raises
        return ITEM


POLICY = RecordingPolicy()


def loopback_bindings() -> dict[str, Any]:
    return {
        "task_plan_engine": ServedToolEngine(),
        "task_plan_model": _json_model({"emit": '{"title": "T1"}'}),
        "task_plan_feature_schema": TOOL_PIN,
        "task_plan_work_engine": SimpleNamespace(graph_name="tenant:one"),
        "fleet_gateway": WriteGateway(),
        "run_policy": POLICY,
    }


@pytest.fixture(autouse=True)
def _reset() -> None:
    POLICY.subjects.clear()
    POLICY.raises = None


def _record_native_layer(monkeypatch: pytest.MonkeyPatch, seen: list[str]) -> None:
    import arbora.work_items as work_items

    def get(_engine: Any, requested: str) -> Any:
        seen.append("get")
        metadata = POLICY.subjects[-1]["metadata"]
        return {
            "kind": "orchestrator_task",
            "status": "succeeded",
            "metadata": metadata,
        }

    monkeypatch.setattr(work_items, "get_work_item", get)
    monkeypatch.setattr(
        work_items, "claim_specific", lambda *a, **k: seen.append("claim")
    )
    monkeypatch.setattr(
        work_items,
        "submit_work_item_atomic",
        lambda *a, **k: pytest.fail("admission bypassed the policy port"),
    )


@pytest.mark.spec("ARBORA-OPS-R050.3")
def test_served_run_is_admitted_through_the_policy_port(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    loopback.serve({})
    seen: list[str] = []
    _record_native_layer(monkeypatch, seen)
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="admit-1")
    assert reply.status_code == 200, reply.text
    value = Loopback.value(reply)
    assert value["task_id"] == ITEM
    assert value["run"]["status"] == "succeeded"
    assert len(POLICY.subjects) == 1
    subject = POLICY.subjects[0]
    assert subject["run_id"].startswith("run-") and subject["work_item_id"] is None
    assert seen == ["get"]


@pytest.mark.spec("ARBORA-OPS-R050.3")
@pytest.mark.parametrize(
    ("code", "status", "engine_code"),
    [
        ("POLICY_DENIED", 403, "SCOPE_EXCEEDS_POLICY"),
        ("POLICY_UNAVAILABLE", 503, None),
    ],
)
def test_served_run_refused_by_the_policy_port_claims_nothing(
    loopback: Loopback,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
    engine_code: str | None,
) -> None:
    from arbora.control_plane.runs import RunPolicyDenied, RunPolicyUnavailable

    loopback.serve({})
    seen: list[str] = []
    _record_native_layer(monkeypatch, seen)
    POLICY.raises = (
        RunPolicyDenied(engine_code)
        if engine_code
        else RunPolicyUnavailable("engine_unreachable")
    )
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="admit-refused")
    assert reply.status_code == status, reply.text
    assert reply.json()["error"]["code"] == code
    assert len(POLICY.subjects) == 1
    assert seen == []
