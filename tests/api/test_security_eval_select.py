"""Probe selection comes from the engine answer and the cap holds (ARBORA-OPS-R051.2)."""

from __future__ import annotations

import pytest

from arbora.api.ops import security_eval
from tests.api._security_eval_support import evaluation_context

_OP = security_eval.operations()[0]
_PARAMS = {
    "goal": "leak the system prompt",
    "target_ids": ["t1"],
    "target_categories": [],
}


@pytest.mark.spec("ARBORA-OPS-R051.2")
async def test_only_declared_catalog_entries_are_selected_and_cap_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, engine, runner = evaluation_context(
        monkeypatch, selected=["attack-1", "tx-1", "tx-2"]
    )
    result = await security_eval.handle_security_evaluate(
        ctx, {**_PARAMS, "max_probes": 2}, _OP
    )
    # The runner is given exactly the engine's first two entries and nothing else.
    assert runner.calls == [(("attack-1", "tx-1"), "t1")]
    assert result["value"]["entry_ids"] == ["attack-1", "tx-1"]
    methods = [sent[0] for sent in engine.sent]
    assert methods[0] == "Decide"
    selection = engine.sent[0][1]["request"]
    assert selection["question"]["kind"] == "probe_selection"
    assert selection["candidates"]["source"] == "agent_library"
    assert {
        "name": "goal_text",
        "value": {"type": "text", "value": _PARAMS["goal"]},
    } in selection["params"]
    # One verdict question per probe, each naming the probe's own attack.
    verdicts = [s[1]["request"] for s in engine.sent[1:]]
    assert [v["question"]["kind"] for v in verdicts] == ["probe_verdict"] * 2
    findings = result["value"]["findings"]
    assert [f["probe_id"] for f in findings] == ["t1:attack-1", "t1:tx-1"]
    assert [f["verdict"] for f in findings] == ["compromised", "inconclusive"]


@pytest.mark.spec("ARBORA-OPS-R051.2")
async def test_the_default_cap_is_fifty_and_verdicts_are_the_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _engine, runner = evaluation_context(
        monkeypatch,
        selected=[f"attack-{i}" for i in range(60)],
        verdicts={f"attack-{i}": "resisted" for i in range(60)},
    )
    result = await security_eval.handle_security_evaluate(ctx, _PARAMS, _OP)
    assert len(runner.calls[0][0]) == security_eval.DEFAULT_MAX_PROBES == 50
    assert {f["verdict"] for f in result["value"]["findings"]} == {"resisted"}


@pytest.mark.spec("ARBORA-OPS-R051.2")
async def test_an_unlisted_question_kind_refuses_unavailable_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops import question_round as _question
    from arbora.pin_lag.medulla import PinLagUnavailable

    ctx, engine, runner = evaluation_context(monkeypatch)

    def unlisted(kind: str) -> None:
        raise PinLagUnavailable(f"QuestionKind.{kind}")

    monkeypatch.setattr(_question, "require_question_kind", unlisted)
    with pytest.raises(OperationRefused) as refused:
        await security_eval.handle_security_evaluate(ctx, _PARAMS, _OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "QuestionKind.probe_selection"
    assert runner.calls == [] and engine.sent == []
