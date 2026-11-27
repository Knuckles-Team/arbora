"""The bounded interaction space (ARBORA-OPS-R053).

``retrieval.space`` returns a bounded interaction space rather than ranked
snippets: the corpus boundary is the union of the top-K hits of a multi-query
search, with one directory per query. The engine ranks (the ``Retriever`` port is
its search) and decides the retrieval plan (``Decide`` ``retrieval_plan``): K, the
query cap and the document ceiling. Arbora unions references in first-seen order
and names the directories; it ranks and scores nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops.retrieval import (
    RetrievalHit,
    RetrievalSearchOutcome,
    Retriever,
    request_retriever,
)
from arbora.api.registry import OpSpec, Verb

#: Used only when the engine decision carries no value (the rows' stated fallback).
DEFAULT_TOP_K = 20
DEFAULT_MAX_QUERIES = 8
_PLAN_KINDS = "retrieval_plan"
_KNOBS = (
    "retrieval_space.top_k",
    "retrieval_space.max_queries",
    "retrieval_space.document_ceiling",
)
#: What each hit property means; the directory carries it so the harness can navigate.
HIT_PROPERTIES = {
    "ref": "the provenance reference of one retrieved passage",
    "text": "the passage text the engine returned",
    "tokens": "the passage size in context-budget tokens",
}
#: The navigation commands a directory supports.
COMMANDS = ("list", "read", "search")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CorpusBoundary(_Frozen):
    document_refs: tuple[str, ...]
    top_k: int = Field(ge=1)


class QueryDirectory(_Frozen):
    name: str = Field(pattern=r"^q\d{2,}$")
    query: str = Field(min_length=1, max_length=4096)
    hit_refs: tuple[str, ...]
    properties: dict[str, str]
    commands: tuple[str, ...]
    toc_digests: tuple[str, ...] = ()


class InteractionSpace(_Frozen):
    space_id: str = Field(pattern=r"^space:[0-9a-f]{32}$")
    boundary: CorpusBoundary
    directories: tuple[QueryDirectory, ...]


class RetrievalPlan(_Frozen):
    """The engine's retrieval-plan decision, or the stated fallback when it is absent."""

    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=1000)
    max_queries: int = Field(default=DEFAULT_MAX_QUERIES, ge=1, le=64)
    #: The most documents one boundary may hold; absent, every query's full top-K.
    document_ceiling: int | None = Field(default=None, ge=1)

    @property
    def ceiling(self) -> int:
        return self.document_ceiling or self.top_k * self.max_queries


@runtime_checkable
class RetrievalPlanDecision(Protocol):
    """What the engine decides before any query runs."""

    async def plan(self, context: Any, *, query_count: int) -> RetrievalPlan: ...


def build_space(
    queries: Sequence[str],
    outcomes: Sequence[RetrievalSearchOutcome],
    plan: RetrievalPlan,
) -> InteractionSpace:
    """Union the top-K hit references in first-seen order; one directory per query."""
    directories: list[QueryDirectory] = []
    boundary: dict[str, None] = {}
    for index, (query, outcome) in enumerate(zip(queries, outcomes, strict=True), 1):
        refs = tuple(hit.ref for hit in outcome.hits[: plan.top_k])
        boundary.update(dict.fromkeys(refs))
        directories.append(
            QueryDirectory(
                name=f"q{index:02d}",
                query=query,
                hit_refs=refs,
                properties=dict(HIT_PROPERTIES),
                commands=COMMANDS,
            )
        )
    document_refs = tuple(boundary)
    seed = json.dumps([list(queries), list(document_refs)], separators=(",", ":"))
    return InteractionSpace(
        space_id="space:" + hashlib.sha256(seed.encode()).hexdigest()[:32],
        boundary=CorpusBoundary(document_refs=document_refs, top_k=plan.top_k),
        directories=tuple(directories),
    )


class EngineRetrievalPlan:
    """The engine ``Decide`` ``retrieval_plan`` question and its knob values."""

    async def plan(self, context: Any, *, query_count: int) -> RetrievalPlan:
        from medulla import decision_client as dc

        from arbora.api.ops._question import decide_question, question_request
        from arbora.api.ops.plans import _feature_schema

        request = question_request(
            str(context.caller.tenant),
            "retrieval.space.plan",
            _PLAN_KINDS,
            dc.declared([dc.DeclaredOption("space")]),
            await _feature_schema(context, str(context.caller.tenant)),
            [dc.param("query_count", "int", query_count)],
        )
        record = await decide_question(context, _PLAN_KINDS, request)
        knobs = record.get("knobs")
        values = knobs if isinstance(knobs, Mapping) else {}
        wanted = dict(
            zip(("top_k", "max_queries", "document_ceiling"), _KNOBS, strict=True)
        )
        chosen = {
            name: int(values[knob]) for name, knob in wanted.items() if knob in values
        }
        return RetrievalPlan(**chosen)


