"""``market.positions`` over the engine account projection (ARBORA-DATA-MARKET-R003.11)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest

from tests.api._market_support import MarketServed, capture_with_bindings, plain
from tests.mcp_server._read_port_double import FakeReadPort
from tests.test_served_operation_api import _value, served  # noqa: F401

BODY = {
    "as_of": "2026-11-01T00:00:00Z",
    "known_at": "2026-11-01T00:00:00Z",
    "base_currency": "USD",
    "basis_policy": "average",
}
PROJECTION = {
    "positions": [
        {
            "listing_id": "lst-1",
            "quantity": "10",
            "value": "1234.50",
            "source": "venue-1",
            "as_of": "2026-11-01T00:00:00Z",
            "session": "regular",
        }
    ]
}


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> FakeReadPort:
    """Composed before ``served``, so the host binds this port as ``finance_read_port``."""
    fake = FakeReadPort()
    fake.captured = capture_with_bindings(  # type: ignore[attr-defined]
        monkeypatch, {"finance_read_port": fake}
    )
    return fake


def _market(
    port: FakeReadPort,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, Any],
    scopes: tuple[str, ...] = ("finance:read",),
) -> MarketServed:
    return MarketServed(
        port.captured,  # type: ignore[attr-defined]
        served,
        monkeypatch,
        answers,
        scopes=scopes,
    )


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.11")
def test_positions_carry_provenance(
    port: FakeReadPort, served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = _market(port, served, monkeypatch, {"FinanceAccountProject": PROJECTION})
    answer = _value(market.http("market.positions", BODY))
    # Every figure is the engine's, with its source, as-of and session intact.
    assert answer == PROJECTION
    assert answer["positions"][0]["value"] == "1234.50"
    over_mcp = market.mcp("market.positions", BODY)
    assert over_mcp["ok"] is True, over_mcp
    sent = [plain(params) for _name, params, _principal in market.engine.calls]
    # The account is the verified binding, never a request field.
    assert sent[0]["account"] == "acct-1" and sent[0]["base_currency"] == "USD"
    refused = market.http("market.positions", {**BODY, "account": "other"})
    assert refused.status_code == 400
    assert len(market.engine.calls) == 2


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.11")
def test_an_unverified_binding_refuses_before_any_engine_call(
    port: FakeReadPort, served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    port.bindings = {}
    market = _market(port, served, monkeypatch, {"FinanceAccountProject": {}})
    refused = market.http("market.positions", BODY)
    assert refused.json()["error"]["code"] == "ACCOUNT_BINDING_UNVERIFIED"
    assert market.engine.calls == []


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.11")
def test_a_missing_finance_grant_is_denied(
    port: FakeReadPort, served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = _market(
        port, served, monkeypatch, {"FinanceAccountProject": {}}, scopes=()
    )
    assert market.http("market.positions", BODY).status_code == 403
    assert market.engine.calls == []
