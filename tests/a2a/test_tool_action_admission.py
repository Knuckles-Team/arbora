"""Tool-action admission (ARBORA-A2A-R005.3 to R005.7)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.a2a.models import parse_confirmation
from arbora.a2a.tool_action_admission import (
    ConfirmationKindInvalid,
    ToolActionAdmissionRequest,
    ToolActionAdmitter,
    ToolActionApproval,
    ToolActionRefusal,
)

DIGEST = "a" * 64


def _raw(**over: Any) -> dict[str, Any]:
    return {
        "task_id": "t",
        "pending_call_id": "c",
        "plan_ref": "p",
        "signature": "s",
        "confirmation_kind": "tool_action",
        "params_digest": DIGEST,
        "trust_mark_ref": "mark:1",
        "action_class": "egress",
    } | over


class Scripted:
    def __init__(self, answer: Mapping[str, Any] | None, error: bool = False) -> None:
        self.answer, self.error, self.calls = answer, error, 0

    async def decide(self, request: Any, *, tenant: str) -> Any:
        self.calls += 1
        if self.error:
            raise RuntimeError("engine down")
        return self.answer


class Audit:
    def __init__(self, fail: bool = False) -> None:
        self.entries: list[Mapping[str, Any]] = []
        self.fail = fail

    async def __call__(self, entry: Mapping[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.entries.append(entry)


def _request(**over: Any) -> ToolActionAdmissionRequest:
    parsed = parse_confirmation(_raw(**over))
    assert isinstance(parsed, ToolActionAdmissionRequest)
    return parsed


ADMIT = {"verdict": "admit", "approval_ref": "ap:1", "decision_ref": "de:1"}


@pytest.mark.spec("ARBORA-A2A-R005.3")
def test_models_are_closed_and_frozen() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        request.action_class = "delegation"
    with pytest.raises(ValidationError):
        ToolActionApproval.model_validate(
            {"verdict": "maybe", "approval_ref": "a", "decision_ref": "d"}
        )
    for bad in (
        {"confirmation_kind": None},
        {"confirmation_kind": "other"},
        {"held_item_only": 1},
    ):
        raw = _raw(**bad)
        if bad.get("confirmation_kind", "x") is None:
            raw.pop("confirmation_kind")
        with pytest.raises(ConfirmationKindInvalid):
            parse_confirmation(raw)
    with pytest.raises(ValidationError):
        _request(params_digest="A" * 64)
    with pytest.raises(ValidationError):
        _request(action_class="shell")


@pytest.mark.spec("ARBORA-A2A-R005.4")
def test_admit_returns_engine_verdict_unchanged() -> None:
    decider = Scripted({**ADMIT, "verdict": "deny"})
    approval = asyncio.run(
        ToolActionAdmitter(decider, Audit()).admit(_request(), tenant="t")
    )
    assert approval.verdict == "deny" and approval.approval_ref == "ap:1"
    assert decider.calls == 1


@pytest.mark.spec("ARBORA-A2A-R005.4")
def test_default_applies_only_without_verdict() -> None:
    absent = asyncio.run(
        ToolActionAdmitter(Scripted(None), Audit()).admit(_request(), tenant="t")
    )
    assert absent.verdict == "deny" and absent.approval_ref == "deny_without_verdict"
    given = asyncio.run(
        ToolActionAdmitter(Scripted(ADMIT), Audit()).admit(_request(), tenant="t")
    )
    assert given.verdict == "admit"
    with pytest.raises(ValueError):
        ToolActionAdmitter(Scripted(ADMIT), Audit(), timeout_s=61)


@pytest.mark.spec("ARBORA-A2A-R005.6")
def test_each_refusal_reason_denies() -> None:
    cases = {
        "ENGINE_UNAVAILABLE": (Scripted(None, error=True), Audit(), {}),
        "ABSTAINED": (Scripted({"verdict": "abstain"}), Audit(), {}),
        "TRUST_MARK_MISSING": (Scripted(ADMIT), Audit(), {"trust_mark_ref": " "}),
        "AUDIT_UNAVAILABLE": (Scripted(ADMIT), Audit(fail=True), {}),
    }
    for reason, (decider, audit, over) in cases.items():
        with pytest.raises(ToolActionRefusal) as caught:
            asyncio.run(
                ToolActionAdmitter(decider, audit).admit(_request(**over), tenant="t")
            )
        assert caught.value.reason == reason
    # A failed reservation precedes the engine call: the engine is never asked.
    decider = Scripted(ADMIT)
    with pytest.raises(ToolActionRefusal):
        asyncio.run(
            ToolActionAdmitter(decider, Audit(fail=True)).admit(_request(), tenant="t")
        )
    assert decider.calls == 0


@pytest.mark.spec("ARBORA-A2A-R005.7")
def test_audit_entry_names_decision_without_values() -> None:
    audit = Audit()
    asyncio.run(
        ToolActionAdmitter(Scripted(ADMIT), audit).admit(_request(), tenant="t")
    )
    final = audit.entries[-1]
    assert final == {
        "approval_ref": "ap:1",
        "decision_ref": "de:1",
        "pending_call_id": "c",
        "action_class": "egress",
        "params_digest": DIGEST,
    }
    assert len(audit.entries) == 2 and audit.entries[0]["approval_ref"] == ""
