"""A recording marketplace read port that answers from fixed provider records."""

from __future__ import annotations

from typing import Any

AS_OF = "2026-11-01T12:00:00Z"


def meta(record_id: str) -> dict[str, Any]:
    return {
        "source": "venue-1",
        "as_of": AS_OF,
        "session": None,
        "freshness": {"state": "fresh", "age_seconds": 3},
        "provenance": {"record_id": record_id, "revision": None},
    }


MARKET = {
    "market_id": "m1",
    "event_id": "e1",
    "question": "Will it rain?",
    "status": "open",
    "starts_at": "2026-11-02T00:00:00Z",
    "ends_at": None,
    "outcomes": [{"name": "yes", "price": "0.55", "currency": "USD", "unit": "share"}],
    "updated_at": None,
    "provider_only": "dropped",
    **meta("m1"),
}
EVENT = {
    "event_id": "e1",
    "title": "Weather",
    "description": None,
    "status": "open",
    "starts_at": None,
    "ends_at": None,
    "market_ids": ["m1"],
    "updated_at": None,
    **meta("e1"),
}
ORDER = {
    "order_id": "o1",
    "market_id": "m1",
    "side": "buy",
    "outcome": "yes",
    "price": "0.55",
    "price_currency": "USD",
    "price_unit": "share",
    "quantity": "10",
    "quantity_unit": "share",
    "status": "filled",
    "created_at": AS_OF,
    "updated_at": None,
    **meta("o1"),
}
RECORDS = {"market": MARKET, "event": EVENT, "order": ORDER}


class FakeReadPort:
    """Binds ``tenant:one`` to one account and records every read it is asked for."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self.bindings = {"tenant:one": "acct-1"} if bindings is None else bindings
        self.binding_asks: list[str] = []
        self.calls: list[tuple[str, Any]] = []
        self.known = {"m1", "e1", "o1"}

    async def account_binding(self, _context: Any, tenant: str) -> str | None:
        self.binding_asks.append(tenant)
        return self.bindings.get(tenant)

    def _answer(self, method: str, request: Any) -> dict[str, Any]:
        self.calls.append((method, request))
        kind = method.split("_", 1)[1].rstrip("s")
        if method.startswith("get_"):
            found = request.resource_id in self.known
            return {"as_of": AS_OF, "item": RECORDS[kind] if found else None}
        return {
            "as_of": AS_OF,
            "items": [RECORDS[kind]],
            "next_cursor": "p2",
            "snapshot": "s1",
        }

    async def list_markets(self, _c: Any, request: Any) -> Any:
        return self._answer("list_markets", request)

    async def search_markets(self, _c: Any, request: Any) -> Any:
        return self._answer("search_markets", request)

    async def get_market(self, _c: Any, request: Any) -> Any:
        return self._answer("get_market", request)

    async def list_events(self, _c: Any, request: Any) -> Any:
        return self._answer("list_events", request)

    async def search_events(self, _c: Any, request: Any) -> Any:
        return self._answer("search_events", request)

    async def get_event(self, _c: Any, request: Any) -> Any:
        return self._answer("get_event", request)

    async def list_orders(self, _c: Any, request: Any) -> Any:
        return self._answer("list_orders", request)

    async def search_orders(self, _c: Any, request: Any) -> Any:
        return self._answer("search_orders", request)

    async def get_order(self, _c: Any, request: Any) -> Any:
        return self._answer("get_order", request)