class _RequestRetriever:
    """One request's ``Retriever``: the engine search under the caller's own session."""

    def __init__(self, context: Any) -> None:
        self._context = context

    async def search(
        self, *, tenant: str, query: str, context_budget: int
    ) -> RetrievalSearchOutcome:
        from arbora.api.ops import _engine
        from arbora.api.ops._engine import engine_send
        from arbora.api.ops.retrieval import MAX_SPANS
        from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

        try:
            require_methods("RetrievalContextSearch")
        except PinLagUnavailable as missing:
            raise refusal(missing) from missing
        request = {
            "schema_version": 1,
            "tenant_id": tenant,
            "graph": await _engine.caller_dispatch_graph(self._context),
            "query": query,
            "ngram_n": 3,
            "max_spans": MAX_SPANS,
        }
        answer = await engine_send(
            self._context, "RetrievalContextSearch", {"request": request}
        )
        spans = answer.get("spans") if isinstance(answer, Mapping) else None
        if not isinstance(spans, list):
            raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
        return RetrievalSearchOutcome(
            tenant=tenant, hits=tuple(_hit(span) for span in spans)
        )

    async def freshness(self, *, tenant: str) -> Any:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine publishes no source freshness read"}
        )


def _hit(span: Mapping[str, Any]) -> RetrievalHit:
    text = str(span["context_text"])
    return RetrievalHit(
        ref=f"{span['document_id']}@{span['document_revision']}"
        f":{span['match_start']}-{span['match_end']}",
        text=text,
        # Budget accounting only: one token per four bytes, never a ranking input.
        tokens=max(1, (len(text.encode()) + 3) // 4),
    )


class EngineRetriever:
    """The engine-backed ``Retriever``; each request binds it to its own session."""

    def bind(self, context: Any) -> Retriever:
        return _RequestRetriever(context)


class RetrievalSpaceParams(Params):
    queries: list[str] = Field(default_factory=list, max_length=64)
    context_budget: int = Field(default=4096, ge=1, le=1_000_000)


class RetrievalSpaceResult(Params):
    value: dict[str, Any]


async def handle_retrieval_space(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Plan, refuse before any query, then search once per query and build the space."""
    queries = [str(query) for query in params["queries"]]
    if not queries or any(not query.strip() for query in queries):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "queries"})
    retriever = request_retriever(context)
    decision: RetrievalPlanDecision = bound_service(
        context, "retrieval_plan", reason="retrieval plan decision is not composed"
    )
    plan = await decision.plan(context, query_count=len(queries))
    potential = len(queries) * plan.top_k
    if len(queries) > plan.max_queries or potential > plan.ceiling:
        raise OperationRefused("LOAD_CAP_EXCEEDED", {"loaded_items": potential})
    outcomes = [
        await retriever.search(
            tenant=str(context.caller.tenant),
            query=query,
            context_budget=params["context_budget"],
        )
        for query in queries
    ]
    space = build_space(queries, outcomes, plan)
    return {
        "value": {
            "space": space.model_dump(mode="json"),
            "query_count": len(queries),
            "boundary_size": len(space.boundary.document_refs),
            "hit_counts": [len(d.hit_refs) for d in space.directories],
            "context_budget": params["context_budget"],
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.space",
            summary="Build a bounded interaction space from a multi-query search",
            examples=("build a retrieval space for these questions",),
            params=RetrievalSpaceParams,
            result=RetrievalSpaceResult,
            handler="arbora.api.ops.retrieval_space.handle_retrieval_space",
            scope="memory:read",
        ),
    )


specs = operations

__all__ = [
    "CorpusBoundary",
    "EngineRetrievalPlan",
    "EngineRetriever",
    "InteractionSpace",
    "QueryDirectory",
    "RetrievalPlan",
    "RetrievalPlanDecision",
    "RetrievalSpaceParams",
    "build_space",
    "handle_retrieval_space",
    "operations",
    "specs",
]
