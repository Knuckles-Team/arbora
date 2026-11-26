"""The sources-used-and-why block is served unaltered (ARBORA-FLEET-R052.3)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import require_sources_block

BLOCK = [
    {"source": "ledger", "why": "owns the record", "rank": 1},
    {"source": "wiki", "why": "mentions it", "rank": 2},
]


@pytest.mark.spec("ARBORA-FLEET-R052.3")
def test_block_present_unaltered_and_in_engine_order() -> None:
    reply = {"rows": [1], "audit": BLOCK}
    assert require_sources_block(reply) is reply
    assert reply["audit"] == BLOCK and reply["audit"][0]["source"] == "ledger"


@pytest.mark.spec("ARBORA-FLEET-R052.3")
@pytest.mark.parametrize(
    "reply", [{"rows": []}, {"audit": []}, {"audit": None}, [], None]
)
def test_missing_block_is_refused(reply: object) -> None:
    with pytest.raises(OperationRefused) as caught:
        require_sources_block(reply)
    assert caught.value.code == "SOURCES_BLOCK_MISSING"
