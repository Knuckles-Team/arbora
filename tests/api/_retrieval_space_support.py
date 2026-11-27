"""Doubles for the interaction space tests: a recording retriever and plan decision."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops.retrieval import RetrievalHit, RetrievalSearchOutcome
from arbora.api.ops.retrieval_space import RetrievalPlan

Q32 = 1 << 32


def outcome(*refs: str) -> RetrievalSearchOutcome:
    return RetrievalSearchOutcome(
        tenant="tenant-a",
        hits=tuple(RetrievalHit(ref=ref, text=f"text {ref}", tokens=3) for ref in refs),
    )


class Retriever:
    """The Retriever port: answers each query with its scripted references."""

    def __init__(self, **answers: tuple[str, ...]) -> None:
        self.answers = answers
        self.queries: list[str] = []

    async def search(
        self, *, tenant: str, query: str, context_budget: int
    ) -> RetrievalSearchOutcome:
        self.queries.append(query)
        return outcome(*self.answers.get(query, ()))


class Plan:
    """The plan decision port: one fixed plan, recording each ask."""

    def __init__(self, plan: RetrievalPlan | None = None) -> None:
        self.plan_value = plan or RetrievalPlan(top_k=2, max_queries=3)
        self.asked: list[int] = []

    async def plan(self, context: Any, *, query_count: int) -> RetrievalPlan:
        self.asked.append(query_count)
        return self.plan_value


def space_context(**services: Any) -> Any:
    caller = SimpleNamespace(tenant="tenant-a", principal="alice", request_id="r-1")
    return SimpleNamespace(caller=caller, services=dict(services))


def serve_context_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned engine lists ``RetrievalContextSearch`` and the double answers it."""
    import medulla.generated as generated

    from arbora.pin_lag import medulla as pin_lag

    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"RetrievalContextSearch"},
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any) -> Any:
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


def span(document: str, start: int) -> dict[str, Any]:
    text = f"passage {document}"
    return {
        "document_id": document,
        "document_revision": 1,
        "context_start": 0,
        "context_end": len(text.encode()),
        "context_text": text,
        "match_start": start,
        "match_end": start + 1,
        "text_digest": "d" * 64,
    }
