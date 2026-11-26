"""Retrieval read operations (ARBORA-OPS-R021.2.1, retrieval slice).

``retrieval.search`` runs a context-budgeted retrieval through one typed port
under the caller's own tenant and authority; there is no local cache
substitute. An uncomposed retriever fails closed with a typed ``UNAVAILABLE``
refusal, matching :mod:`arbora.api.ops.ingest`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.ops._common import Params, bound_service, build_read_op, build_write_op
from arbora.api.registry import AuditClass, OpSpec, Verb
from arbora.pin_lag.medulla import guard_field, send_main


class RetrievalSearchParams(Params):
    query: str = Field(min_length=1, max_length=4096)
    context_budget: int = Field(default=4096, ge=1, le=1_000_000)


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


async def handle_retrieval_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return budget-bounded hits for the caller's own tenant."""
    retriever: Retriever = bound_service(
        context, "retriever", reason="retriever is not composed"
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
    retriever: Retriever = bound_service(
        context, "retriever", reason="retriever is not composed"
    )
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
    """Set one agent's knowledge scope; an engine refusal of a widening scope passes through."""
    answer = await send_main(
        context,
        "AgentKnowledgeScopeSet",
        {"agent_id": params["agent_id"], "sources": list(params["sources"])},
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "retrieval.feedback": "medulla:RetrievalFeedbackRecord",
    "agent.knowledge_scope.set": "medulla:AgentKnowledgeScopeSet",
}


def operations() -> tuple[OpSpec, ...]:
    return (
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
            scope="registry:write",
        ).model_copy(update={"audit": AuditClass.IDENTITY_CHAIN}),
    )


specs = operations

__all__ = [
    "RetrievalFreshnessOutcome",
    "RetrievalFreshnessParams",
    "RetrievalFreshnessResult",
    "RetrievalHit",
    "RetrievalSearchOutcome",
    "RetrievalSearchParams",
    "RetrievalSearchResult",
    "Retriever",
    "KnowledgeScopeSetParams",
    "RetrievalFeedbackParams",
    "handle_agent_knowledge_scope_set",
    "handle_retrieval_feedback",
    "handle_retrieval_freshness",
    "handle_retrieval_search",
    "operations",
    "specs",
]
