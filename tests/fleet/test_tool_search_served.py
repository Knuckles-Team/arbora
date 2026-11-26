"""Tool search order equals the engine's sealed ranking (ARBORA-OPS-R866)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.multiplexer_ops import MultiplexerOps


class _Catalog:
    async def search(self, caller, **params):
        return {"items": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}


def _ops(ranker):
    ops = MultiplexerOps.__new__(MultiplexerOps)
    ops.catalog = _Catalog()
    ops.sessions = SimpleNamespace(pinned_items=lambda key: ())
    ops._session_key_for = lambda caller: None
    ops._prime = None
    ops.tool_ranker = ranker
    return ops


@pytest.mark.spec("ARBORA-OPS-R866")
async def test_order_equals_sealed_record() -> None:
    seen: dict = {}

    async def ranker(caller, **kwargs):
        seen.update(kwargs)
        return ["c", "a", "b"]

    ops = _ops(ranker)
    ranked = await ops.find_tools(None, query="deploy", context_budget_tokens=500)
    assert [row["id"] for row in ranked["items"]] == ["c", "a", "b"]
    assert seen["query"] == "deploy" and seen["context_budget_tokens"] == 500
    browse = await ops.find_tools(None)
    assert [row["id"] for row in browse["items"]] == ["a", "b", "c"]

    async def down(caller, **kwargs):
        raise RuntimeError("engine down")

    with pytest.raises(OperationRefused) as refused:
        await _ops(down).find_tools(None, query="deploy")
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details == {"reason": "tool_ranking_unavailable"}
