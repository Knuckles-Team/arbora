"""Focused authority tests for ``retrieval.search`` (ARBORA-OPS-R021.2.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import require_scopes
from arbora.api.ops import retrieval
from arbora.api.ops.retrieval import (
    RetrievalFreshnessOutcome,
    RetrievalHit,
    RetrievalSearchOutcome,
)
from arbora.api.registry import AuditClass, Effect
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context
from tests.api._ops_support import tenant_store_context


class _FakeRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.freshness_calls: list[str] = []

    async def freshness(self, *, tenant: str) -> RetrievalFreshnessOutcome:
        self.freshness_calls.append(tenant)
        return RetrievalFreshnessOutcome(tenant=tenant, sources={"docs": "fresh"})

    async def search(
        self, *, tenant: str, query: str, context_budget: int
    ) -> RetrievalSearchOutcome:
        self.calls.append((tenant, query, context_budget))
        return RetrievalSearchOutcome(
            tenant=tenant,
            hits=(
                RetrievalHit(ref="a", text="one", tokens=60),
                RetrievalHit(ref="b", text="two", tokens=60),
            ),
        )


def _context(retriever: object | None):
    return tenant_store_context(store=retriever, service_name="retriever")


@pytest.mark.spec("ARBORA-OPS-R021.2.1")
async def test_search_reaches_engine_under_callers_tenant_within_budget(
    op_by_id,
) -> None:
    fake = _FakeRetriever()
    op = op_by_id(retrieval.operations(), "retrieval.search")
    result = await retrieval.handle_retrieval_search(
        _context(fake), {"query": "q", "context_budget": 100}, op
    )
    assert fake.calls == [("tenant-a", "q", 100)]
    assert result["value"]["tenant"] == "tenant-a"
    assert [h["ref"] for h in result["value"]["hits"]] == ["a"]
    assert op.scopes == frozenset({"memory:read"})


@pytest.mark.spec("ARBORA-OPS-R021.2.1")
async def test_search_fails_closed_when_retriever_not_composed(op_by_id) -> None:
    op = op_by_id(retrieval.operations(), "retrieval.search")
    with pytest.raises(OperationRefused) as excinfo:
        await retrieval.handle_retrieval_search(
            _context(None), {"query": "q", "context_budget": 100}, op
        )
    assert excinfo.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R021.2.2")
async def test_freshness_reaches_engine_under_callers_tenant_read_only(
    op_by_id,
) -> None:
    fake = _FakeRetriever()
    op = op_by_id(retrieval.operations(), "retrieval.freshness")
    result = await retrieval.handle_retrieval_freshness(_context(fake), {}, op)
    assert fake.freshness_calls == ["tenant-a"]
    assert result["value"] == {"tenant": "tenant-a", "sources": {"docs": "fresh"}}
    assert op.scopes == frozenset({"memory:read"})
    assert op.verb.value == "find"


@pytest.mark.spec("ARBORA-OPS-R021.2.2")
async def test_freshness_fails_closed_when_retriever_not_composed(op_by_id) -> None:
    op = op_by_id(retrieval.operations(), "retrieval.freshness")
    with pytest.raises(OperationRefused) as excinfo:
        await retrieval.handle_retrieval_freshness(_context(None), {}, op)
    assert excinfo.value.code == "UNAVAILABLE"


# --- feedback and agent knowledge scope (ARBORA-OPS-R854) ---------------------------------

_RETRIEVAL_OPS = {op.id: op for op in retrieval.operations()}


@pytest.mark.spec("ARBORA-OPS-R854.1")
def test_feedback_and_scope_operations_listed() -> None:
    feedback = _RETRIEVAL_OPS["retrieval.feedback"]
    scope = _RETRIEVAL_OPS["agent.knowledge_scope.set"]
    assert feedback.effect is Effect.WRITE
    assert feedback.scopes == frozenset({"memory:write"})
    assert scope.effect is Effect.WRITE
    assert scope.scopes == frozenset({"registry:write"})
    assert scope.audit is AuditClass.IDENTITY_CHAIN
    assert set(retrieval.NATIVE_IDEMPOTENCY) == {
        "retrieval.feedback",
        "agent.knowledge_scope.set",
    }


@pytest.mark.spec("ARBORA-OPS-R854.1")
def test_feedback_principal_comes_from_admission() -> None:
    body = {"document_ref": "d1", "kind": "upvote", "weight": 1.0}
    assert retrieval.RetrievalFeedbackParams(**body)
    with pytest.raises(ValidationError):
        retrieval.RetrievalFeedbackParams(principal="someone-else", **body)
    with pytest.raises(ValidationError):
        retrieval.RetrievalFeedbackParams(document_ref="d1", kind="implicit_click")


@pytest.mark.spec("ARBORA-OPS-R854.1")
async def test_a_pinned_engine_without_the_method_refuses_naming_it() -> None:
    from medulla.generated import METHOD_IDS

    if "RetrievalFeedbackRecord" in METHOD_IDS:
        pytest.skip("the pin carries RetrievalFeedbackRecord")
    with pytest.raises(OperationRefused) as refused:
        await retrieval.handle_retrieval_feedback(
            engine_context(),
            {"document_ref": "d1", "kind": "upvote", "weight": 1.0},
            _RETRIEVAL_OPS["retrieval.feedback"],
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "RetrievalFeedbackRecord"
    with pytest.raises(OperationRefused) as scope:
        await retrieval.handle_agent_knowledge_scope_set(
            engine_context(),
            {"agent_id": "a", "sources": ["s"]},
            _RETRIEVAL_OPS["agent.knowledge_scope.set"],
        )
    assert scope.value.details["missing"] == "AgentKnowledgeScopeSet"


@pytest.mark.spec("ARBORA-OPS-R854", "ARBORA-OPS-R854.2")
async def test_scope_and_feedback_served(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = EngineDouble(monkeypatch)
    scopes: dict[str, list[str]] = {}
    ranks = {"d1": 2, "d2": 1}

    def set_scope(p: dict[str, Any]) -> Any:
        if "s-admin" in p["sources"]:
            raise EngineRefusal("KNOWLEDGE_SCOPE_WIDENS")
        scopes[p["agent_id"]] = list(p["sources"])
        return {"agent_id": p["agent_id"], "sources": scopes[p["agent_id"]]}

    def feedback(p: dict[str, Any]) -> Any:
        ranks[p["document_ref"]] += 5 if p["kind"] == "upvote" else -5
        return {"recorded": True}

    engine.serve("AgentKnowledgeScopeSet", set_scope)
    engine.serve("RetrievalFeedbackRecord", feedback)
    engine.install(pin_lag)
    ctx = engine_context()

    await retrieval.handle_agent_knowledge_scope_set(
        ctx,
        {"agent_id": "a1", "sources": ["docs"]},
        _RETRIEVAL_OPS["agent.knowledge_scope.set"],
    )
    with pytest.raises(EngineRefusal) as widened:
        await retrieval.handle_agent_knowledge_scope_set(
            ctx,
            {"agent_id": "a1", "sources": ["s-admin"]},
            _RETRIEVAL_OPS["agent.knowledge_scope.set"],
        )
    assert widened.value.code == "KNOWLEDGE_SCOPE_WIDENS"
    assert scopes == {"a1": ["docs"]}

    before = dict(ranks)
    await retrieval.handle_retrieval_feedback(
        ctx,
        {"document_ref": "d2", "kind": "upvote", "weight": 1.0},
        _RETRIEVAL_OPS["retrieval.feedback"],
    )
    # The rank change is the engine's: arbora sends one event and reorders nothing.
    assert ranks["d2"] > before["d2"] and ranks["d1"] == before["d1"]
    sent = next(p for m, p, _ in engine.sent if m == "RetrievalFeedbackRecord")
    assert "principal" not in sent
    caller = SimpleNamespace(effective_scopes=frozenset({"memory:read"}))
    for op_id in ("retrieval.feedback", "agent.knowledge_scope.set"):
        refused = require_scopes(_RETRIEVAL_OPS[op_id], caller)
        assert refused is not None and refused.code == "SCOPE_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R868")
async def test_listing_toc_digest_equals_read_span_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    monkeypatch.setattr(
        models.MethodReadSpanParams,
        "model_fields",
        {**models.MethodReadSpanParams.model_fields, "toc_digest": object()},
    )
    engine = EngineDouble(monkeypatch)
    digest = "b" * 64

    def read_span(p: dict[str, Any]) -> Any:
        if p.get("toc_digest") not in (None, digest):
            raise EngineRefusal("TOC_DIGEST_STALE")
        return {"document_id": p["document_id"], "toc_digest": digest, "text": "line"}

    engine.serve("ReadSpan", read_span)
    engine.install(pin_lag)
    ctx = engine_context()
    listing = await retrieval.handle_retrieval_documents_list(
        ctx, {"document_ids": ["d1"]}, _RETRIEVAL_OPS["retrieval.documents.list"]
    )
    span = await retrieval.handle_retrieval_span_read(
        ctx,
        {
            "document_id": "d1",
            "revision": 1,
            "line_start": 0,
            "line_end": 4,
            "toc_digest": digest,
        },
        _RETRIEVAL_OPS["retrieval.span.read"],
    )
    assert listing["value"]["documents"][0]["toc_digest"] == span["value"]["toc_digest"]
    with pytest.raises(EngineRefusal) as stale:
        await retrieval.handle_retrieval_span_read(
            ctx,
            {
                "document_id": "d1",
                "revision": 1,
                "line_start": 0,
                "line_end": 4,
                "toc_digest": "c" * 64,
            },
            _RETRIEVAL_OPS["retrieval.span.read"],
        )
    assert stale.value.code == "TOC_DIGEST_STALE"
    with pytest.raises(ValidationError):
        retrieval.RetrievalSpanReadParams(
            document_id="d1", revision=1, line_start=0, line_end=4, toc_digest="short"
        )


@pytest.mark.spec("ARBORA-OPS-R868")
async def test_a_pinned_engine_without_toc_digest_refuses_naming_it() -> None:
    from medulla.generated import models

    if "toc_digest" in models.MethodReadSpanParams.model_fields:
        pytest.skip("the pin carries toc_digest")
    with pytest.raises(OperationRefused) as refused:
        await retrieval.handle_retrieval_documents_list(
            engine_context(),
            {"document_ids": ["d1"]},
            _RETRIEVAL_OPS["retrieval.documents.list"],
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "MethodReadSpanParams.toc_digest"


# --- the verified agent reference (ARBORA-OPS-R883) -----------------------------------------


def _eg_op(method: str) -> Any:
    return SimpleNamespace(binding=SimpleNamespace(kind="eg", service=method))


def _who(kind: str, agent: str | None = None) -> Any:
    claims = {"agent_id": agent} if agent else {}
    return SimpleNamespace(principal_kind=kind, engine_claims=claims)


@pytest.mark.spec("ARBORA-OPS-R883")
def test_agent_ref_is_taken_from_identity() -> None:
    from arbora.api.invoke.agent_ref import AGENT_REF_METHODS, bind_agent_ref
    from arbora.api.invoke.steps import OpError

    assert len(AGENT_REF_METHODS) == 6
    for method in sorted(AGENT_REF_METHODS):
        op = _eg_op(method)
        agent = _who("agent", "agent:one")
        bound = bind_agent_ref(op, {"k": 1}, agent)
        assert bound["agent_ref"] == "agent:one" and bound["k"] == 1
        assert (
            bind_agent_ref(op, {"agent_ref": "agent:one"}, agent)["agent_ref"]
            == "agent:one"
        )
        forged = bind_agent_ref(op, {"agent_ref": "agent:two"}, agent)
        assert isinstance(forged, OpError) and forged.code == "AGENT_REF_FORGED"
        human = _who("human")
        assert "agent_ref" not in bind_agent_ref(op, {"k": 1}, human)
        assert bind_agent_ref(op, {"agent_ref": "x"}, human).code == "AGENT_REF_FORGED"
    other = {"k": 1, "agent_ref": "x"}
    assert bind_agent_ref(_eg_op("GetNodesByLabel"), other, _who("human")) is other
