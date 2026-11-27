"""Finance receipts move through the engine machine (ARBORA-DATA-MARKET-R001.1)."""

from __future__ import annotations

import pytest

from arbora.api.domain.finance.receipts import claim_receipt, receipt_id
from arbora.finance.alerts import FlipAlertDelivery, record_alert_delivery
from tests.finance._receipt_engine import ReceiptMachine


@pytest.mark.spec("ARBORA-DATA-MARKET-R001.1")
async def test_receipt_claims_once_and_survives_a_new_port() -> None:
    machine = ReceiptMachine()
    first = machine.engine()
    ids = ("tenant-a", "schedule_run", "k-1")
    claim = {"event_id": "e", "recorded_at": "t"}
    assert (
        await claim_receipt(first, "tenant-a", "schedule_run", "k-1", **claim) is True
    )
    restarted = machine.engine()
    assert (
        await claim_receipt(restarted, "tenant-a", "schedule_run", "k-1", **claim)
        is False
    )
    node = machine.nodes[receipt_id(*ids)]
    assert node["receiptState"] == "claimed"
    assert node["receiptAttempt"] == 1
    assert len(machine.nodes) == 1
    # The id never carries the state, so the same key in another tenant is another node.
    assert receipt_id("tenant-b", "schedule_run", "k-1") not in machine.nodes


@pytest.mark.spec("ARBORA-DATA-MARKET-R001.1")
async def test_alert_delivery_completes_once_and_replays_as_duplicate() -> None:
    machine = ReceiptMachine()
    alert = FlipAlertDelivery(
        tenant_id="tenant-a", event_id="e-1", subscriber_id="s-1", channel="chat"
    )
    assert await record_alert_delivery(machine.engine(), alert) == "delivered"
    assert await record_alert_delivery(machine.engine(), alert) == "duplicate"
    node = machine.nodes[receipt_id("tenant-a", "alert_delivery", "e-1:s-1")]
    assert node["receiptState"] == "completed"
    assert node["receiptChannel"] == "chat"
    other = alert.model_copy(update={"event_id": "e-2"})
    assert await record_alert_delivery(machine.engine(), other) == "delivered"


@pytest.mark.spec("ARBORA-DATA-MARKET-R001.1")
async def test_engine_failure_propagates_and_nothing_is_acknowledged() -> None:
    machine = ReceiptMachine()
    machine.fail = True
    with pytest.raises(ConnectionError):
        await claim_receipt(
            machine.engine(), "t", "schedule_run", "k", event_id="e", recorded_at="t"
        )
    assert machine.nodes == {}
