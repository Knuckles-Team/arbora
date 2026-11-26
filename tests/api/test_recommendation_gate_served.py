"""A recommendation surfaces only after a passing safety verdict (ARBORA-OPS-R862)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.recommendation_gate import gate_recommendation, gate_result
from arbora.api.invoke.steps import OpError, OpResult


@pytest.mark.spec("ARBORA-OPS-R862")
def test_pass_surfaces_withheld_and_absent_block() -> None:
    passing = {"safety_verdict": "Pass", "answer": "a"}
    assert gate_recommendation(passing) is passing
    withheld = gate_recommendation(
        {"safety_verdict": {"Withheld": {"bound_id": "bound-7"}}}
    )
    assert isinstance(withheld, OpError) and withheld.code == "POLICY_DENIED"
    assert withheld.details == {
        "reason": "recommendation_withheld",
        "bound_id": "bound-7",
    }
    absent = gate_recommendation({"answer": "a"})
    assert absent.details == {"reason": "safety_verdict_absent"}
    # The one chokepoint: both Decide operations are gated, others pass unchanged.
    for op_id in ("eg.query.Decide", "eg.query.DecideText"):
        assert gate_result(op_id, OpResult(passing)).value is passing
        assert isinstance(gate_result(op_id, OpResult({"answer": 1})), OpError)
    other = OpResult({"answer": 1})
    assert gate_result("eg.query.Other", other) is other
    assert gate_recommendation([passing, {"safety_verdict": "Withheld"}]).code == (
        "POLICY_DENIED"
    )
