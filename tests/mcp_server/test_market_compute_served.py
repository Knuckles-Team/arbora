"""Served market calculations over HTTP and MCP against a recorded engine (ARBORA-DATA-MARKET)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError

from tests.api._market_support import (  # noqa: F401
    BACKTEST,
    FLIP,
    REPLAY,
    SCAN,
    MarketServed,
    captured,
    plain,
)
from tests.test_served_operation_api import _value, served  # noqa: F401


def _refuse(code: str) -> Any:
    def raise_it(_params: Any, _principal: str) -> Any:
        raise EngineResponseError(code)

    return raise_it


def _code(response: Any) -> str:
    return response.json()["error"]["code"]


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.8")
def test_signal_ops_over_http_and_mcp(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    answers = {"FinanceMarket": lambda params, _p: {"echo": params["op"]["op"]}}
    market = MarketServed(captured, served, monkeypatch, answers)
    cases = (
        ("market.signal_replay", "request", REPLAY, "signal_replay"),
        ("market.signal_scan", "request", SCAN, "signal_scan"),
        ("market.flip_confidence", "request", FLIP, "flip_confidence"),
        ("market.backtest_run", "draft", BACKTEST, "backtest_run"),
    )
    for op_id, key, body, wire in cases:
        assert _value(market.http(op_id, {key: body})) == {"echo": wire}
        answer = market.mcp(op_id, {key: body})
        assert answer["ok"] is True, answer
    sent = [plain(params) for _name, params, _principal in market.engine.calls]
    assert sent[0] == {"op": {"op": "signal_replay", "request": REPLAY}}
    assert sent[6] == {"op": {"op": "backtest_run", "draft": BACKTEST}}
    assert len(sent) == 8
    # A request outside the engine's shape is refused before any engine call.
    bad = market.http("market.signal_scan", {"request": {"limit": -1, "states": []}})
    assert bad.status_code == 400 and _code(bad) == "INVALID_ARGUMENT"
    extra = market.http("market.signal_scan", {"request": SCAN, "tenant": "other"})
    assert extra.status_code == 400
    assert len(market.engine.calls) == 8


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.8")
def test_signal_ops_need_the_finance_grant_and_pass_engine_codes(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    denied = MarketServed(
        captured, served, monkeypatch, {"FinanceMarket": {"ok": 1}}, scopes=()
    )
    refused = denied.http("market.signal_scan", {"request": SCAN})
    assert refused.status_code == 403
    assert denied.engine.calls == []
    refusing = MarketServed(
        captured,
        served,
        monkeypatch,
        {"FinanceMarket": _refuse("KERNEL_PARAM_INVALID")},
    )
    answer = refusing.http("market.signal_scan", {"request": SCAN})
    assert _code(answer) == "KERNEL_PARAM_INVALID"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.12")
def test_research_kernel_ops_over_http_and_mcp(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    answers = {
        "FinancePatternClassify": lambda params, _p: {"patterns": len(params["open"])},
        "FinanceProfitAttribution": lambda params, _p: {
            "groups": len(params["groups"])
        },
    }
    market = MarketServed(captured, served, monkeypatch, answers)
    window = {
        "open": [1.0, 2.0],
        "high": [2.0, 3.0],
        "low": [0.5, 1.5],
        "close": [1.5, 2.5],
    }
    assert _value(market.http("market.pattern_classify", window)) == {"patterns": 2}
    groups = {
        "groups": [{"group": "tech", "r_b": 0.1, "r_p": 0.2, "w_b": 0.5, "w_p": 0.6}]
    }
    assert _value(market.http("market.profit_attribution", groups)) == {"groups": 1}
    assert market.mcp("market.pattern_classify", window)["ok"] is True
    assert market.mcp("market.profit_attribution", groups)["ok"] is True
    assert plain(market.engine.calls[0][1]) == window
    # Engine refusal codes reach the caller unchanged.
    for method, op_id, body, code in (
        (
            "FinancePatternClassify",
            "market.pattern_classify",
            window,
            "PATTERN_WINDOW_SHORT",
        ),
        (
            "FinancePatternClassify",
            "market.pattern_classify",
            window,
            "PATTERN_INPUT_INVALID",
        ),
        (
            "FinanceProfitAttribution",
            "market.profit_attribution",
            groups,
            "ATTRIBUTION_WEIGHTS_INVALID",
        ),
        (
            "FinanceProfitAttribution",
            "market.profit_attribution",
            groups,
            "ATTRIBUTION_INPUT_INVALID",
        ),
    ):
        refusing = MarketServed(captured, served, monkeypatch, {method: _refuse(code)})
        assert _code(refusing.http(op_id, body)) == code


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.14")
def test_banking_ops_over_http_and_mcp(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    answers = {
        "FinanceAmlScreen": {"flag": False},
        "FinanceCapitalRatios": {"cet1_ratio": "0.1"},
        "FinanceExpectedLoss": {"minor": 12},
        "FinanceSettlementChain": {"hops": 2},
    }
    market = MarketServed(captured, served, monkeypatch, answers)
    bodies = {
        "market.aml_screen": {
            "amount_minor": 5000,
            "rule_id": "r1",
            "threshold_minor": 10000,
            "version": 1,
        },
        "market.capital_ratios": {
            "at1": 1,
            "cet1": 2,
            "risk_weighted_assets": 30,
            "tier2": 3,
            "total_exposure": 40,
        },
        "market.expected_loss": {"ead_minor": 1000, "lgd": 0.4, "pd": 0.02},
        "market.settlement_chain": {
            "amount_minor": 100,
            "beneficiary": "b",
            "correspondents": ["c"],
            "origin": "o",
        },
    }
    for op_id, body in bodies.items():
        assert _value(market.http(op_id, body)) is not None
        assert market.mcp(op_id, body)["ok"] is True
    assert len(market.engine.calls) == 8
    # A float in a minor-unit field is refused before dispatch.
    before = len(market.engine.calls)
    for op_id, field in (
        ("market.aml_screen", "amount_minor"),
        ("market.capital_ratios", "cet1"),
        ("market.expected_loss", "ead_minor"),
        ("market.settlement_chain", "amount_minor"),
    ):
        floaty = {**bodies[op_id], field: 1.5}
        refused = market.http(op_id, floaty)
        assert refused.status_code == 400 and _code(refused) == "INVALID_ARGUMENT"
    assert len(market.engine.calls) == before
    for method, op_id, code in (
        ("FinanceCapitalRatios", "market.capital_ratios", "CAPITAL_RWA_INVALID"),
        ("FinanceExpectedLoss", "market.expected_loss", "CREDIT_INPUT_INVALID"),
    ):
        refusing = MarketServed(captured, served, monkeypatch, {method: _refuse(code)})
        assert _code(refusing.http(op_id, bodies[op_id])) == code
