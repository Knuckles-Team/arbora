"""Owner-published live graph limits (ARBORA-OPS-R062.6)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import graph_limits

pytestmark = pytest.mark.spec("ARBORA-OPS-R062.6")

GOOD = {
    "profile_revision": 1,
    "contract_revision": "c1",
    "workload": "2 editors, 500 nodes",
    "commit_to_visible_p95_ms": 1000,
    "frame_p95_ms": 50.0,
    "layout_settle_ms": 2000,
}


def _handler(monkeypatch: pytest.MonkeyPatch, answer: Any):
    async def send(context, method, params, **_):
        assert method == "GraphLiveLimits" and params == {}
        return answer

    monkeypatch.setattr(graph_limits, "engine_send", send)
    return graph_limits.handle_graph_limits_read(
        SimpleNamespace(), {}, graph_limits.operations()[0]
    )


@pytest.mark.asyncio
async def test_limits_forward_unchanged_and_invalid_values_are_unavailable(
    monkeypatch,
) -> None:
    result = await _handler(monkeypatch, GOOD)
    assert result["value"]["commit_to_visible_p95_ms"] == 1000.0
    assert result["value"]["workload"] == GOOD["workload"]
    for bad in (
        {**GOOD, "frame_p95_ms": 0},
        {**GOOD, "layout_settle_ms": float("inf")},
        {**GOOD, "commit_to_visible_p95_ms": float("nan")},
        {**GOOD, "commit_to_visible_p95_ms": True},
        {**GOOD, "workload": ""},
        {k: v for k, v in GOOD.items() if k != "profile_revision"},
        None,
    ):
        with pytest.raises(OperationRefused) as refused:
            await _handler(monkeypatch, bad)
        assert refused.value.code == "UNAVAILABLE"


def test_operation_is_registered_read_only() -> None:
    [op] = graph_limits.operations()
    assert op.id == "graph.limits.read"
