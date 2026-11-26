"""Skill evaluation, ablation, canary and tier operations (ARBORA-OPS-R955 to R957)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.ops import config_layers, skills_eval
from tests.api._knowledge_support import NO_OP, caller_context


def _record(
    monkeypatch: pytest.MonkeyPatch, module: Any, answer: Any = None
) -> list[Any]:
    sent: list[Any] = []

    async def engine_send(
        context: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> Any:
        sent.append((method, params, idempotent))
        if isinstance(answer, Exception):
            raise answer
        return answer or {"ok": True}

    monkeypatch.setattr(module, "engine_send", engine_send)
    return sent


@pytest.mark.spec("ARBORA-OPS-R955")
async def test_case_report_and_calibrate_forward_the_new_eval_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch, skills_eval)
    context = caller_context()
    await skills_eval.handle_case_add(
        context, {"case": {"case_id": "c"}, "known_tools": ["t"]}, NO_OP
    )
    await skills_eval.handle_report(context, {"skill_ref": "s"}, NO_OP)
    await skills_eval.handle_calibrate(
        context, {"skill_ref": "s", "calibration": {"n": 1}}, NO_OP
    )
    assert [(m, p["op"]["op"], i) for m, p, i in sent] == [
        ("DecisionEval", "skill_eval_case_add", True),
        ("DecisionEval", "skill_eval_report", False),
        ("DecisionEval", "skill_eval_calibrate", True),
    ]
    assert all(p["op"]["request"]["tenant_id"] == "tenant:one" for _, p, _ in sent)
    _record(monkeypatch, skills_eval, EngineRefusal("EVAL_SCOPE_REQUIRED"))
    with pytest.raises(EngineRefusal) as refused:
        await skills_eval.handle_report(context, {"skill_ref": "s"}, NO_OP)
    assert refused.value.code == "EVAL_SCOPE_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R956")
async def test_ablation_record_and_canary_verdict_pass_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch, skills_eval, {"verdict": "ramp"})
    context = caller_context()
    await skills_eval.handle_ablation_record(
        context, {"record": {"skill_ref": "s", "runs": []}}, NO_OP
    )
    method, body, idempotent = sent[0]
    assert (method, idempotent) == ("DecisionLog", True)
    assert body["op"] == {
        "op": "learn",
        "tenant_id": "tenant:one",
        "write": {"write": "record_ablation", "record": {"skill_ref": "s", "runs": []}},
    }
    verdict = await skills_eval.handle_canary_decide(
        context, {"request": {"tenant_id": "other", "mode": "canary"}}, NO_OP
    )
    assert verdict == {"value": {"verdict": "ramp"}}
    assert sent[1][1]["request"]["tenant_id"] == "tenant:one"


async def test_promotion_never_sends_a_reviewer_and_surfaces_engine_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch, skills_eval)
    context = caller_context()
    await skills_eval.handle_tier_promote(
        context,
        {
            "request": {
                "tier_review": {"tier": "domain"},
                "tier_reviewer": "someone:else",
                "approver_principal": "someone:else",
            }
        },
        NO_OP,
    )
    request = sent[0][1]["request"]
    assert "tier_reviewer" not in request and "approver_principal" not in request
    assert request["tier_review"] == {"tier": "domain"}
    for code in ("TIER_SELF_REVIEW", "TIER_REVIEW_SCOPE_REQUIRED"):
        _record(monkeypatch, skills_eval, EngineRefusal(code))
        with pytest.raises(EngineRefusal) as refused:
            await skills_eval.handle_tier_promote(context, {"request": {}}, NO_OP)
        assert refused.value.code == code


async def test_config_layers_forward_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    layers = {"layers": [{"layer": "company", "scope": "c", "revision": 1}]}
    sent = _record(monkeypatch, config_layers, layers)
    reply = await config_layers.handle_config_layers(caller_context(), {}, NO_OP)
    assert reply == {"value": layers} and sent[0][:2] == ("ConfigLayers", {})
