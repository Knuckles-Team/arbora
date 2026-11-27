"""Three refusals run zero queries (ARBORA-OPS-R053.4)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.retrieval_space import RetrievalPlan, handle_retrieval_space
from tests.api._retrieval_space_support import Plan, Retriever, space_context

_PARAMS = {"queries": ["a", "b"], "context_budget": 100}


@pytest.mark.spec("ARBORA-OPS-R053.4")
async def test_three_refusals_run_zero_queries() -> None:
    retriever = Retriever(a=("x",), b=("y",))
    plan = Plan(RetrievalPlan(top_k=2, max_queries=1))
    ctx = space_context(retriever=retriever, retrieval_plan=plan)

    with pytest.raises(OperationRefused) as empty:
        await handle_retrieval_space(ctx, {**_PARAMS, "queries": []}, None)  # type: ignore[arg-type]
    assert empty.value.code == "INVALID_ARGUMENT"

    with pytest.raises(OperationRefused) as over:
        await handle_retrieval_space(ctx, _PARAMS, None)  # type: ignore[arg-type]
    assert over.value.code == "LOAD_CAP_EXCEEDED"
    assert over.value.details["loaded_items"] == 4

    bare = space_context(retrieval_plan=plan)
    with pytest.raises(OperationRefused) as unbound:
        await handle_retrieval_space(bare, _PARAMS, None)  # type: ignore[arg-type]
    assert unbound.value.code == "UNAVAILABLE"

    no_plan = space_context(retriever=retriever)
    with pytest.raises(OperationRefused) as undecided:
        await handle_retrieval_space(no_plan, _PARAMS, None)  # type: ignore[arg-type]
    assert undecided.value.code == "UNAVAILABLE"
    assert retriever.queries == []
