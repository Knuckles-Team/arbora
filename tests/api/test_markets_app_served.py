"""The Markets reads over the served app, HTTP and MCP (ARBORA-DATA-MARKET-R009.7)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import msgpack
import pytest

from arbora.api.ops import finance_import as imp
from arbora.api.ops import market_compute
from arbora.api.ops import market_detail as detail
from tests.api._market_support import MarketServed, capture_with_bindings, plain
from tests.api.test_finance_import_served import (
    DIGEST,
    MAPPING,
    Sink,
    Source,
    sdk,
)
from tests.api.test_finance_receipts_list import VIEWS
from tests.mcp_server._read_port_double import FakeReadPort
from tests.test_served_operation_api import _value, served  # noqa: F401

SCOPES = ("finance:read", "finance:track")
KEY = {"Idempotency-Key": "journey-commit-1"}
PROJECTION = {
    "positions": [
        {
            "listing_id": "lst-1",
            "quantity": "10.000",
            "value": "1234.50",
            "source": "venue-1",
            "as_of": "2026-11-01T00:00:00Z",
            "session": "regular",
        }
    ]
}
CARD = {
    "method": "twr",
    "currency": "USD",
    "value": "0.0825",
    "unavailable_reason": None,
    "benchmark": {"name": "spx", "value": "0.1050"},
}
EVIDENCE = {"ref": "scorecard:7", "digest": "a" * 64, "as_of": "2026-11-01T00:00:00Z"}
RECOMMENDATION = {
    "recommendation": "hold",
    "strategy_version": "3",
    "evidence": [EVIDENCE],
    "abstain_reason": None,
    "scorecard_ref": "scorecard:7",
}
IMPORT = {"artifact": "up-1", "file_digest": DIGEST, "mapping": MAPPING}
READ_BODY = {
    "as_of": "2026-11-01T00:00:00Z",
    "known_at": "2026-11-01T00:00:00Z",
    "base_currency": "USD",
    "basis_policy": "average",
}
PERFORMANCE = {
    "range": "1Y",
    "method": "twr",
    "as_of": "2026-11-01T00:00:00Z",
    "known_at": "2026-11-01T00:00:00Z",
    "base_currency": "USD",
    "benchmark": "spx",
}


class Nodes:
    """The engine nodes the import keeps, as one in-memory ledger."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}

    async def send(
        self, _context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        if method == "CreateNodeIfAbsent":
            fields = msgpack.unpackb(params["properties_msgpack"], raw=False)
            self.nodes.setdefault(params["node_id"], fields)
            return True
        return [
            [n, msgpack.packb(self.nodes[n], use_bin_type=True)]
            for n in params["node_ids"]
            if n in self.nodes
        ]


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Sink, Nodes, list[Any]]:
    sink, nodes, fleet_calls = Sink(), Nodes(), []
    # Composed before ``served`` builds the app, so the host binds these ports.
    captured = capture_with_bindings(
        monkeypatch,
        {
            "finance_read_port": FakeReadPort(),
            "finance_import_source": Source(),
            "finance_activity_sink": sink,
        },
    )
    monkeypatch.setattr(imp, "engine_send", nodes.send)
    monkeypatch.setattr(imp, "require_csv_preview", lambda: sdk())
    monkeypatch.setattr(detail, "guard", lambda *_a, **_k: None)

    async def fleet(_context: Any, op_id: str, arguments: Any, **_k: Any) -> Any:
        fleet_calls.append((op_id, dict(arguments)))
        return {"value": RECOMMENDATION}

    monkeypatch.setattr(market_compute, "invoke_registered", fleet)
    return captured, sink, nodes, fleet_calls


def _journey(
    composed: tuple[Any, Sink, Nodes, list[Any]],
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> MarketServed:
    captured = composed[0]
    return MarketServed(
        captured,
        served,
        monkeypatch,
        {
            "FinanceAccountProject": PROJECTION,
            "FinancePerformance": CARD,
            "FinanceReceiptList": lambda p, _u: {
                "receipts": [v for v in VIEWS if v["tenant_key"] == p["tenant_key"]]
            },
        },
        scopes=SCOPES,
    )


def _import_step(market: MarketServed, sink: Sink) -> None:
    """Preview, commit, then the same file again is the first receipt."""
    preview = _value(market.http("finance.import.preview", IMPORT))
    assert preview["file_digest"] == DIGEST
    assert preview["reimport"] is False
    assert preview["mapping_errors"] == []
    assert preview["row_errors"] == []
    commit = {"preview_id": preview["preview_id"], "account": "acct-1"}
    first = _value(market.http("finance.import.commit", commit, **KEY))
    assert first["imported"] == 2
    assert first["skipped_duplicate"] == 0
    again = market.mcp(
        "finance.import.commit", commit, verb="act", idempotency_key="journey-commit-2"
    )
    assert again["ok"] is True, again
    repeat = again["result"]["value"]
    assert repeat["receipt_id"] == first["receipt_id"]
    assert repeat["imported"] == 0
    assert len(sink.ingested) == 1
    assert _value(market.http("finance.import.preview", IMPORT))["reimport"] is True
    assert market.mcp("finance.import.preview", IMPORT)["ok"] is True


def _holdings_and_card_step(market: MarketServed) -> None:
    """Holdings come from the positions read and a card from the performance read."""
    positions = _value(market.http("market.positions", READ_BODY))
    assert positions["positions"][0]["quantity"] == "10.000"
    assert positions["positions"][0]["value"] == "1234.50"
    assert market.mcp("market.positions", READ_BODY)["ok"] is True
    card = _value(market.http("market.performance", PERFORMANCE))
    assert card["value"] == "0.0825"
    assert card["benchmark"]["value"] == "0.1050"
    assert market.mcp("market.performance", PERFORMANCE)["result"]["value"] == card


def _receipts_and_recommendation_step(market: MarketServed) -> None:
    receipts = _value(market.http("finance.receipts.list", {}))
    assert [i["receipt_id"] for i in receipts["items"]] == ["r1", "r2"]
    assert receipts["items"][1]["mode"] == "paper"
    assert market.mcp("finance.receipts.list", {})["ok"] is True
    body = {"listing_id": "lst-1"}
    rec = _value(market.http("market.recommendation.get", body))
    assert rec["recommendation"] == "hold"
    assert rec["evidence"] == [EVIDENCE]
    assert market.mcp("market.recommendation.get", body)["ok"] is True


def _no_order_step(market: MarketServed, fleet_calls: list[Any]) -> None:
    sent = [name for name, *_ in market.engine.calls]
    assert not [n for n in sent if "Order" in n]
    tools = [arguments.get("tool") for _op, arguments in fleet_calls]
    assert "emerald_orders" not in tools
    projected = [
        plain(p) for n, p, _u in market.engine.calls if n == "FinanceAccountProject"
    ]
    assert projected[0]["account"] == "acct-1"


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.7")
def test_markets_journey_over_mcp_and_http(
    composed: tuple[Any, Sink, Nodes, list[Any]],
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _captured, sink, nodes, fleet_calls = composed
    market = _journey(composed, served, monkeypatch)
    _import_step(market, sink)
    _holdings_and_card_step(market)
    _receipts_and_recommendation_step(market)
    _no_order_step(market, fleet_calls)
    assert nodes.nodes  # the preview and the first receipt are engine nodes
