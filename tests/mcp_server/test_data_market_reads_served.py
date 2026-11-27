"""The nine marketplace reads over HTTP and MCP with a recording port (ARBORA-DATA-MARKET-R003.1 to R003.6)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.registry import Effect
from tests.api._market_support import MarketServed, capture_with_bindings
from tests.mcp_server._read_port_double import FakeReadPort
from tests.test_served_operation_api import _value, served  # noqa: F401

ALL_OPS = (
    "markets.list", "markets.search", "markets.get",
    "events.list", "events.search", "events.get",
    "orders.list", "orders.search", "orders.get",
)  # fmt: skip


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> FakeReadPort:
    """Composed before ``served``, so the host binds this port as ``finance_read_port``."""
    fake = FakeReadPort()
    fake.captured = capture_with_bindings(  # type: ignore[attr-defined]
        monkeypatch, {"finance_read_port": fake}
    )
    return fake


@pytest.fixture
def market(
    port: FakeReadPort,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> MarketServed:
    return MarketServed(port.captured, served, monkeypatch, {})  # type: ignore[attr-defined]


def _code(response: Any) -> str:
    return response.json()["error"]["code"]


def _each_resource(
    market: MarketServed, kind: str, filters: dict[str, Any], bad: dict[str, Any]
) -> None:
    plural = f"{kind}s"
    listed = _value(market.http(f"{plural}.list", {"limit": 5, **filters}))
    assert listed["schema_version"] == 1 and listed["page"]["limit"] == 5
    assert "provider_only" not in listed["items"][0]
    assert listed["page"]["next_cursor"] is not None
    onward = market.http(
        f"{plural}.list",
        {"limit": 5, **filters, "cursor": listed["page"]["next_cursor"]},
    )
    assert onward.status_code == 200
    searched = _value(market.http(f"{plural}.search", {"query": "a", **filters}))
    assert searched["page"]["limit"] == 50
    got = _value(market.http(f"{plural}.get", {f"{kind}_id": f"{kind[0]}1"}))
    assert got["item"]["provenance"]["record_id"] == f"{kind[0]}1"
    mcp = market.mcp(f"{plural}.list", {"limit": 5, **filters})
    assert mcp["ok"] is True and mcp["result"]["items"] == listed["items"]
    # Bounds, unlisted fields and a missing record stop or answer without an oracle.
    for body in (
        {"limit": 0}, {"limit": 101}, {"tenant": "t"}, {"account": "a"}, {"cursor": "x" * 513}, bad,
    ):  # fmt: skip
        assert market.http(f"{plural}.list", body).status_code == 400, body
    assert market.http(f"{plural}.search", {}).status_code == 400
    assert market.http(f"{plural}.search", {"query": "é"}).status_code == 400
    assert market.http(f"{plural}.search", {"query": "x" * 201}).status_code == 400
    assert market.http(f"{plural}.get", {f"{kind}_id": "-bad"}).status_code == 400
    absent = market.http(f"{plural}.get", {f"{kind}_id": "zzz"})
    assert absent.status_code == 404 and _code(absent) == "NOT_FOUND"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.1")
def test_markets_reads_served(market: MarketServed, port: FakeReadPort) -> None:
    filters = {
        "event_id": "e1",
        "status": "open",
        "starts_at_from": "2026-11-01T00:00:00Z",
    }
    _each_resource(market, "market", filters, {"side": "buy"})
    forwarded = [request for method, request in port.calls if method == "list_markets"]
    assert dict(forwarded[0].filters) == filters
    assert forwarded[0].tenant == "tenant:one" and forwarded[0].account == "acct-1"
    assert forwarded[1].position == "p2" and forwarded[1].snapshot == "s1"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.2")
def test_events_reads_served(market: MarketServed, port: FakeReadPort) -> None:
    filters = {"status": "open", "ends_at_to": "2026-12-01T00:00:00Z"}
    _each_resource(market, "event", filters, {"event_id": "e1"})
    assert {m for m, _r in port.calls} == {"list_events", "search_events", "get_event"}


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.3")
def test_orders_reads_served(market: MarketServed, port: FakeReadPort) -> None:
    filters = {
        "market_id": "m1",
        "side": "sell",
        "created_at_from": "2026-11-01T00:00:00Z",
    }
    _each_resource(market, "order", filters, {"side": "hold"})
    assert {m for m, _r in port.calls} == {"list_orders", "search_orders", "get_order"}


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.5")
def test_two_tenant_denials_make_zero_provider_calls(
    port: FakeReadPort,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No account is verified for the caller's tenant: unavailable, and no read happened.
    port.bindings = {"tenant:two": "acct-2"}
    captured = port.captured  # type: ignore[attr-defined]
    unbound = MarketServed(captured, served, monkeypatch, {})
    for op_id, body in (("markets.list", {}), ("events.get", {"event_id": "e1"})):
        refused = unbound.http(op_id, body)
        assert refused.status_code == 503 and _code(refused) == "UNAVAILABLE"
    assert port.calls == [] and port.binding_asks == ["tenant:one", "tenant:one"]
    # A missing grant is refused before the binding is even asked.
    denied = MarketServed(captured, served, monkeypatch, {}, scopes=())
    assert denied.http("orders.list", {}).status_code == 403
    assert port.calls == [] and len(port.binding_asks) == 2


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.6")
def test_both_routes_share_provider_dispatch_and_isolate_effects(
    market: MarketServed, port: FakeReadPort
) -> None:
    assert market.http("markets.list", {"limit": 3}).status_code == 200
    assert market.mcp("markets.list", {"limit": 3})["ok"] is True
    first, second = (request for _m, request in port.calls)
    assert first == second and first.tenant == "tenant:one"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.6")
def test_reads_declare_no_effect_confirmation_or_submission() -> None:
    from arbora.api.domain.finance.fleet_read_port import FinanceReadPort
    from arbora.api.ops import finance

    declared = {op.id: op for op in finance.operations() if op.id in ALL_OPS}
    assert set(declared) == set(ALL_OPS)
    for op in declared.values():
        assert op.effect is Effect.READ and op.confirm.name == "NONE"
        assert op.idempotency.name == "NONE" and op.scopes == {"finance:read"}
    assert not [
        name for name in dir(FinanceReadPort) if "submit" in name or "paper" in name
    ]


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.6")
def test_the_host_composes_the_fleet_read_port() -> None:
    from arbora.api.domain.finance.fleet_read_port import FleetFinanceReadPort
    from arbora.mcp_server.served_finance import _with_finance_read_port

    composed = _with_finance_read_port({})
    assert composed is not None
    assert isinstance(composed["finance_read_port"], FleetFinanceReadPort)
    sentinel = object()
    assert (
        _with_finance_read_port({"finance_read_port": sentinel})["finance_read_port"]
        is sentinel
    )  # type: ignore[index]
    assert _with_finance_read_port(None) is None
