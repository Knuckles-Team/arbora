"""``market.recommendation.get`` cites evidence or abstains, with no order field (ARBORA-DATA-MARKET-R009.3)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import market_compute
from tests.api._market_support import MarketServed, captured  # noqa: F401
from tests.test_served_operation_api import _value, served  # noqa: F401

EVIDENCE = {"ref": "scorecard:7", "digest": "a" * 64, "as_of": "2026-11-01T00:00:00Z"}
GROUNDED = {
    "recommendation": "accumulate",
    "strategy_version": "3",
    "evidence": [EVIDENCE],
    "abstain_reason": None,
    "scorecard_ref": "scorecard:7",
    "order": {"side": "buy", "qty": 5},
    "place_order": True,
}


def _tool(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[Any]:
    seen: list[Any] = []

    async def fleet(_context: Any, op_id: str, arguments: Any, **_k: Any) -> Any:
        assert op_id == "fleet.call"
        seen.append(dict(arguments))
        return {"value": answer}

    monkeypatch.setattr(market_compute, "invoke_registered", fleet)
    return seen


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.3")
def test_recommendation_cites_evidence_abstains_and_has_no_order_field(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    market = MarketServed(captured, served, monkeypatch, {})
    seen = _tool(monkeypatch, GROUNDED)
    body = {"listing_id": "lst-1", "strategy_id": "s-1"}
    served_value = _value(market.http("market.recommendation.get", body))
    assert served_value["recommendation"] == "accumulate"
    assert served_value["evidence"] == [EVIDENCE]
    assert set(served_value) == {
        "recommendation", "strategy_version", "evidence", "abstain_reason", "scorecard_ref",
    }  # fmt: skip
    assert market.mcp("market.recommendation.get", body)["ok"] is True
    assert seen[0] == {
        "server": "emerald-exchange",
        "tool": "emerald_strategy",
        "arguments": {
            "action": "recommend",
            "listing_id": "lst-1",
            "strategy_id": "s-1",
        },
    }
    # An abstain carries its reason and needs no evidence.
    _tool(
        monkeypatch,
        {
            **GROUNDED,
            "recommendation": "abstain",
            "evidence": [],
            "strategy_version": None,
            "abstain_reason": "sample too short",
        },
    )
    abstained = _value(
        market.http("market.recommendation.get", {"listing_id": "lst-1"})
    )
    assert (
        abstained["abstain_reason"] == "sample too short"
        and abstained["evidence"] == []
    )
    # A recommendation without evidence, or an abstain without a reason, is not served.
    for bad in (
        {**GROUNDED, "evidence": []},
        {**GROUNDED, "strategy_version": None},
        {**GROUNDED, "recommendation": "abstain", "abstain_reason": None},
        {**GROUNDED, "recommendation": "buy-now"},
        {"error": "tool unavailable"},
    ):
        _tool(monkeypatch, bad)
        refused = market.http("market.recommendation.get", {"listing_id": "lst-1"})
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "UNAVAILABLE"
    _tool(monkeypatch, {"error": "STRATEGY_NOT_MAPPED"})
    unmapped = market.http("market.recommendation.get", {"listing_id": "lst-1"})
    assert unmapped.json()["error"]["code"] == "STRATEGY_NOT_MAPPED"
    denied = MarketServed(captured, served, monkeypatch, {}, scopes=())
    assert (
        denied.http("market.recommendation.get", {"listing_id": "l"}).status_code == 403
    )
