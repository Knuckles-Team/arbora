"""Held-item admission (ARBORA-A2A-R006.3 to R006.7)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.a2a.held_item_admission import (
    HeldItemAdmissionRequest,
    HeldItemAdmitter,
    HeldItemRefusal,
    HeldItemVerdict,
)
from arbora.a2a.models import parse_confirmation
from arbora.a2a.tool_action_admission import ConfirmationKindInvalid

DIGEST = "b" * 64


def _raw(**over: Any) -> dict[str, Any]:
    return {
        "task_id": "t",
        "pending_call_id": "c",
        "plan_ref": "p",
        "signature": "s",
        "confirmation_kind": "held_item",
        "item_kind": "stored_run_program",
        "item_ref": "item:1",
        "content_digest": DIGEST,
        "engine_decision_ref": "dec:1",
    } | over


def _request(**over: Any) -> HeldItemAdmissionRequest:
    parsed = parse_confirmation(_raw(**over))
    assert isinstance(parsed, HeldItemAdmissionRequest)
    return parsed


class Scripted:
    def __init__(self, answer: Mapping[str, Any] | None, error: bool = False) -> None:
        self.answer, self.error = answer, error

    async def decide(self, request: Any, *, tenant: str) -> Any:
        if self.error:
            raise RuntimeError("down")
        return self.answer


class Audit:
    def __init__(self) -> None:
        self.entries: list[Mapping[str, Any]] = []

    async def __call__(self, entry: Mapping[str, Any]) -> None:
        self.entries.append(entry)


GOOD = {
    "outcome": "admitted",
    "verdict_ref": "v:1",
    "verification_ref": "ver:1",
    "verifier": "verifier-a",
    "compiler": "compiler-b",
}


def _admit(answer: Any, audit: Audit | None = None, **over: Any) -> Any:
    admitter = HeldItemAdmitter(Scripted(answer), audit or Audit())
    return asyncio.run(admitter.admit(_request(**over), tenant="t"))


@pytest.mark.spec("ARBORA-A2A-R006.3")
def test_models_are_closed_and_frozen() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        request.item_ref = "x"
    with pytest.raises(ValidationError):
        _request(item_kind="other")
    with pytest.raises(ValidationError):
        _request(content_digest="short")
    with pytest.raises(ValidationError):
        HeldItemVerdict.model_validate({"outcome": "maybe", "verdict_ref": "v"})
    with pytest.raises(ConfirmationKindInvalid):
        parse_confirmation(_raw(action_class="egress"))


@pytest.mark.spec("ARBORA-A2A-R006.4")
def test_program_needs_distinct_verifier_and_required_passes() -> None:
    assert _admit(GOOD).outcome == "admitted"
    for answer, reason in (
        ({**GOOD, "verifier": "compiler-b"}, "VERIFIER_NOT_INDEPENDENT"),
        ({**GOOD, "verification_ref": None}, "POLICY_EVIDENCE_MISSING"),
        (None, "POLICY_EVIDENCE_MISSING"),
    ):
        with pytest.raises(HeldItemRefusal) as caught:
            _admit(answer)
        assert caught.value.reason == reason
    # A run output needs no verifier.
    assert (
        _admit(
            {"outcome": "admitted", "verdict_ref": "v:2"},
            item_kind="run_output_proposal",
        ).verdict_ref
        == "v:2"
    )


@pytest.mark.spec("ARBORA-A2A-R006.6")
def test_each_refusal_leaves_item_held() -> None:
    for answer, reason in (
        ({"outcome": "rejected", "verdict_ref": "v", "reason": "RETIRED"}, "RETIRED"),
        (
            {"outcome": "rejected", "verdict_ref": "v", "reason": "DIGEST_MISMATCH"},
            "DIGEST_MISMATCH",
        ),
        ({"outcome": "abstained", "verdict_ref": "v"}, "ABSTAINED"),
        (
            {
                "outcome": "abstained",
                "verdict_ref": "v",
                "reason": "PASSES_INSUFFICIENT",
            },
            "PASSES_INSUFFICIENT",
        ),
    ):
        with pytest.raises(HeldItemRefusal) as caught:
            _admit(answer)
        assert caught.value.reason == reason
    with pytest.raises(HeldItemRefusal) as caught:
        asyncio.run(
            HeldItemAdmitter(Scripted(None, error=True), Audit()).admit(
                _request(), tenant="t"
            )
        )
    assert caught.value.reason == "ENGINE_UNAVAILABLE"


@pytest.mark.spec("ARBORA-A2A-R006.7")
def test_audit_entry_names_verdict_and_item() -> None:
    audit = Audit()
    _admit(GOOD, audit)
    assert audit.entries[-1] == {
        "verdict_ref": "v:1",
        "item_kind": "stored_run_program",
        "item_ref": "item:1",
        "content_digest": DIGEST,
        "verification_ref": "ver:1",
    }
    refused = Audit()
    with pytest.raises(HeldItemRefusal):
        _admit(
            {"outcome": "rejected", "verdict_ref": "v", "reason": "RETIRED"}, refused
        )
    assert refused.entries[-1]["reason"] == "RETIRED"
    assert set(refused.entries[-1]) <= {
        "verdict_ref",
        "item_kind",
        "item_ref",
        "content_digest",
        "verification_ref",
        "reason",
    }


@pytest.mark.spec("ARBORA-A2A-R006.5")
def test_confirm_dispatch_reaches_held_item_admitter() -> None:
    """The real route's ``held_item`` branch runs the admitter exactly once."""
    from tests.a2a._confirm_served import Served

    served = Served()
    ran: list[str] = []
    admitter = served.chain.held_items
    assert admitter is not None
    original = admitter.admit

    async def counted(request: Any, *, tenant: str) -> Any:
        ran.append(request.item_ref)
        return await original(request, tenant=tenant)

    object.__setattr__(admitter, "admit", counted)
    served.engine.answers["item"] = {
        "type": "held_item_admission",
        "value": {
            "outcome": "admitted",
            "verdict_ref": "rec:item",
            "verification_ref": "ver:item",
            "verifier": "v",
            "compiler": "c",
            "reason": None,
        },
    }
    import asyncio

    from medulla.generated import models

    from tests.a2a.test_engine_deciders import _HeldFamily

    patch = pytest.MonkeyPatch()
    try:
        for name, value in {
            "ToolActionAnswer": object,
            "HeldItemAnswer": object,
            "FamilyAnswerHeldItemAdmission": _HeldFamily,
            "FamilyAnswerToolActionAdmission": _HeldFamily,
        }.items():
            patch.setattr(models, name, value, raising=False)
        signature = asyncio.run(served.pend("task:r", "call:item"))
        response = served.post(
            served.caller(),
            {
                "task_id": "task:r",
                "pending_call_id": "call:item",
                "plan_ref": "plan:one",
                "signature": signature,
                "confirmation_kind": "held_item",
                "item_kind": "run_output_proposal",
                "item_ref": "item",
                "content_digest": "b" * 64,
                "engine_decision_ref": "dec:item",
            },
        )
    finally:
        patch.undo()
    assert response.status_code == 200
    assert ran == ["item"]
