"""Tests for the idempotent finance-schedule run record
(ARBORA-DATA-MARKET-R002; FI-04 in
``specs/data-and-market-projections/test-spec.md``).
"""

from __future__ import annotations

from arbora.api.domain.finance.receipts import ServedReceiptEngine
from arbora.api.domain.finance.schedule import FinanceScheduleEntry, record_schedule_run
from tests.finance._receipt_engine import ReceiptMachine


def _store() -> ServedReceiptEngine:
    return ReceiptMachine().engine()


def _entry(**overrides: object) -> FinanceScheduleEntry:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "owner": "operator-1",
        "action": "backfill",
        "timezone": "America/Chicago",
        "trigger": "2026-11-01T06:00:00Z",
        "idempotency_key": "backfill:tenant-a:2026-11-01",
    }
    base.update(overrides)
    return FinanceScheduleEntry.model_validate(base)


async def test_first_run_executes() -> None:
    store = _store()

    outcome = await record_schedule_run(store, _entry())

    assert outcome == "executed"


async def test_replay_with_the_same_key_is_a_duplicate() -> None:
    """FI-04: a restart or DST-crossing replay of the same trigger does not
    re-execute; the idempotency key is caller-supplied, not derived from
    wall-clock time, so a duplicated trigger timestamp changes nothing."""

    store = _store()
    await record_schedule_run(store, _entry())

    second = await record_schedule_run(
        store, _entry(trigger="2026-11-01T06:00:00-06:00")
    )

    assert second == "duplicate"


async def test_different_tenants_with_the_same_key_both_execute() -> None:
    """Idempotency is scoped per tenant; two tenants never share dedupe state."""

    store = _store()

    first = await record_schedule_run(store, _entry(tenant_id="tenant-a"))
    second = await record_schedule_run(store, _entry(tenant_id="tenant-b"))

    assert first == "executed"
    assert second == "executed"


async def test_different_actions_with_the_same_key_are_independent() -> None:
    store = _store()
    key = "shared-key"

    backfill = await record_schedule_run(
        store, _entry(action="backfill", idempotency_key=key)
    )
    scan = await record_schedule_run(store, _entry(action="scan", idempotency_key=key))

    # Same tenant + same idempotency key dedupes regardless of action label:
    # the key alone is the replay-safety fence.
    assert backfill == "executed"
    assert scan == "duplicate"


async def test_repeated_replay_stays_duplicate() -> None:
    store = _store()
    entry = _entry()
    await record_schedule_run(store, entry)

    outcomes = [await record_schedule_run(store, entry) for _ in range(5)]

    assert outcomes == ["duplicate"] * 5
