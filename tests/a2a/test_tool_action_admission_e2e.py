"""Hot-path proof: confirm returns the engine verdict with one audit entry.

ARBORA-A2A-R005.8. ``POST /a2a`` ``arbora.plan/confirm`` reaches the tool-action admitter
through the production ``ask_decide`` seam against a loopback ``Decide`` engine. A
refusal, a deny and an abstain never let the action proceed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.a2a._confirm_served import PARAMS, Served

pytestmark = pytest.mark.spec("ARBORA-A2A-R005.8")


def _answer(call: str, verdict: str) -> dict[str, Any]:
    return {
        "type": "tool_action_admission",
        "value": {
            "verdict": verdict,
            "approval_ref": call,
            "decision_ref": f"rec:{call}",
        },
    }


def _params(task: str, call: str, signature: str, action_class: str) -> dict[str, Any]:
    return {
        "task_id": task,
        "pending_call_id": call,
        "plan_ref": "plan:one",
        "signature": signature,
        "confirmation_kind": "tool_action",
        "params_digest": PARAMS,
        "trust_mark_ref": "node:mark",
        "action_class": action_class,
    }


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    from medulla.generated import models

    from tests.a2a.test_engine_deciders import _HeldFamily, _ToolFamily

    for name, value in {
        "ToolActionAnswer": object,
        "HeldItemAnswer": object,
        "FamilyAnswerToolActionAdmission": _ToolFamily,
        "FamilyAnswerHeldItemAdmission": _HeldFamily,
    }.items():
        monkeypatch.setattr(models, name, value, raising=False)
    return Served()


def test_confirm_returns_engine_verdict_and_audit(served: Served) -> None:
    approver = served.caller()
    cases = [
        ("egress", "admit"),
        ("privileged_call", "deny"),
        ("delegation", "abstain"),
    ]
    for index, (action_class, verdict) in enumerate(cases):
        call = f"call:{action_class}"
        served.engine.answers[call] = _answer(call, verdict)
        signature = asyncio.run(served.pend(f"task:{index}", call))
        response = served.post(
            approver, _params(f"task:{index}", call, signature, action_class)
        )
        if verdict == "abstain":
            assert response.status_code == 403
            assert response.json()["error"]["data"]["reason"] == "ABSTAINED"
        else:
            assert response.status_code == 200
            admission = response.json()["result"]["admission"]
            assert admission["verdict"] == verdict
            assert admission["decisionRef"] == f"rec:{call}"
    # The effect ran for the admitted call only; deny and abstain never proceed.
    assert served.effects == ["call:egress"]
    # Each call asked the engine once, with the action class it carried.
    classes = [
        {p["name"]: p["value"]["value"] for p in r["params"]}["action_class"]
        for r in served.engine.requests
    ]
    assert classes == ["egress", "privileged_call", "delegation"]
    # One linked audit entry per call: the consumed grant, then the reservation.
    consumed = [e for e in served.audit.entries if e.get("outcome") == "consumed"]
    assert len(consumed) == 3
    decisions = [
        e for e in served.audit.entries if "decision_ref" in e and e["decision_ref"]
    ]
    assert {e["decision_ref"] for e in decisions} == {
        "rec:call:egress",
        "rec:call:privileged_call",
    }


def test_a_foreign_tenant_is_refused_before_the_engine_is_asked(served: Served) -> None:
    call = "call:foreign"
    served.engine.answers[call] = _answer(call, "admit")
    signature = asyncio.run(served.pend("task:foreign", call))
    foreign = served.caller(tenant="tenant:other")
    response = served.post(foreign, _params("task:foreign", call, signature, "egress"))
    assert response.status_code == 403
    assert served.engine.requests == [] and served.effects == []
