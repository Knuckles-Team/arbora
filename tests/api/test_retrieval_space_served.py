"""Two queries yield the boundary and two directories through the engine adapters (ARBORA-OPS-R053, R053.6)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.registry_factory import get_registry
from arbora.api.ops.retrieval_space import (
    EngineRetrievalPlan,
    EngineRetriever,
    handle_retrieval_space,
)
from tests.api._question_support import served
from tests.api._retrieval_space_support import Q32, serve_context_search, span

_SPANS = {
    "alpha": [span("doc-1", 0), span("doc-2", 4)],
    "beta": [span("doc-2", 4), span("doc-3", 9)],
}


def _decide(knobs: dict[str, int] | None) -> Any:
    def answer(_params: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "outcome": {
                "outcome": "advisory",
                "scores": [
                    {"option_id": "space", "score": {"scale": "q32", "value": Q32}}
                ],
            }
        }
        if knobs is not None:
            record["knobs"] = knobs
        return {"records": [record]}

    return answer


def _served(monkeypatch: pytest.MonkeyPatch, knobs: dict[str, int] | None) -> Any:
    ctx, engine = served(
        monkeypatch,
        decide=_decide(knobs),
        services={
            "retriever": EngineRetriever(),
            "retrieval_plan": EngineRetrievalPlan(),
        },
    )
    serve_context_search(monkeypatch)
    engine.answers["RetrievalContextSearch"] = lambda p: {
        "spans": _SPANS[p["request"]["query"]]
    }
    return ctx, engine


@pytest.mark.spec("ARBORA-OPS-R053")
@pytest.mark.spec("ARBORA-OPS-R053.6")
async def test_two_queries_yield_boundary_and_two_directories(monkeypatch) -> None:
    op = get_registry()["retrieval.space"]
    ctx, engine = _served(monkeypatch, {"retrieval_space.top_k": 2})
    answer = await handle_retrieval_space(
        ctx, {"queries": ["alpha", "beta"], "context_budget": 100}, op
    )
    value = answer["value"]
    assert value["space"]["boundary"]["document_refs"] == [
        "doc-1@1:0-1",
        "doc-2@1:4-5",
        "doc-3@1:9-10",
    ]
    assert [d["name"] for d in value["space"]["directories"]] == ["q01", "q02"]
    assert value["hit_counts"] == [2, 2]
    methods = [method for method, *_ in engine.sent]
    assert methods == ["Decide", "RetrievalContextSearch", "RetrievalContextSearch"]


@pytest.mark.spec("ARBORA-OPS-R053.6")
async def test_an_absent_decision_uses_the_stated_fallback(monkeypatch) -> None:
    op = get_registry()["retrieval.space"]
    ctx, _engine = _served(monkeypatch, None)
    answer = await handle_retrieval_space(
        ctx, {"queries": ["alpha"], "context_budget": 100}, op
    )
    assert answer["value"]["space"]["boundary"]["top_k"] == 20


@pytest.mark.spec("ARBORA-OPS-R053.6")
async def test_missing_producer_contracts_stay_typed_unavailable(monkeypatch) -> None:
    op = get_registry()["retrieval.space"]
    ctx, engine = served(
        monkeypatch,
        decide=_decide(None),
        services={
            "retriever": EngineRetriever(),
            "retrieval_plan": EngineRetrievalPlan(),
        },
    )
    # The pinned engine does not list RetrievalContextSearch: the search refuses.
    with pytest.raises(OperationRefused) as refused:
        await handle_retrieval_space(
            ctx, {"queries": ["alpha"], "context_budget": 100}, op
        )
    assert refused.value.code == "UNAVAILABLE"
    assert [method for method, *_ in engine.sent] == ["Decide"]
