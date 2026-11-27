"""A served request reaches the composed retriever; a missing producer refuses (ARBORA-OPS-R053.3)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.registry_factory import get_registry
from arbora.api.ops.retrieval_space import (
    EngineRetrievalPlan,
    EngineRetriever,
    handle_retrieval_space,
)
from arbora.mcp_server.served_invocation import _with_retriever
from tests.api._retrieval_space_support import Plan, Retriever, space_context


@pytest.mark.spec("ARBORA-OPS-R053.3")
async def test_served_request_reaches_composed_retriever_and_missing_producer_refuses() -> (
    None
):
    op = get_registry()["retrieval.space"]
    assert op.scopes == frozenset({"memory:read"})

    composed = _with_retriever({"other": 1})
    assert composed is not None
    assert isinstance(composed["retriever"], EngineRetriever)
    assert isinstance(composed["retrieval_plan"], EngineRetrievalPlan)
    own = object()
    kept = _with_retriever({"retriever": own})
    assert kept is not None and kept["retriever"] is own
    assert _with_retriever(None) is None

    retriever = Retriever(a=("x", "y"), b=("y", "z"))
    plan = Plan()
    ctx = space_context(retriever=retriever, retrieval_plan=plan)
    answer = await handle_retrieval_space(
        ctx, {"queries": ["a", "b"], "context_budget": 50}, op
    )
    assert retriever.queries == ["a", "b"] and plan.asked == [2]
    assert answer["value"]["space"]["boundary"]["document_refs"] == ["x", "y", "z"]

    with pytest.raises(OperationRefused) as missing:
        await handle_retrieval_space(
            space_context(retrieval_plan=plan),
            {"queries": ["a"], "context_budget": 50},
            op,
        )
    assert missing.value.code == "UNAVAILABLE"
