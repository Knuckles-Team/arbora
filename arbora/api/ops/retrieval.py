"""Retrieval read operations (ARBORA-OPS-R021.2.1, retrieval slice).

``retrieval.search`` runs a context-budgeted retrieval through one typed port
under the caller's own tenant and authority; there is no local cache
substitute. An uncomposed retriever fails closed with a typed ``UNAVAILABLE``
refusal, matching :mod:`arbora.api.ops.ingest`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

import msgpack
from pydantic import BaseModel, ConfigDict, Field

from arbora.api.ops._common import Params, bound_service, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send
from arbora.api.registry import AuditClass, OpSpec, Verb
from arbora.pin_lag.medulla import guard_field, send_main


class RetrievalSearchParams(Params):
    query: str = Field(min_length=1, max_length=4096)
    context_budget: int = Field(default=4096, ge=1, le=1_000_000)
    #: A committed decision record whose ``retrieval_binding`` names the one provider
    #: to run (ARBORA-OPS-R864); absent, the composed retriever answers.
    decision_record_id: str | None = Field(default=None, min_length=1, max_length=256)


class RetrievalHit(BaseModel):
    """One retrieved passage with its provenance reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    text: str
    tokens: int = Field(ge=0)


class RetrievalSearchOutcome(BaseModel):
    """Budget-bounded hits for one tenant-scoped query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    hits: tuple[RetrievalHit, ...]


class RetrievalSearchResult(Params):
    value: dict[str, Any]


class RetrievalFreshnessParams(Params):
    """``retrieval.freshness`` takes no parameters; the tenant is the caller's."""


class RetrievalFreshnessOutcome(BaseModel):
    """Freshness of one tenant's retrieval sources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    sources: dict[str, str]


class RetrievalFreshnessResult(Params):
    value: dict[str, Any]


@runtime_checkable
class Retriever(Protocol):
    """The one typed port Arbora calls the engine retrieval through."""

    async def search(
        self, *, tenant: str, query: str, context_budget: int
    ) -> RetrievalSearchOutcome: ...

    async def freshness(self, *, tenant: str) -> RetrievalFreshnessOutcome: ...


def request_retriever(context: Any) -> Retriever:
    """The composed retriever for this request; a request-bound one binds its session."""
    service = bound_service(context, "retriever", reason="retriever is not composed")
    bind = getattr(service, "bind", None)
    return bind(context) if callable(bind) else service  # type: ignore[no-any-return]


async def _bound_retriever(context: Any, record_id: str) -> Retriever:
    """The one provider the committed record binds, or a refusal that calls no other."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops._decide import committed_record

    binding = (await committed_record(context, record_id)).get("retrieval_binding")
    if not isinstance(binding, Mapping) or not binding.get("provider"):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "decision_record_id"})
    registry = context.services.get("retrievers") or {}
    provider = registry.get(binding["provider"])
    if provider is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "bound_provider_unavailable"})
    if getattr(provider, "config_digest", None) != binding.get("config_digest"):
        raise OperationRefused("POLICY_DENIED", {"reason": "bound_provider_mismatch"})
    return provider  # type: ignore[no-any-return]


async def handle_retrieval_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return budget-bounded hits for the caller's own tenant."""
    record_id = params.get("decision_record_id")
    retriever: Retriever = (
        await _bound_retriever(context, record_id)
        if record_id
        else request_retriever(context)
    )
    context_budget = params["context_budget"]
    outcome = await retriever.search(
        tenant=context.caller.tenant,
        query=params["query"],
        context_budget=context_budget,
    )
    kept: list[RetrievalHit] = []
    used = 0
    for hit in outcome.hits:
        if used + hit.tokens > context_budget:
            break
        used += hit.tokens
        kept.append(hit)
    bounded = outcome.model_copy(update={"hits": tuple(kept)})
    return {"value": bounded.model_dump(mode="json")}


