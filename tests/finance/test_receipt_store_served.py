"""ARBORA-DATA-MARKET-R001.1: the finance receipt store is engine-durable.

The store is composed exactly as invocation composition composes it -- a
``FinanceReceiptStore`` over the caller-bound ``ServedBrowserState`` engine
authority -- against a loopback engine node table that outlives the host.
"""

from __future__ import annotations

import pytest

from arbora.finance.alerts import FlipAlertDelivery, record_alert_delivery
from arbora.finance.receipt_store import FinanceReceiptUnavailable, receipt_node_id
from arbora.security.session import use_session
from tests.finance._receipt_engine import ReceiptEngine, receipt_store, tenant_session


@pytest.mark.spec("ARBORA-DATA-MARKET-R001.1")
def test_receipt_survives_restart_and_is_tenant_scoped() -> None:
    engine = ReceiptEngine()
    with use_session(tenant_session("tenant-a")):
        assert receipt_store(engine).claim("tenant-a", "schedule_run", "k-1") is True

    # Host restart: a new store and a new engine authority over the same engine.
    restarted = receipt_store(engine)
    with use_session(tenant_session("tenant-a")):
        assert restarted.claim("tenant-a", "schedule_run", "k-1") is False
        assert restarted.state("tenant-a", "schedule_run", "k-1") == "claimed"
        restarted.complete("tenant-a", "schedule_run", "k-1")
        assert restarted.state("tenant-a", "schedule_run", "k-1") == "completed"

    claimed = receipt_node_id("tenant-a", "schedule_run", "k-1", "claimed")
    assert [key for key in engine.nodes if key[1] == claimed] == [("tenant-a", claimed)]
    node = engine.nodes[("tenant-a", claimed)]
    assert node["type"] == "FinanceReceipt"
    assert node["tenantKey"] == "tenant-a"
    assert node["idempotencyKey"] == "k-1"

    # Zero cross-tenant visibility: tenant B sees nothing and cannot name A.
    with use_session(tenant_session("tenant-b")):
        assert restarted.state("tenant-b", "schedule_run", "k-1") is None
        with pytest.raises(PermissionError, match="AUTH_TENANT_MISMATCH"):
            restarted.state("tenant-a", "schedule_run", "k-1")


@pytest.mark.spec("ARBORA-DATA-MARKET-R001.1")
def test_alert_dedupes_across_restart_and_unavailable_store_refuses() -> None:
    engine = ReceiptEngine()
    alert = FlipAlertDelivery(
        tenant_id="tenant-a", event_id="e-1", subscriber_id="s-1", channel="email"
    )
    with use_session(tenant_session("tenant-a")):
        assert record_alert_delivery(receipt_store(engine), alert) == "delivered"
        assert record_alert_delivery(receipt_store(engine), alert) == "duplicate"

        engine.fail = True
        with pytest.raises(FinanceReceiptUnavailable) as refused:
            record_alert_delivery(
                receipt_store(engine), alert.model_copy(update={"event_id": "e-2"})
            )
    assert refused.value.code == "UNAVAILABLE"
