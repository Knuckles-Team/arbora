"""The production ``ask_decide`` seam binds confirm (ARBORA-A2A-R005.4, R006.4).

The seam builds the engine ``Decide`` request of each admission question and checks the
record's receipt. The admit and deny tests drive it through the engine deciders and the
admitters over a loopback ``Decide`` that answers like the engine family does.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from typing import Any

import pytest

from arbora.a2a.engine_decide import build_ask_decide, decide_request_for
from arbora.a2a.engine_deciders import EngineHeldItemDecider, EngineToolActionDecider
from arbora.a2a.held_item_admission import HeldItemAdmitter, HeldItemRefusal
from arbora.a2a.tool_action_admission import ToolActionAdmitter, ToolActionRefusal
from tests._support import unwrap_decide
from tests.a2a.test_engine_deciders import HELD, main_shape  # noqa: F401

SCHEMA = {"schema_version": 1, "body": "pin"}
PRINCIPAL = "service:arbora"


def _principal_ref(principal: str) -> str:
    return "principal:sha256:" + hashlib.sha256(principal.encode()).hexdigest()


class LoopbackEngine:
    """Answers ``Decide`` as the engine families do: a record bound to its request."""

    def __init__(self, answers: Mapping[str, Any], principal: str = PRINCIPAL) -> None:
        self.answers, self.principal = answers, principal
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        assert method == "Decide"
        params = unwrap_decide(params)
        self.requests.append(params)
        kind = params["question"]["kind"]
        return {
            "records": [
                {
                    "record_id": "rec-1",
                    "record_digest": "d" * 64,
                    "caller_principal": _principal_ref(self.principal),
                    "tenant_id": params["tenant_id"],
                    "question": params["question"],
                    "inputs": {"params": params["params"]},
                    "answer": self.answers[kind],
                }
            ]
        }


def _ask(engine: LoopbackEngine) -> Any:
    return build_ask_decide(
        engine,
        graph="g",
        feature_schema=lambda tenant: SCHEMA,
        principal=lambda: PRINCIPAL,
    )


def _tool_answer(verdict: str) -> dict[str, Any]:
    return {
        "type": "tool_action_admission",
        "value": {
            "verdict": verdict,
            "approval_ref": "call-1",
            "decision_ref": "rec-1",
        },
    }


def _held_answer(outcome: str) -> dict[str, Any]:
    return {
        "type": "held_item_admission",
        "value": {
            "outcome": outcome,
            "verdict_ref": "rec-1",
            "verification_ref": "ver-1",
            "verifier": "v",
            "compiler": "c",
            "reason": None,
        },
    }


@pytest.mark.spec("ARBORA-A2A-R005.4")
@pytest.mark.spec("ARBORA-A2A-R006.4")
def test_request_carries_policy_schema_option_and_typed_params() -> None:
    tool = decide_request_for(
        "tool_action_admission",
        {
            "pending_call_id": "call-1",
            "params_digest": "a" * 64,
            "trust_mark_ref": "node:1",
            "action_class": "egress",
        },
        "t",
        graph="g",
        feature_schema=SCHEMA,
    )
    assert tool["question"]["kind"] == "tool_action_admission"
    assert tool["policy"] == {"policy": "default"}
    assert tool["feature_schema"] == SCHEMA
    assert [o["option_id"] for o in tool["candidates"]["options"]] == ["admission"]
    assert {p["name"]: p["value"] for p in tool["params"]} == {
        "action_class": {"type": "text", "value": "egress"},
        "graph": {"type": "text", "value": "g"},
        "params_digest": {"type": "text", "value": "a" * 64},
        "pending_call_id": {"type": "text", "value": "call-1"},
        "trust_mark_ref": {"type": "text", "value": "node:1"},
    }
    held = decide_request_for(
        "held_item_admission",
        {
            "item_kind": "stored_run_program",
            "item_ref": "i",
            "content_digest": "b" * 64,
        },
        "t",
        graph="g",
        feature_schema=SCHEMA,
    )
    assert {p["name"] for p in held["params"]} == {
        "graph",
        "item_kind",
        "item_ref",
        "content_digest",
    }
    with pytest.raises(ValueError):
        decide_request_for(
            "held_item_admission",
            {"item_ref": "i"},
            "t",
            graph="g",
            feature_schema=SCHEMA,
        )
    with pytest.raises(ValueError):
        decide_request_for("other", {}, "t", graph="g", feature_schema=SCHEMA)


def _tool_request() -> Any:
    from arbora.a2a.tool_action_admission import ToolActionAdmissionRequest

    return ToolActionAdmissionRequest(
        task_id="task-1",
        pending_call_id="call-1",
        plan_ref="plan-1",
        signature="sig",
        confirmation_kind="tool_action",
        params_digest="a" * 64,
        trust_mark_ref="node:1",
        action_class="egress",
    )


class _Audit:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def __call__(self, entry: Any) -> None:
        self.entries.append(entry)


@pytest.mark.spec("ARBORA-A2A-R005.4")
def test_tool_action_admits_and_denies_through_the_production_seam(
    main_shape: None,  # noqa: F811
) -> None:
    engine = LoopbackEngine({"tool_action_admission": _tool_answer("admit")})
    admitter = ToolActionAdmitter(EngineToolActionDecider(_ask(engine)), _Audit())
    typed = _tool_request()
    approved = asyncio.run(admitter.admit(typed, tenant="t"))
    assert approved.approval_ref == "call-1" and len(engine.requests) == 1

    engine.answers = {"tool_action_admission": _tool_answer("deny")}
    denied = asyncio.run(admitter.admit(typed, tenant="t"))
    assert str(getattr(denied.verdict, "value", denied.verdict)) == "deny"


@pytest.mark.spec("ARBORA-A2A-R006.4")
def test_held_item_admits_and_rejects_through_the_production_seam(
    main_shape: None,  # noqa: F811
) -> None:
    from arbora.a2a.held_item_admission import HeldItemAdmissionRequest

    typed = HeldItemAdmissionRequest(
        task_id="task-1",
        pending_call_id="call-1",
        plan_ref="plan-1",
        signature="sig",
        confirmation_kind="held_item",
        item_kind="run_output_proposal",
        item_ref=HELD.item_ref,
        content_digest=HELD.content_digest,
        engine_decision_ref="dec-1",
    )
    engine = LoopbackEngine({"held_item_admission": _held_answer("admitted")})
    admitter = HeldItemAdmitter(EngineHeldItemDecider(_ask(engine)), _Audit())
    admitted = asyncio.run(admitter.admit(typed, tenant="t"))
    assert str(getattr(admitted.outcome, "value", admitted.outcome)) == "admitted"
    engine.answers = {"held_item_admission": _held_answer("rejected")}
    with pytest.raises(HeldItemRefusal):
        asyncio.run(admitter.admit(typed, tenant="t"))


@pytest.mark.spec("ARBORA-A2A-R005.6")
def test_a_record_for_another_caller_or_a_missing_pin_refuses_closed(
    main_shape: None,  # noqa: F811
) -> None:
    typed = _tool_request()
    foreign = LoopbackEngine(
        {"tool_action_admission": _tool_answer("admit")}, principal="service:other"
    )
    admitter = ToolActionAdmitter(EngineToolActionDecider(_ask(foreign)), _Audit())
    with pytest.raises(ToolActionRefusal) as refused:
        asyncio.run(admitter.admit(typed, tenant="t"))
    assert refused.value.reason == "ENGINE_UNAVAILABLE"

    def no_pin(tenant: str) -> Mapping[str, Any]:
        raise RuntimeError("no published pin")

    engine = LoopbackEngine({"tool_action_admission": _tool_answer("admit")})
    unpinned = build_ask_decide(
        engine, graph="g", feature_schema=no_pin, principal=lambda: PRINCIPAL
    )
    admitter = ToolActionAdmitter(EngineToolActionDecider(unpinned), _Audit())
    with pytest.raises(ToolActionRefusal):
        asyncio.run(admitter.admit(typed, tenant="t"))
    assert engine.requests == []


@pytest.mark.spec("ARBORA-A2A-R005.4")
@pytest.mark.spec("ARBORA-A2A-R006.4")
def test_served_composition_binds_both_admitters_behind_the_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from tests.test_served_notices_composition import NoticeClient, _compose

    invocation = _compose(NoticeClient(), monkeypatch)
    chain = invocation.approval_chain
    assert isinstance(chain.tool_actions, ToolActionAdmitter)
    assert isinstance(chain.held_items, HeldItemAdmitter)
    # No feature-schema pin is published in this composition: the question is refused
    # before any engine call, so confirm refuses closed rather than guessing a verdict.
    ask = chain.tool_actions._decider._ask  # noqa: SLF001
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(
            ask(
                "tool_action_admission",
                {
                    "pending_call_id": "call-1",
                    "params_digest": "a" * 64,
                    "trust_mark_ref": "node:1",
                    "action_class": "egress",
                },
                "tenant:one",
            )
        )
    assert refused.value.code == "UNAVAILABLE"
