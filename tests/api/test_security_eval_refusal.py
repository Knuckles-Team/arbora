"""Refusals start zero probes (ARBORA-OPS-R051.4)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import security_eval
from tests.api._security_eval_support import evaluation_context

_OP = security_eval.operations()[0]
_PARAMS = {"goal": "leak it", "target_ids": ["t1"], "target_categories": []}


async def _refused(ctx: object, params: dict) -> str:
    with pytest.raises(OperationRefused) as refused:
        await security_eval.handle_security_evaluate(ctx, params, _OP)
    return refused.value.code


@pytest.mark.spec("ARBORA-OPS-R051.4")
async def test_refusals_start_zero_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A target outside the caller's verified scope.
    ctx, engine, runner = evaluation_context(
        monkeypatch, scopes=("security:target:other",)
    )
    assert await _refused(ctx, _PARAMS) == "SUBJECT_ACCESS_DENIED"
    assert runner.calls == [] and engine.sent == []
    # One authorized target does not authorize a second.
    ctx, engine, runner = evaluation_context(monkeypatch)
    assert (
        await _refused(ctx, {**_PARAMS, "target_ids": ["t1", "t2"]})
        == "SUBJECT_ACCESS_DENIED"
    )
    assert runner.calls == [] and engine.sent == []
    # A request above the probe cap.
    ctx, engine, runner = evaluation_context(monkeypatch)
    assert await _refused(ctx, {**_PARAMS, "max_probes": 51}) == "LOAD_CAP_EXCEEDED"
    assert runner.calls == [] and engine.sent == []
    # A goal that selects no catalog entry.
    ctx, engine, runner = evaluation_context(monkeypatch, selected=[])
    assert await _refused(ctx, _PARAMS) == "INVALID_ARGUMENT"
    assert runner.calls == []
    # No composed evaluation or runner.
    ctx, engine, runner = evaluation_context(monkeypatch, bound=False)
    assert await _refused(ctx, _PARAMS) == "UNAVAILABLE"
    assert runner.calls == [] and engine.sent == []
