"""The closed version-1 projections and the cursor binding (ARBORA-DATA-MARKET-R003.4)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.domain.finance import read_models
from arbora.api.invoke.pipeline import OperationRefused
from tests.mcp_server._read_port_double import AS_OF, EVENT, MARKET, ORDER

SCOPE = {"op": "markets.list", "tenant": "tenant:one", "account": "acct-1"}
FILTERS = {"status": "open"}


def _page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"as_of": AS_OF, "items": items}


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.4")
def test_projection_keys_types_and_cursor_binding() -> None:
    page = read_models.project_market_page(_page([MARKET]), limit=7, cursor=None)
    assert set(page) == {"schema_version", "as_of", "items", "page"}
    assert page["schema_version"] == 1 and page["page"] == {
        "limit": 7,
        "next_cursor": None,
    }
    item = page["items"][0]
    assert set(item) == {
        "market_id", "event_id", "question", "status", "starts_at", "ends_at",
        "outcomes", "updated_at", "source", "as_of", "session", "freshness", "provenance",
    }  # fmt: skip
    assert item["ends_at"] is None and item["session"] is None
    assert item["outcomes"] == [
        {"name": "yes", "price": "0.55", "currency": "USD", "unit": "share"}
    ]
    assert set(item["freshness"]) == {"state", "age_seconds"}
    assert set(item["provenance"]) == {"record_id", "revision"}
    event = read_models.project_event({"as_of": AS_OF, "item": EVENT})
    assert set(event) == {"schema_version", "as_of", "item"}
    assert (
        event["item"]["market_ids"] == ["m1"] and event["item"]["description"] is None
    )
    order = read_models.project_order({"as_of": AS_OF, "item": ORDER})["item"]
    assert order["price"] == "0.55" and order["quantity_unit"] == "share"
    assert (
        read_models.project_event_page(_page([EVENT]), limit=1, cursor="c")["page"][
            "next_cursor"
        ]
        == "c"
    )
    assert read_models.project_order_page(_page([ORDER]), limit=1, cursor=None)["items"]


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.4")
def test_a_float_a_bad_code_or_a_missing_field_is_unavailable_never_invented() -> None:
    for broken in (
        {
            **MARKET,
            "outcomes": [{"name": "y", "price": 0.55, "currency": "USD", "unit": None}],
        },
        {
            **MARKET,
            "outcomes": [
                {"name": "y", "price": "0.5", "currency": "usd", "unit": None}
            ],
        },
        {**MARKET, "freshness": {"state": "fresh"}},
        {key: value for key, value in MARKET.items() if key != "source"},
        {**MARKET, "as_of": "yesterday"},
    ):
        with pytest.raises(OperationRefused) as refused:
            read_models.project_market_page(_page([broken]), limit=5, cursor=None)
        assert refused.value.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as too_many:
        read_models.project_market_page(_page([MARKET, MARKET]), limit=1, cursor=None)
    assert too_many.value.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as absent:
        read_models.project_market({"as_of": AS_OF, "item": None})
    assert absent.value.code == "NOT_FOUND"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.4")
def test_a_cursor_binds_tenant_account_filters_operation_and_size() -> None:
    token = read_models.encode_cursor(
        **SCOPE, filters=FILTERS, position="p2", snapshot="s1"
    )
    assert len(token.encode()) <= read_models.MAX_CURSOR_BYTES
    assert read_models.decode_cursor(token, **SCOPE, filters=FILTERS) == ("p2", "s1")
    for changed in (
        {**SCOPE, "tenant": "tenant:two"},
        {**SCOPE, "account": "acct-2"},
        {**SCOPE, "op": "markets.search"},
    ):
        with pytest.raises(OperationRefused) as refused:
            read_models.decode_cursor(token, **changed, filters=FILTERS)
        assert refused.value.code == "INVALID_ARGUMENT"
    with pytest.raises(OperationRefused):
        read_models.decode_cursor(token, **SCOPE, filters={"status": "closed"})
    for garbage in ("", "%%%", "e30", "x" * 600):
        with pytest.raises(OperationRefused):
            read_models.decode_cursor(garbage, **SCOPE, filters=FILTERS)
