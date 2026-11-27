"""Tests for the recurring DCA plan model and non-duplicating proposal key
(ARBORA-DATA-MARKET-R006; FI-10 in
``specs/data-and-market-projections/test-spec.md``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from arbora.api.domain.finance.dca import (
    DCAPlan,
    build_dca_proposal_key,
    record_dca_proposal,
)
from arbora.api.domain.finance.receipts import ServedReceiptEngine
from tests.finance._receipt_engine import ReceiptMachine


def _store() -> ServedReceiptEngine:
    return ReceiptMachine().engine()


def _plan(**overrides: object) -> DCAPlan:
    base: dict[str, object] = {
        "plan_id": "plan-1",
        "revision": 0,
        "owner": "operator-1",
        "tenant_id": "tenant-a",
        "symbol": "VTI",
        "account_id": "acct-1",
        "amount": Decimal("100.00"),
        "currency": "USD",
        "timezone": "America/Chicago",
        "recurrence_rule": "FREQ=WEEKLY",
    }
    base.update(overrides)
    return DCAPlan.model_validate(base)


async def test_first_proposal_for_an_occurrence_is_proposed() -> None:
    store = _store()

    outcome = await record_dca_proposal(store, _plan(), "2026-11-01")

    assert outcome == "proposed"


async def test_clock_drift_replay_of_the_same_occurrence_is_a_duplicate() -> None:
    """FI-10: replaying the same due occurrence under the same plan
    revision -- from clock drift or a retried trigger -- never produces a
    second proposal."""

    store = _store()
    plan = _plan()
    await record_dca_proposal(store, plan, "2026-11-01")

    second = await record_dca_proposal(store, plan, "2026-11-01")

    assert second == "duplicate"


async def test_a_later_revision_proposes_independently_of_an_earlier_one() -> None:
    """The proposal key is derived from the plan revision, so a revised
    plan's proposals never collide with its earlier revision's proposals
    for the same nominal occurrence."""

    store = _store()
    revision_0 = _plan(revision=0)
    revision_1 = _plan(revision=1)

    first = await record_dca_proposal(store, revision_0, "2026-11-01")
    second = await record_dca_proposal(store, revision_1, "2026-11-01")

    assert first == "proposed"
    assert second == "proposed"
    assert build_dca_proposal_key(revision_0, "2026-11-01") != build_dca_proposal_key(
        revision_1, "2026-11-01"
    )


async def test_zero_amount_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(amount=Decimal("0"))


async def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(live_order_id="order-1")
