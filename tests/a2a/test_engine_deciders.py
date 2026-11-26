"""The engine deciders over the typed ``Decide`` answers (ARBORA-A2A-R005.4, R006.4).

The refusal tests run under the pinned engine, whose generated models lack the typed
answers and the claim identity digests. The real-path tests install structural doubles
of engine ``main``'s models and drive the deciders and the admitters through them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.chain_composition import compose_approval_chain
from arbora.a2a.engine_deciders import EngineHeldItemDecider, EngineToolActionDecider
from arbora.a2a.held_item_admission import HeldItemAdmitter, HeldItemRefusal
from arbora.a2a.tool_action_admission import ToolActionAdmitter, ToolActionRefusal
from arbora.pin_lag import medulla as guard


def _pinned_has_answers() -> bool:
    from medulla.generated import models

    return all(hasattr(models, name) for name in guard.ADMISSION_MODELS)


class _Parsed:
    """Structural double of a generated family answer: ``model_validate`` then ``value``."""

    inner: type

    def __init__(self, value: Any) -> None:
        self.value = value

    @classmethod
    def model_validate(cls, raw: Any) -> _Parsed:
        if raw["type"] != cls.kind:  # type: ignore[attr-defined]
            raise ValueError("wrong family answer")
        return cls(SimpleNamespace(**raw["value"]))


class _ToolFamily(_Parsed):
    kind = "tool_action_admission"


class _HeldFamily(_Parsed):
    kind = "held_item_admission"


@pytest.fixture
def main_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from medulla.generated import models

    for name, value in {
        "ToolActionAnswer": object,
        "HeldItemAnswer": object,
        "FamilyAnswerToolActionAdmission": _ToolFamily,
        "FamilyAnswerHeldItemAdmission": _HeldFamily,
    }.items():
        monkeypatch.setattr(models, name, value, raising=False)


class _Ask:
    def __init__(self, answer: Any) -> None:
        self.answer, self.calls = answer, []

    async def __call__(self, kind: str, params: Any, tenant: str) -> Any:
        self.calls.append((kind, dict(params), tenant))
        return self.answer


TOOL = SimpleNamespace(
    pending_call_id="call-1",
    params_digest="a" * 64,
    trust_mark_ref="node:1",
    action_class="egress",
)
HELD = SimpleNamespace(
    item_kind="run_output_proposal",
    item_ref="item-1",
    content_digest="b" * 64,
    engine_decision_ref="dec-1",
)


@pytest.mark.spec("ARBORA-A2A-R005.4")
@pytest.mark.spec("ARBORA-A2A-R006.4")
@pytest.mark.skipif(_pinned_has_answers(), reason="the pin carries the typed answers")
def test_pinned_engine_refuses_both_deciders_naming_the_missing_answer() -> None:
    ask = _Ask({"answer": {}})
    for decider, request in (
        (EngineToolActionDecider(ask), TOOL),
        (EngineHeldItemDecider(ask), HELD),
    ):
        with pytest.raises(guard.PinLagUnavailable) as refused:
            asyncio.run(decider.decide(request, tenant="t"))
        assert refused.value.code == "UNAVAILABLE"
        assert refused.value.missing == "ToolActionAnswer"
    assert ask.calls == []


@pytest.mark.spec("ARBORA-OPS-R890")
def test_pinned_claim_without_identity_digests_refuses_naming_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    class Old:
        model_fields = {"capability_digest": None}

    monkeypatch.setattr(models, "ClaimWorkItemResult", Old, raising=False)
    with pytest.raises(guard.PinLagUnavailable) as refused:
        guard.require_claim_identity()
    assert refused.value.missing == "ClaimWorkItemResult.catalog_digest"

    class Current:
        model_fields = dict.fromkeys(guard.CLAIM_IDENTITY_FIELDS)

    monkeypatch.setattr(models, "ClaimWorkItemResult", Current, raising=False)
    guard.require_claim_identity()


@pytest.mark.spec("ARBORA-A2A-R005.4")
def test_tool_action_verdict_is_read_from_the_typed_answer(main_shape: None) -> None:
    deny = _Ask(
        {
            "answer": {
                "type": "tool_action_admission",
                "value": {
                    "verdict": "deny",
                    "approval_ref": "call-1",
                    "decision_ref": "rec-9",
                },
            }
        }
    )
    decider = EngineToolActionDecider(deny)
    assert asyncio.run(decider.decide(TOOL, tenant="t")) == {
        "verdict": "deny",
        "approval_ref": "call-1",
        "decision_ref": "rec-9",
    }
    assert deny.calls == [
        (
            "tool_action_admission",
            {
                "pending_call_id": "call-1",
                "params_digest": "a" * 64,
                "trust_mark_ref": "node:1",
                "action_class": "egress",
            },
            "t",
        )
    ]
    abstain = _Ask(
        {
            "answer": {
                "type": "tool_action_admission",
                "value": {
                    "verdict": "abstain",
                    "approval_ref": "call-1",
                    "decision_ref": "rec-9",
                    "reason": "TRUST_MARK_MISSING",
                },
            }
        }
    )
    assert asyncio.run(EngineToolActionDecider(abstain).decide(TOOL, tenant="t")) == {
        "verdict": "abstain"
    }
    assert (
        asyncio.run(EngineToolActionDecider(_Ask(None)).decide(TOOL, tenant="t"))
        is None
    )
    wrong = _Ask({"answer": {"type": "held_item_admission", "value": {}}})
    with pytest.raises(ValueError):
        asyncio.run(EngineToolActionDecider(wrong).decide(TOOL, tenant="t"))


@pytest.mark.spec("ARBORA-A2A-R006.4")
def test_held_item_verdict_is_read_from_the_typed_answer(main_shape: None) -> None:
    ask = _Ask(
        {
            "answer": {
                "type": "held_item_admission",
                "value": {
                    "outcome": "admitted",
                    "verdict_ref": "rec-3",
                    "verification_ref": "ver-1",
                    "verifier": "v",
                    "compiler": "c",
                    "reason": None,
                },
            }
        }
    )
    assert asyncio.run(EngineHeldItemDecider(ask).decide(HELD, tenant="t")) == {
        "outcome": "admitted",
        "verdict_ref": "rec-3",
        "verification_ref": "ver-1",
        "verifier": "v",
        "compiler": "c",
    }
    assert ask.calls[0][0] == "held_item_admission"
    assert ask.calls[0][1]["engine_decision_ref"] == "dec-1"


@pytest.mark.spec("ARBORA-A2A-R005.4")
@pytest.mark.spec("ARBORA-A2A-R006.4")
def test_the_chain_binds_both_admitters_from_one_decide_seam(main_shape: None) -> None:
    class Adapter:
        async def preflight(self, event: Any, audit_class: Any, caller: Any) -> str:
            return "ref"

    async def client() -> Any:
        return object()

    def build(**kwargs: Any) -> Any:
        return compose_approval_chain(
            leases=SimpleNamespace(control_leases=SimpleNamespace()),
            client=client,
            graph="g",
            tenant="t",
            audit_adapter=Adapter(),
            current_session=lambda: None,
            **kwargs,
        )

    bare = build()
    assert bare.tool_actions is None and bare.held_items is None
    bound = build(ask_decide=_Ask(None))
    assert isinstance(bound.tool_actions, ToolActionAdmitter)
    assert isinstance(bound.held_items, HeldItemAdmitter)


@pytest.mark.spec("ARBORA-A2A-R005.6")
@pytest.mark.spec("ARBORA-A2A-R006.6")
def test_an_unreadable_engine_answer_refuses_closed_not_approved(
    main_shape: None,
) -> None:
    async def audit(entry: Any) -> None:
        return None

    bad = _Ask({"answer": {"type": "other", "value": {}}})
    request = SimpleNamespace(**TOOL.__dict__)
    request.confirmation_kind = "tool_action"
    with pytest.raises(ToolActionRefusal) as tool:
        asyncio.run(
            ToolActionAdmitter(EngineToolActionDecider(bad), audit).admit(
                request, tenant="t"
            )
        )
    assert str(tool.value) == "ENGINE_UNAVAILABLE" or "ENGINE_UNAVAILABLE" in repr(
        tool.value
    )
    held = SimpleNamespace(**HELD.__dict__)
    with pytest.raises(HeldItemRefusal) as item:
        asyncio.run(
            HeldItemAdmitter(EngineHeldItemDecider(bad), audit).admit(held, tenant="t")
        )
    assert (
        "ENGINE_UNAVAILABLE" in repr(item.value)
        or str(item.value) == "ENGINE_UNAVAILABLE"
    )
