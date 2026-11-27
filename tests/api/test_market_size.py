"""``market.size`` forwards RegimeSizingInput and adds no weight (ARBORA-DATA-MARKET-R003.9)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError

from tests.api._market_support import (  # noqa: F401
    SIZING_POLICY,
    SIZING_REQUEST,
    MarketServed,
    captured,
    plain,
)
from tests.test_served_operation_api import _value, served  # noqa: F401

BODY = {
    "forecast_digest": "d" * 64,
    "policy": SIZING_POLICY,
    "request": SIZING_REQUEST,
}


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.9")
def test_size_and_abstain_over_http_and_mcp(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    sized = {"weights": {"lst-1": 25}, "digest": "x"}
    market = MarketServed(
        captured, served, monkeypatch, {"RegimeSizingInput": lambda _p, _u: sized}
    )
    assert _value(market.http("market.size", BODY)) == sized
    assert market.mcp("market.size", BODY)["result"]["value"] == sized
    # The engine receives the request unchanged: Arbora applies no weight or default.
    assert [plain(params) for _m, params, _p in market.engine.calls] == [BODY, BODY]
    # A malformed policy is refused before any engine call.
    bad = market.http("market.size", {**BODY, "policy": {"policy_id": "p"}})
    assert bad.status_code == 400
    assert len(market.engine.calls) == 2


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.9")
def test_an_abstain_passes_through_unchanged(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    abstain = {"abstain": True, "code": "SIZING_ABSTAIN", "reason": "calibration"}
    market = MarketServed(
        captured, served, monkeypatch, {"RegimeSizingInput": lambda _p, _u: abstain}
    )
    assert _value(market.http("market.size", BODY)) == abstain

    def refuse(_params: Any, _principal: str) -> Any:
        raise EngineResponseError("SIZING_ABSTAIN")

    refusing = MarketServed(
        captured, served, monkeypatch, {"RegimeSizingInput": refuse}
    )
    answer = refusing.http("market.size", BODY)
    assert answer.json()["error"]["code"] == "SIZING_ABSTAIN"
    denied = MarketServed(
        captured, served, monkeypatch, {"RegimeSizingInput": abstain}, scopes=()
    )
    assert denied.http("market.size", BODY).status_code == 403
    assert denied.engine.calls == []
