"""Replay ledger primitive for ARBORA-FLEET-R022 (unbound: engine event-log source not wired)."""

from __future__ import annotations

import pytest

from arbora.fleet.catalog_replay import (
    CatalogReplayLedger,
    DeltaOutOfOrderError,
    DeltaReceipt,
)


def _r(seq: int, item: str, kind="tool") -> DeltaReceipt:
    return DeltaReceipt("t1", 1, seq, item, kind, f"d{seq}")


def test_replaying_a_delta_sequence_twice_is_idempotent_and_ordered() -> None:
    ledger = CatalogReplayLedger()
    seen: list[tuple[int, str]] = []
    deltas = [
        _r(2, "p1", "prompt"),
        _r(1, "a"),
        _r(3, "s1", "skill"),
        _r(4, "r1", "resource"),
    ]

    first = ledger.replay(deltas, lambda r: seen.append((r.sequence, r.item_id)))
    second = ledger.replay(deltas, lambda r: seen.append((r.sequence, r.item_id)))

    assert (first, second) == (4, 0)
    assert seen == [(1, "a"), (2, "p1"), (3, "s1"), (4, "r1")]


def test_gap_in_sequence_is_refused_without_applying() -> None:
    ledger = CatalogReplayLedger()
    seen: list[int] = []
    ledger.replay([_r(1, "a")], lambda r: seen.append(r.sequence))
    with pytest.raises(DeltaOutOfOrderError):
        ledger.replay([_r(3, "c")], lambda r: seen.append(r.sequence))
    assert seen == [1]
    assert (
        ledger.replay([_r(2, "b"), _r(3, "c")], lambda r: seen.append(r.sequence)) == 2
    )
