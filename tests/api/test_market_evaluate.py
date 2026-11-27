"""``market.evaluate`` forwards NumericEvaluate and computes nothing (ARBORA-DATA-MARKET-R003.10)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError

from tests.api._market_support import (  # noqa: F401
    MarketServed,
    captured,
    plain,
)
from tests.test_served_operation_api import _value, served  # noqa: F401

BRIER = {"op": "brier", "forecasts": [0.5, 0.25], "outcomes": [1.0, 0.0]}
ADF = {"op": "adf", "max_lag": 1, "series": [1.0, 2.0, 1.5, 2.5]}


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.10")
def test_market_evaluate_over_http_and_mcp(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    answers = {"NumericEvaluate": lambda params, _p: {"kernel": params["op"]["op"]}}
    market = MarketServed(captured, served, monkeypatch, answers)
    assert _value(market.http("market.evaluate", BRIER)) == {"kernel": "brier"}
    assert _value(market.http("market.evaluate", ADF)) == {"kernel": "adf"}
    assert market.mcp("market.evaluate", BRIER)["result"]["value"] == {
        "kernel": "brier"
    }
    sent = [plain(params) for _m, params, _p in market.engine.calls]
    assert sent == [{"op": BRIER}, {"op": ADF}, {"op": BRIER}]
    # An unknown kernel, a stray field and an oversized series stop before the engine.
    for body in (
        {"op": "nope"},
        {**BRIER, "tenant_id": "t"},
        {"op": "brier", "forecasts": [0.0] * 60_000, "outcomes": [0.0] * 60_000},
    ):
        refused = market.http("market.evaluate", body)
        assert refused.status_code == 400
    assert len(market.engine.calls) == 3


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.10")
def test_denied_grant_and_engine_codes(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    denied = MarketServed(
        captured, served, monkeypatch, {"NumericEvaluate": {"x": 1}}, scopes=()
    )
    assert denied.http("market.evaluate", BRIER).status_code == 403
    assert denied.engine.calls == []

    def refuse(_params: Any, _principal: str) -> Any:
        raise EngineResponseError("KERNEL_INPUT_SHORT")

    refusing = MarketServed(captured, served, monkeypatch, {"NumericEvaluate": refuse})
    assert (
        refusing.http("market.evaluate", ADF).json()["error"]["code"]
        == "KERNEL_INPUT_SHORT"
    )