async def handle_retrieval_freshness(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return source freshness for the caller's own tenant."""
    retriever = request_retriever(context)
    outcome = await retriever.freshness(tenant=context.caller.tenant)
    return {"value": outcome.model_dump(mode="json")}


class RetrievalDocumentsListParams(Params):
    """One source document's listing, each entry carrying its table-of-contents digest."""

    document_ids: list[str] = Field(min_length=1, max_length=100)


class RetrievalSpanReadParams(Params):
    document_id: str = Field(min_length=1, max_length=512)
    revision: int = Field(ge=0)
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    toc_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


async def _read_span(
    context: Any, request: dict[str, Any], *, with_toc: bool
) -> dict[str, Any]:
    """One engine ``ReadSpan``; the digest field is an engine ``main`` field the pin lacks."""
    models = ("MethodReadSpanParams",)
    if with_toc:
        guard_field("MethodReadSpanParams", "toc_digest")
    answer = await send_main(context, "ReadSpan", request, models=models)
    return dict(answer) if isinstance(answer, Mapping) else {}


async def handle_retrieval_documents_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List documents with the ``toc_digest`` the engine returned; arbora builds no contents table."""
    entries = []
    for document_id in params["document_ids"]:
        span = await _read_span(
            context,
            {"document_id": document_id, "revision": 0, "line_start": 0, "line_end": 0},
            with_toc=True,
        )
        entries.append(
            {"document_id": document_id, "toc_digest": span.get("toc_digest")}
        )
    return {"value": {"documents": entries}}


async def handle_retrieval_span_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read one span, forwarding ``toc_digest``; a stale digest is the engine's refusal."""
    request = {
        key: params[key]
        for key in ("document_id", "revision", "line_start", "line_end", "toc_digest")
    }
    return {"value": await _read_span(context, request, with_toc=True)}


FeedbackKind = Literal["upvote", "downvote", "cited", "ignored"]


class RetrievalFeedbackParams(Params):
    """One feedback event; the principal is the admitted caller, never a body field."""

    document_ref: str = Field(min_length=1, max_length=512)
    kind: FeedbackKind
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class KnowledgeScopeSetParams(Params):
    """The graph sources one agent may retrieve from (ARBORA-OPS-R854)."""

    agent_id: str = Field(min_length=1, max_length=256)
    sources: list[str] = Field(max_length=256)


class RetrievalWriteResult(Params):
    value: dict[str, Any]


async def handle_retrieval_feedback(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Record one feedback event; ranking stays in the engine."""
    answer = await send_main(
        context,
        "RetrievalFeedbackRecord",
        {
            "document_ref": params["document_ref"],
            "kind": params["kind"],
            "weight": float(params.get("weight", 1.0)),
        },
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_agent_knowledge_scope_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Write ``knowledgeScope`` on the agent's definition node (``MEDULLA-ORCH-VOCAB-R808``).

    The typed admission refuses ``SCOPE_SET_UNKNOWN`` for a set the writer's graph does
    not hold; a scope only narrows, so there is no widening refusal to build.
    """
    answer = await engine_send(
        context,
        "CompareAndSetNodeFields",
        {
            "node_id": params["agent_id"],
            "conditions_msgpack": msgpack.packb({}),
            "updates_msgpack": msgpack.packb(
                {"knowledgeScope": list(params["sources"])}
            ),
        },
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "retrieval.feedback": "medulla:RetrievalFeedbackRecord",
    "agent.knowledge_scope.set": "medulla:CompareAndSetNodeFields",
}


# --- retrieval evaluation and authorized context search (ARBORA-OPS-R063) ----


class RetrievalPinned(Params):
    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    digest: str = Field(min_length=1, max_length=128)


class RetrievalLabelPair(Params):
    query_id: str = Field(min_length=1, max_length=256)
    document_id: str = Field(min_length=1, max_length=256)


class RetrievalRanking(Params):
    query_id: str = Field(min_length=1, max_length=256)
    document_ids: list[str] = Field(max_length=1000)


class RetrievalAlgorithmRun(Params):
    algorithm_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    settings_digest: str = Field(min_length=1, max_length=128)
    corpus_digest: str = Field(min_length=1, max_length=128)
    input_digest: str = Field(min_length=1, max_length=128)
    rankings: list[RetrievalRanking] = Field(min_length=1)


class RetrievalCompareParams(Params):
    """One evaluation: shared pinned inputs and at least two algorithm runs."""

    corpus: RetrievalPinned
    labels: RetrievalPinned
    label_pairs: list[RetrievalLabelPair] = Field(min_length=1, max_length=100_000)
    queries: RetrievalPinned
    query_ids: list[str] = Field(min_length=1, max_length=100_000)
    k: int = Field(ge=1, le=1000)
    evaluator_revision: int = Field(ge=0)
    algorithms: list[RetrievalAlgorithmRun] = Field(min_length=2, max_length=16)


class RetrievalContextSearchParams(Params):
    query: str = Field(min_length=1, max_length=4096)
    ngram_n: int = Field(default=3, ge=1, le=16)
    max_spans: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class RetrievalEvalResult(Params):
    value: dict[str, Any]


MAX_SPAN_BYTES = 8192
MAX_SPANS = 100


def _check_runs(params: Mapping[str, Any]) -> None:
    """Refuse inputs that disagree before anything is sent to the engine."""
    from arbora.api.invoke.pipeline import OperationRefused

    queries = set(params["query_ids"])
    labelled = {pair["query_id"] for pair in params["label_pairs"]}
    if not labelled or not queries <= labelled:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "label_pairs"})
    algorithms = params["algorithms"]
    if len({run["algorithm_id"] for run in algorithms}) != len(algorithms):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "algorithms"})
    for run in algorithms:
        if run["corpus_digest"] != params["corpus"]["digest"]:
            raise OperationRefused("PLAN_STALE", {"field": "corpus_digest"})
        if {row["query_id"] for row in run["rankings"]} != queries:
            raise OperationRefused("INVALID_ARGUMENT", {"field": "rankings"})


