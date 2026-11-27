"""Hot-path proof: confirm admits or rejects a held item (ARBORA-A2A-R006.8).

``POST /a2a`` ``arbora.plan/confirm`` reaches the held-item admitter through the
production ``ask_decide`` seam against a loopback ``Decide`` engine. An admitted item is
applied once; a rejected one stays held; operator elevation is never called.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.a2a._confirm_served import Served
from tests.a2a.test_tool_action_admission_e2e import served  # noqa: F401

pytestmark = pytest.mark.spec("ARBORA-A2A-R006.8")

DIGEST = "b" * 64


def _answer(item: str, outcome: str, **over: Any) -> dict[str, Any]:
    value = {
        "outcome": outcome,
        "verdict_ref": f"rec:{item}",
        "verification_ref": f"ver:{item}",
        "verifier": "verifier",
        "compiler": "compiler",
        "reason": None,
    }
    value.update(over)
    return {"type": "held_item_admission", "value": value}


def _params(
    task: str,
    item: str,
    signature: str,
    kind: str = "run_output_proposal",
    ref: str = "plan:one",
) -> dict[str, Any]:
    return {
        "task_id": task,
        "pending_call_id": f"call:{item}",
        "plan_ref": ref,
        "signature": signature,
        "confirmation_kind": "held_item",
        "item_kind": kind,
        "item_ref": item,
        "content_digest": DIGEST,
        "engine_decision_ref": f"dec:{item}",
    }


@pytest.fixture(autouse=True)
def no_operator_elevation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Operator elevation is a different authority surface: fail loudly if reached."""
    import arbora.api.ops.access_service as access

    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        calls.append("approve_elevation")
        raise AssertionError("confirm must never call operator elevation")

    monkeypatch.setattr(access, "approve_elevation", forbidden, raising=False)
    return calls


def _pend(served: Served, task: str, item: str) -> str:  # noqa: F811
    return asyncio.run(served.pend(task, f"call:{item}"))


def test_confirm_admits_or_rejects_held_item(
    served: Served,  # noqa: F811
    no_operator_elevation: list[str],
) -> None:
    approver = served.caller()
    served.engine.answers["prog"] = _answer("prog", "admitted")
    served.engine.answers["out"] = _answer("out", "rejected", reason="DIGEST_MISMATCH")

    sig = _pend(served, "task:a", "prog")
    admitted = served.post(
        approver, _params("task:a", "prog", sig, "stored_run_program")
    )
    assert admitted.status_code == 200
    body = admitted.json()["result"]
    assert body["admission"]["outcome"] == "admitted"
    assert body["admission"]["verdictRef"] == "rec:prog"
    assert served.effects == ["call:prog"]

    sig = _pend(served, "task:b", "out")
    rejected = served.post(approver, _params("task:b", "out", sig))
    assert rejected.status_code == 403
    assert rejected.json()["error"]["data"]["reason"] == "DIGEST_MISMATCH"
    assert served.effects == ["call:prog"]  # the rejected item stays held

    # One reservation and one outcome entry per call, linked by the verdict ref.
    linked = [e for e in served.audit.entries if e.get("item_ref") in {"prog", "out"}]
    assert {e["verdict_ref"] for e in linked if e["verdict_ref"]} == {
        "rec:prog",
        "rec:out",
    }
    assert any(e.get("reason") == "DIGEST_MISMATCH" for e in linked)
    assert no_operator_elevation == []


def test_replay_changed_plan_wrong_human_expiry_and_producer_apply_nothing(
    served: Served,  # noqa: F811
) -> None:
    approver = served.caller()
    served.engine.answers["item"] = _answer("item", "admitted")
    sig = _pend(served, "task:c", "item")

    assert (
        served.post(
            served.caller("person:other"), _params("task:c", "item", sig)
        ).status_code
        == 403
    )
    assert (
        served.post(
            approver, _params("task:c", "item", sig, ref="plan:other")
        ).status_code
        == 403
    )
    served.keys.down = True
    assert served.post(approver, _params("task:c", "item", sig)).status_code == 403
    served.keys.down = False
    assert served.effects == [] and served.engine.requests == []

    first = served.post(approver, _params("task:c", "item", sig))
    assert first.status_code == 200 and served.effects == ["call:item"]
    replay = served.post(approver, _params("task:c", "item", sig))
    assert replay.status_code == 403 and served.effects == ["call:item"]

    served.engine.answers["late"] = _answer("late", "admitted")
    late = _pend(served, "task:d", "late")
    served.clock[0] += 10**12  # past the grant's fixed expiry
    expired = served.post(approver, _params("task:d", "late", late))
    assert expired.status_code == 403 and served.effects == ["call:item"]


def test_a_foreign_tenant_cannot_confirm_the_item(served: Served) -> None:  # noqa: F811
    served.engine.answers["item"] = _answer("item", "admitted")
    sig = _pend(served, "task:e", "item")
    foreign = served.caller(tenant="tenant:other")
    assert served.post(foreign, _params("task:e", "item", sig)).status_code == 403
    assert served.effects == [] and served.engine.requests == []
