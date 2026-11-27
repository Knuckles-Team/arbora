"""An in-scope target yields findings and an out-of-scope target refuses (ARBORA-OPS-R051, R051.6)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.registry_factory import get_registry
from arbora.api.ops.security_eval import handle_security_evaluate
from tests.api._security_eval_support import evaluation_context

_PARAMS = {
    "goal": "leak the system prompt",
    "target_ids": ["t1"],
    "target_categories": [],
}


@pytest.mark.spec("ARBORA-OPS-R051")
@pytest.mark.spec("ARBORA-OPS-R051.6")
async def test_in_scope_target_yields_findings_and_out_of_scope_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op = get_registry()["security.evaluate"]
    ctx, engine, runner = evaluation_context(monkeypatch)
    result = await handle_security_evaluate(ctx, _PARAMS, op)
    findings = result["value"]["findings"]
    assert {f["verdict"] for f in findings} == {"compromised", "inconclusive"}
    assert runner.calls == [(("attack-1", "tx-1"), "t1")]

    ctx, engine, runner = evaluation_context(monkeypatch, scopes=())
    with pytest.raises(OperationRefused) as refused:
        await handle_security_evaluate(ctx, _PARAMS, op)
    assert refused.value.code == "SUBJECT_ACCESS_DENIED"
    assert runner.calls == [] and engine.sent == []

    # A missing producer contract stays typed UNAVAILABLE.
    ctx, _, _ = evaluation_context(monkeypatch, bound=False)
    with pytest.raises(OperationRefused) as missing:
        await handle_security_evaluate(ctx, _PARAMS, op)
    assert missing.value.code == "UNAVAILABLE"
