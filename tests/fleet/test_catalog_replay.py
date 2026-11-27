"""ARBORA-FLEET-R022: catalog changes replay from durable receipts."""

from __future__ import annotations

import pytest

from arbora.fleet.catalog_replay import (
    CatalogDeltaReceipt,
    CatalogReplayRefused,
    next_position,
    plan_deltas,
    replay_deltas,
)


def _r(
    seq: int, item: str, op: str = "upsert", digest: str = "d1"
) -> CatalogDeltaReceipt:
    return CatalogDeltaReceipt("acme", 1, seq, item, op, digest)  # type: ignore[arg-type]


SEQUENCE = [_r(1, "a"), _r(2, "b"), _r(3, "a", digest="d2"), _r(4, "b", "remove")]


@pytest.mark.spec("ARBORA-FLEET-R022")
def test_replay_twice_same_state() -> None:
    state: dict[str, str] = {}
    applied: set[tuple[str, int, int, str]] = set()
    assert replay_deltas(SEQUENCE, state=state, applied=applied) == 4
    once = dict(state)
    assert once == {"a": "d2"}
    assert replay_deltas(SEQUENCE, state=state, applied=applied) == 0  # a no-op
    assert state == once
    fresh: dict[str, str] = {}
    replay_deltas(reversed(SEQUENCE), state=fresh, applied=set())  # order is by key
    assert fresh == once


@pytest.mark.spec("ARBORA-FLEET-R022")
def test_gap_and_digest_mismatch_refuse() -> None:
    with pytest.raises(CatalogReplayRefused) as gap:
        replay_deltas([_r(1, "a"), _r(3, "b")], state={}, applied=set())
    assert gap.value.code == "CATALOG_REPLAY_GAP"
    with pytest.raises(CatalogReplayRefused) as bad:
        replay_deltas(
            [_r(1, "a", digest="d1")],
            state={},
            applied=set(),
            stored_digest=lambda _item: "other",
        )
    assert bad.value.code == "CATALOG_REPLAY_DIGEST_MISMATCH"


@pytest.mark.spec("ARBORA-FLEET-R022")
def test_plan_covers_upserts_and_removals_in_order() -> None:
    planned = plan_deltas(
        "acme", {"a": "d1", "gone": "d9"}, {"a": "d2", "n": "d3"}, (1, 5)
    )
    assert [(r.sequence, r.item_id, r.op) for r in planned] == [
        (5, "a", "upsert"),
        (6, "n", "upsert"),
        (7, "gone", "remove"),
    ]
    assert next_position(planned) == (1, 8)
