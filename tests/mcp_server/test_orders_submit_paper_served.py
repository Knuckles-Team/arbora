"""``orders.paper_submit`` is planned, keyed and tenant-bound (ARBORA-DATA-MARKET-R003.7)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import finance
from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.test_served_operation_api import _call, served  # noqa: F401

KEY = "Idempotency-Key"
PAPER = {"account_id": "acct-1", "symbol": "AAPL", "side": "buy", "qty": 2.0}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    confirmable_invocation(monkeypatch)
    venue: list[Any] = []

    async def fleet(_context: Any, op_id: str, arguments: Any, **_k: Any) -> Any:
        venue.append((op_id, arguments["tool"], arguments["arguments"]))
        return {"value": {"status": "FILLED", "order_id": "o1"}}

    monkeypatch.setattr(finance, "invoke_registered", fleet)
    return venue


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.7")
def test_paper_submit_is_planned_keyed_and_tenant_bound(
    stack: list[Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    route_methods(
        client,
        {},
        monkeypatch,
        {
            "fixture-verified": person(
                "one", extra_scopes=("finance:propose-order", "finance:read")
            )
        },
    )
    op = {o.id: o for o in finance.operations()}["orders.paper_submit"]
    assert op.confirm.name == "PLAN" and op.idempotency.name == "KEY_REQUIRED"
    assert op.scopes == {"finance:read", "finance:propose-order"}
    # The served runtime binds no service executor, so the account binding is
    # unverified: every attempt refuses UNAVAILABLE before any venue call.
    for headers, body in (
        ({}, PAPER),
        ({KEY: "k1"}, PAPER),
        ({KEY: "k2"}, {**PAPER, "mode": "live"}),
    ):
        refused = _call(app, "orders.paper_submit", body, **headers)
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["code"] == "UNAVAILABLE"
    assert stack == []
