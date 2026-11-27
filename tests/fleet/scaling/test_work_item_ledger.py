"""The WorkItem ledger's fences: claim, lease and expiry, and identity by idempotency key."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from arbora import work_items
from arbora.fleet.scaling.scaling_intent_reconciler import (
    ScaleActuationResult,
    ScaleIntentConflict,
    ScaleIntentState,
    ScaleLeaseLost,
    ScaleLeaseUnavailable,
)
from arbora.fleet.scaling.work_item_ledger import (
    KIND,
    WorkItemScaleIntentLedger,
    _item_id,
)
from tests.fleet.scaling._work_item_engine import FakeWorkItems, Session, engine
from tests.fleet.scaling.test_scaling_intent_reconciler import NOW, TARGET, intent

pytestmark = pytest.mark.spec("ARBORA-FLEET-R024.14")


@pytest.fixture
def ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkItemScaleIntentLedger, FakeWorkItems]:
    monkeypatch.setattr(work_items, "_session", Session)
    served, fake = engine()
    return WorkItemScaleIntentLedger(served), fake


def _done(
    record: Any, *, retryable: bool = False, ok: bool = True
) -> ScaleActuationResult:
    return ScaleActuationResult(
        execution_key=record.execution_key,
        target=record.target,
        state=ScaleIntentState.SUCCEEDED if ok else ScaleIntentState.FAILED,
        scaled=ok,
        retryable=retryable,
        error_code=None if ok else "temporarily_unavailable",
        observed_replicas=record.desired_replicas if ok else None,
    )


async def test_one_controller_holds_the_lease_until_it_expires(ledger: Any) -> None:
    store, fake = ledger
    record = intent()
    await store.persist_intent(record)
    mine = await store.acquire_controller_lease(record, "writer-1", now=NOW)
    assert mine is not None
    assert await store.acquire_controller_lease(record, "writer-1", now=NOW) == mine
    assert await store.acquire_controller_lease(record, "writer-2", now=NOW) is None
    fake.expire_lease(_item_id(KIND, record.execution_key))
    later = await store.acquire_controller_lease(
        record, "writer-2", now=NOW + timedelta(minutes=5)
    )
    assert later is not None and later.lease_instance_id != mine.lease_instance_id


async def test_a_reclaimed_lease_fences_the_old_holder(ledger: Any) -> None:
    store, fake = ledger
    record = intent()
    persisted = await store.persist_intent(record)
    old = await store.acquire_controller_lease(record, "writer-1", now=NOW)
    assert old is not None
    fake.expire_lease(_item_id(KIND, record.execution_key))
    new = await store.acquire_controller_lease(record, "writer-2", now=NOW)
    assert new is not None
    with pytest.raises(ScaleLeaseLost):
        await store.mark_started(record, persisted.execution, old, now=NOW)
    with pytest.raises(ScaleLeaseLost):
        await store.record_result(
            record, persisted.execution, old, _done(record), now=NOW
        )
    started = await store.mark_started(record, persisted.execution, new, now=NOW)
    assert started.state == ScaleIntentState.STARTED.value


async def test_an_expired_lease_cannot_write(ledger: Any) -> None:
    store, _ = ledger
    record = intent()
    persisted = await store.persist_intent(record)
    lease = await store.acquire_controller_lease(record, "writer-1", now=NOW)
    assert lease is not None
    with pytest.raises(ScaleLeaseLost, match="expired"):
        await store.mark_started(
            record, persisted.execution, lease, now=lease.expires_at
        )


async def test_the_engine_refusing_the_effect_marker_stops_the_controller(
    ledger: Any,
) -> None:
    store, fake = ledger
    record = intent()
    persisted = await store.persist_intent(record)
    lease = await store.acquire_controller_lease(record, "writer-1", now=NOW)
    assert lease is not None
    fake.refuse_effect_marker = True
    with pytest.raises(ScaleLeaseLost, match="effect marker"):
        await store.mark_started(record, persisted.execution, lease, now=NOW)


async def test_a_retryable_failure_releases_the_claim_for_a_newer_lease(
    ledger: Any,
) -> None:
    store, fake = ledger
    record = intent()
    persisted = await store.persist_intent(record)
    first = await store.acquire_controller_lease(record, "writer-1", now=NOW)
    assert first is not None
    started = await store.mark_started(record, persisted.execution, first, now=NOW)
    await store.record_result(
        record, started, first, _done(record, retryable=True, ok=False), now=NOW
    )
    assert "defer" in fake.events
    second = await store.acquire_controller_lease(record, "writer-1", now=NOW)
    assert second is not None and second.attempt == 2
    assert second.lease_instance_id != first.lease_instance_id


async def test_a_changed_identity_for_one_key_or_revision_conflicts(
    ledger: Any,
) -> None:
    store, _ = ledger
    await store.persist_intent(intent())
    replay = await store.persist_intent(intent())
    assert replay.replayed is True
    other = {**TARGET, "target_uid": "uid-2", "target_resource_version": "rv-2"}
    with pytest.raises(ScaleIntentConflict):
        await store.persist_intent(intent(target=other))
    with pytest.raises(ScaleIntentConflict):
        await store.persist_intent(
            intent(execution_key="scale-execution-2", target=other)
        )


async def test_the_fence_expiry_refuses_a_lease(ledger: Any) -> None:
    store, _ = ledger
    record = intent()
    await store.persist_intent(record)
    with pytest.raises(ScaleLeaseUnavailable):
        await store.acquire_controller_lease(
            record, "writer-1", now=record.fence.expires_at
        )
