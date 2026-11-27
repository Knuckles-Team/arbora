"""Instrument detail: rates and point-in-time dividends (ARBORA-DATA-MARKET-R009.2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import market_detail as detail
from arbora.api.ops.registry_factory import get_registry
from arbora.pin_lag.medulla import PinLagUnavailable
from tests.mcp_server._read_port_double import FakeReadPort

NO_OP: Any = None


def action(
    effective: int, revision: int, kind: dict[str, Any], listing: str = "m1"
) -> Any:
    return {
        "action": kind,
        "effective_time": effective,
        "known_at": effective + 1,
        "listing_id": listing,
        "revision": revision,
        "source": "venue-1",
    }


DIVIDEND = {"kind": "dividend", "amount_ticks": 250}
SPLIT = {"kind": "split", "from": 1, "to": 2}


class Source:
    def __init__(self, records: list[Any]) -> None:
        self.rows = records
        self.asked: list[str] = []

    async def records(self, _context: Any, listing_id: str) -> list[Any]:
        self.asked.append(listing_id)
        return self.rows


def context(source: Source | None = None) -> Any:
    services: dict[str, Any] = {"finance_read_port": FakeReadPort()}
    if source is not None:
        services["corporate_action_source"] = source
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant:one", principal="p"), services=services
    )


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(_c: Any, method: str, params: dict[str, Any], **_k: Any) -> Any:
        assert method == "FinanceMarket"
        sent.append(params)
        # The engine's view: the latest revision per effective time, as of the bound.
        rows = params["op"]["records"]
        as_of = params["op"].get("as_of")
        keep: dict[int, Any] = {}
        for row in rows:
            if as_of is not None and row["known_at"] > as_of:
                continue
            best = keep.get(row["effective_time"])
            if best is None or row["revision"] > best["revision"]:
                keep[row["effective_time"]] = row
        return [keep[k] for k in sorted(keep)]

    monkeypatch.setattr(detail, "engine_send", send)
    monkeypatch.setattr(detail, "guard", lambda *_a, **_k: None)
    return sent


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.2")
async def test_rates_and_dividends_tabs_and_unknown_listing(
    engine: list[dict[str, Any]],
) -> None:
    rates = await detail.handle_market_instrument_detail(
        context(), {"listing_id": "m1", "tab": "rates"}, NO_OP
    )
    assert rates["value"]["rates"] == [
        {
            "name": "yes",
            "value": "0.55",
            "currency": "USD",
            "as_of": "2026-11-01T12:00:00Z",
            "source": "venue-1",
        }
    ]
    source = Source(
        [
            action(100, 1, DIVIDEND),
            action(100, 2, {"kind": "dividend", "amount_ticks": 275}),
            action(200, 1, SPLIT),
            action(300, 1, DIVIDEND),
            action(400, 1, DIVIDEND, listing="other"),
        ]
    )
    out = await detail.handle_market_instrument_detail(
        context(source), {"listing_id": "m1", "tab": "dividends", "as_of": 150}, NO_OP
    )
    # Only dividends known by the bound, the latest revision, and only what the engine holds.
    assert out["value"]["dividends"] == [
        {
            "ex_date": 100,
            "amount_ticks": 275,
            "source": "venue-1",
            "revision": 2,
            "known_at": 101,
        }
    ]
    assert all(r["listing_id"] == "m1" for r in engine[0]["op"]["records"])
    assert "pay_date" not in out["value"]["dividends"][0]
    with pytest.raises(OperationRefused) as unknown:
        await detail.handle_market_instrument_detail(
            context(source), {"listing_id": "zz", "tab": "rates"}, NO_OP
        )
    assert unknown.value.code == "LISTING_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.2")
async def test_the_pinned_release_without_the_op_refuses_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.pin_lag import medulla as lag

    monkeypatch.setattr(
        lag,
        "require_models",
        lambda names: (_ for _ in ()).throw(
            PinLagUnavailable("FinanceMarketOpCorporateActions")
        ),
    )
    source = Source([])
    with pytest.raises(OperationRefused) as refused:
        await detail.handle_market_instrument_detail(
            context(source), {"listing_id": "m1", "tab": "dividends"}, NO_OP
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "FinanceMarketOpCorporateActions"
    assert source.asked == []


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.2")
def test_the_operation_is_served_as_a_finance_read() -> None:
    spec = get_registry().get("market.instrument.detail")
    assert spec is not None
    assert spec.scopes == frozenset({"finance:read"})
