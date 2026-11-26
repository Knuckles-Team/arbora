"""Tests for the deduplicated flip-alert delivery record
(ARBORA-DATA-MARKET-R001; FI-06/FI-07 in
``specs/data-and-market-projections/test-spec.md``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.finance.alerts import (
    FlipAlertDelivery,
    record_alert_delivery,
)
from arbora.finance.receipts import ServedReceiptEngine
from tests.finance._receipt_engine import ReceiptMachine


def _store() -> ServedReceiptEngine:
    return ReceiptMachine().engine()


def _delivery(**overrides: object) -> FlipAlertDelivery:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "event_id": "flip-123",
        "subscriber_id": "user-1",
        "channel": "email",
    }
    base.update(overrides)
    return FlipAlertDelivery.model_validate(base)


async def test_first_delivery_is_delivered() -> None:
    store = _store()

    outcome = await record_alert_delivery(store, _delivery())

    assert outcome == "delivered"


async def test_redelivery_of_the_same_event_is_a_duplicate() -> None:
    """FI-06/FI-07: an at-least-once transport retry or a restart replay of
    the same (tenant, event, subscriber) triple never causes a second
    user-visible delivery."""

    store = _store()
    await record_alert_delivery(store, _delivery())

    second = await record_alert_delivery(store, _delivery())

    assert second == "duplicate"


async def test_different_subscribers_both_receive_the_same_event() -> None:
    store = _store()

    first = await record_alert_delivery(store, _delivery(subscriber_id="user-1"))
    second = await record_alert_delivery(store, _delivery(subscriber_id="user-2"))

    assert first == "delivered"
    assert second == "delivered"


async def test_repeated_redelivery_stays_duplicate() -> None:
    store = _store()
    delivery = _delivery()
    await record_alert_delivery(store, delivery)

    outcomes = [await record_alert_delivery(store, delivery) for _ in range(5)]

    assert outcomes == ["duplicate"] * 5


async def test_an_alert_can_never_carry_an_order_or_portfolio_field() -> None:
    """An alert never authorizes an order or changes a portfolio: the model
    rejects any such field outright rather than silently ignoring it."""

    with pytest.raises(ValidationError):
        _delivery(order_id="order-1")
