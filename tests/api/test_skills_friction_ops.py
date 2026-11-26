"""Skill friction operations (ARBORA-OPS-R952)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import skills_friction
from tests.api._knowledge_support import NO_OP, caller_context, record_engine
from tests.test_served_operation_api import _call, served  # noqa: F401

REPORT = {
    "report_id": "rep1",
    "skill_ref": "skill:x@1",
    "skill_version": "1",
    "run_id": "r1",
    "tried": "a",
    "expected": "b",
    "happened": "c",
    "proposed_fix": None,
    "at_ms": 7,
}


@pytest.mark.spec("ARBORA-OPS-R952")
async def test_report_names_the_verified_caller_and_group_reads_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = record_engine(monkeypatch, skills_friction, {"ok": True})
    context = caller_context("knowledge:flag", "knowledge:curate")
    await skills_friction.handle_friction_report(context, REPORT, NO_OP)
    await skills_friction.handle_friction_group(
        context, {"skill_ref": "skill:x@1", "limit": None}, NO_OP
    )
    await skills_friction.handle_friction_get(context, {"group_ref": "g"}, NO_OP)
    report = sent[0][1]["report"]
    assert report["tenant_id"] == "tenant:one"
    assert report["reported_by"] == "person:one"
    assert "proposed_fix" not in report
    assert sent[1][1] == {"op": "group", "skill_ref": "skill:x@1"}
    assert sent[2][1] == {"op": "get", "group_ref": "g"}


@pytest.mark.spec("ARBORA-OPS-R952")
async def test_approving_needs_its_own_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = record_engine(monkeypatch, skills_friction, {"ok": True})
    move = {"group_ref": "g", "to": "approved", "evidence_ref": None, "at_ms": 3}
    with pytest.raises(OperationRefused) as denied:
        await skills_friction.handle_friction_transition(
            caller_context("knowledge:curate"), move, NO_OP
        )
    assert denied.value.code == "POLICY_DENIED"
    assert sent == []
    await skills_friction.handle_friction_transition(
        caller_context("knowledge:curate", "knowledge:approve"), move, NO_OP
    )
    await skills_friction.handle_friction_transition(
        caller_context("knowledge:curate"), {**move, "to": "triaged"}, NO_OP
    )
    assert sent[0][1] == {
        "op": "transition",
        "transition": {"group_ref": "g", "to": "approved"},
        "at_ms": 3,
    }
    assert sent[1][1]["transition"]["to"] == "triaged"


@pytest.mark.spec("ARBORA-OPS-R952")
def test_friction_operations_are_served_unavailable_until_the_scope_is_pinned(
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    reply = _call(app, "skills.friction.get", {"group_ref": "g"})
    assert reply.status_code == 503, reply.text
    assert reply.json()["error"]["code"] == "UNAVAILABLE"