def _comparison_request(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """The engine request for one checked comparison."""
    import hashlib
    import json

    _check_runs(params)
    request = {
        "schema_version": 1,
        "tenant_id": str(context.caller.tenant),
        "corpus": dict(params["corpus"]),
        "labels": {
            "pinned": dict(params["labels"]),
            "pairs": list(params["label_pairs"]),
        },
        "queries": {
            "pinned": dict(params["queries"]),
            "query_ids": list(params["query_ids"]),
        },
        "k": params["k"],
        "evaluator_revision": params["evaluator_revision"],
        "algorithms": [dict(run) for run in params["algorithms"]],
    }
    canonical = json.dumps(request, sort_keys=True, default=list).encode()
    request["idempotency_key"] = hashlib.sha256(canonical).hexdigest()
    return request


async def handle_retrieval_compare(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward one ranking submission; the engine computes every number."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

    try:
        require_models(("DecisionEvalOpRetrievalRankingSubmit",))
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    request = _comparison_request(context, params)
    answer = await engine_send(
        context,
        "DecisionEval",
        {"op": {"op": "retrieval_ranking_submit", "request": request}},
        idempotent=False,
    )
    if not isinstance(answer, Mapping) or "scores" not in answer:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    return {"value": dict(answer)}


def _checked_span(span: Mapping[str, Any]) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    text = span.get("context_text", "")
    start, end = span.get("context_start", -1), span.get("context_end", -1)
    inside = (
        0 <= start <= span.get("match_start", -1) <= span.get("match_end", -1) <= end
    )
    if not (
        isinstance(text, str)
        and inside
        and len(text.encode()) == end - start
        and end - start <= MAX_SPAN_BYTES
    ):
        raise OperationRefused("UNAVAILABLE", {"reason": "invalid byte map"})


async def handle_retrieval_context_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Authorized context spans over original bytes; a bad byte map refuses the page."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

    try:
        require_methods("RetrievalContextSearch")
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    from arbora.api.ops import _engine

    request: dict[str, Any] = {
        "schema_version": 1,
        "tenant_id": str(context.caller.tenant),
        "graph": await _engine.caller_dispatch_graph(context),
        "query": params["query"],
        "ngram_n": params["ngram_n"],
        "max_spans": min(int(params["max_spans"]), MAX_SPANS),
    }
    if params.get("cursor") is not None:
        request["cursor"] = params["cursor"]
    answer = await engine_send(context, "RetrievalContextSearch", {"request": request})
    spans = answer.get("spans") if isinstance(answer, Mapping) else None
    if not isinstance(spans, list) or len(spans) > MAX_SPANS:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    for span in spans:
        _checked_span(span)
    return {"value": dict(answer)}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.evaluation.compare",
            summary="Compare retrieval algorithms on one labeled query set",
            examples=("compare these retrieval algorithms",),
            params=RetrievalCompareParams,
            result=RetrievalEvalResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_compare",
            scope="memory:read",
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.context.search",
            summary="Search authorized documents and return context spans",
            examples=("search my documents with context",),
            params=RetrievalContextSearchParams,
            result=RetrievalEvalResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_context_search",
            scope="memory:read",
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.search",
            summary="Retrieve passages for a query within a context budget",
            examples=("find passages about this topic",),
            params=RetrievalSearchParams,
            result=RetrievalSearchResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_search",
            scope="memory:read",
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.freshness",
            summary="Report freshness of the caller's retrieval sources",
            examples=("how fresh are my retrieval sources",),
            params=RetrievalFreshnessParams,
            result=RetrievalFreshnessResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_freshness",
            scope="memory:read",
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.documents.list",
            summary="List source documents with their table-of-contents digests",
            examples=("list these documents",),
            params=RetrievalDocumentsListParams,
            result=RetrievalWriteResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_documents_list",
            scope="kg:read",
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="retrieval.span.read",
            summary="Read a line span of one document revision",
            examples=("read these lines of the document",),
            params=RetrievalSpanReadParams,
            result=RetrievalWriteResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_span_read",
            scope="kg:read",
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="retrieval.feedback",
            summary="Record feedback on one retrieved document",
            examples=("this document was useful",),
            params=RetrievalFeedbackParams,
            result=RetrievalWriteResult,
            handler="arbora.api.ops.retrieval.handle_retrieval_feedback",
            scope="memory:write",
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="agent.knowledge_scope.set",
            summary="Set the knowledge sources one agent may retrieve from",
            examples=("limit this agent to these sources",),
            params=KnowledgeScopeSetParams,
            result=RetrievalWriteResult,
            handler="arbora.api.ops.retrieval.handle_agent_knowledge_scope_set",
            scope="node:write",
        ).model_copy(update={"audit": AuditClass.IDENTITY_CHAIN}),
    )


specs = operations

__all__ = [
    "RetrievalCompareParams",
    "RetrievalContextSearchParams",
    "handle_retrieval_compare",
    "handle_retrieval_context_search",
    "RetrievalFreshnessOutcome",
    "RetrievalFreshnessParams",
    "RetrievalFreshnessResult",
    "RetrievalHit",
    "RetrievalSearchOutcome",
    "RetrievalSearchParams",
    "RetrievalSearchResult",
    "Retriever",
    "request_retriever",
    "KnowledgeScopeSetParams",
    "RetrievalFeedbackParams",
    "handle_agent_knowledge_scope_set",
    "handle_retrieval_feedback",
    "handle_retrieval_freshness",
    "handle_retrieval_search",
    "operations",
    "specs",
]
