"""``research.web`` composes the engine's selection and enrichment (ARBORA-OPS-R067)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.audit_evidence import result_evidence
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import research
from arbora.api.registry import AuditClass, ContentOrigin, Effect
from tests.api import _research_fakes as fakes
from tests.api._research_fakes import (
    FakeFleet,
    RecordingEngine,
    research_context,
    research_op,
    research_params,
)

_FETCH_AND_INGEST = {"fleet:webarchiver-mcp:web_extract", "engine:ConnectorPack"}


async def _run(
    monkeypatch: pytest.MonkeyPatch, fleet: FakeFleet | None = None, **params: Any
) -> tuple[dict[str, Any], Any, RecordingEngine]:
    context, ingest, engine = research_context(fleet or FakeFleet(), monkeypatch)
    value = (
        await research.handle_research_web(
            context, research_params(**params), research_op()
        )
    )["value"]
    return value, ingest, engine


@pytest.mark.spec("ARBORA-OPS-R067.1")
def test_params_and_result_round_trip() -> None:
    params = research.ResearchWebParams(
        query="q", urls=("https://a.example/x",), max_pages=32, backend="archivebox"
    )
    again = research.ResearchWebParams.model_validate(params.model_dump())
    assert again == params and again.deadline_s == 60
    assert research.ResearchWebParams(query="q").freshness_window_s == 86_400
    fields = {
        "work_item_id": "wi-1",
        "decision_id": "d",
        "state": "complete",
        "documents": [],
        "skipped": [],
        "fallbacks": 0,
        "ingest": {},
        "embedding": "engine",
    }
    result = research.ResearchWebResult(value=fields)
    assert research.ResearchWebResult.model_validate(result.model_dump()) == result
    with pytest.raises(ValueError):
        research.ResearchWebParams(query="q", max_pages=33)
    with pytest.raises(ValueError):
        research.ResearchWebParams()
    with pytest.raises(ValueError):
        research.ResearchWebParams(
            urls=tuple(f"https://e.example/{i}" for i in range(33))
        )
    with pytest.raises(ValueError):
        research.ResearchWebParams(query="q", extra=1)  # type: ignore[call-arg]


@pytest.mark.spec("ARBORA-OPS-R067.2")
async def test_composes_four_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    value, ingest, engine = await _run(monkeypatch, max_pages=2)
    steps = [e for e in fakes.LOG if e not in _FETCH_AND_INGEST]
    assert steps == [
        "fleet:searxng-mcp:web_search",
        "work_item:submit",
        "engine:SelectWebPages",
        "engine:EnrichDocument",
        "engine:EnrichDocument",
        "work_item:commit:succeeded",
    ]
    assert value["work_item_id"] == "wi-1" and value["state"] == "complete"
    _assert_documents(value, ingest, engine)


def _assert_documents(value: dict[str, Any], ingest: Any, engine: Any) -> None:
    docs = value["documents"]
    assert [d["url"] for d in docs] == [
        "https://a.example/pdb",
        "https://b.example/pdb",
    ]
    assert all(d["chunks"] == 3 and len(d["citations"]) == 3 for d in docs)
    citation = docs[0]["citations"][0]
    assert citation["url"] == "https://a.example/pdb"
    assert citation["rank"] == 1 and citation["backend"] == "webarchiver"
    assert citation["query"] == "kubernetes pod disruption budget"
    assert {"retrieved_at", "content_digest"} <= set(citation)
    # The engine, not arbora, picked the pages: the candidates went out unranked.
    select = next(p for m, p in engine.sent if m == "SelectWebPages")["request"]
    assert [c["rank"] for c in select["candidates"]] == [1, 2, 3, 4]
    assert len(ingest.committed) == 2 and value["ingest"]["affected"] == 2


@pytest.mark.spec("ARBORA-OPS-R067.2")
async def test_the_work_item_key_is_the_query_digest_and_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, _ = research_context(FakeFleet(), monkeypatch)
    await research.handle_research_web(context, research_params(), research_op())
    submitted = context.services["task_plan_work_engine"].submitted
    assert [s["kind"] for s in submitted] == ["research_web"]
    assert submitted[0]["tenant"] == "tenant-a"
    assert len(submitted[0]["idempotency_key"]) == 64


@pytest.mark.spec("ARBORA-OPS-R067.2")
async def test_deadline_returns_partial_or_enriching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(research, "_clock", lambda: clock[0])

    class Slow(FakeFleet):
        async def call(
            self, caller: Any, server: str, tool: str, arguments: Any, **kw: Any
        ) -> Any:
            answer = await super().call(caller, server, tool, arguments, **kw)
            if tool == research.WEB_EXTRACT:
                clock[0] += 100.0
            return answer

    # One page lands, then the deadline passes before enrichment finishes: enriching.
    enriching, _, _ = await _run(monkeypatch, Slow(), max_pages=1, deadline_s=60)
    assert enriching["state"] == "enriching"
    assert enriching["documents"][0]["enrichment"] == "pending"
    assert "work_item:commit:succeeded" not in fakes.LOG
    # The deadline passes during the first page: the rest are skipped with `deadline`.
    clock[0] = 0.0
    late, _, _ = await _run(monkeypatch, Slow(), max_pages=3, deadline_s=60)
    assert late["state"] == "enriching"
    assert {s["reason"] for s in late["skipped"]} == {"deadline"}
    # The deadline passes before the first fetch: partial, every page skipped, none fetched.
    ticks = iter([0.0])
    monkeypatch.setattr(research, "_clock", lambda: next(ticks, 1e6))
    fleet = FakeFleet()
    partial, _, _ = await _run(monkeypatch, fleet, max_pages=2, deadline_s=60)
    assert partial["state"] == "partial" and partial["documents"] == []
    assert {s["reason"] for s in partial["skipped"]} == {"deadline"}
    assert not [c for c in fleet.calls if c[1] == research.WEB_EXTRACT]


@pytest.mark.spec("ARBORA-OPS-R067.2")
def test_arbora_ranks_and_chunks_nothing_locally() -> None:
    for name in (
        "select_hits",
        "chunk_text",
        "InterimEnrich",
        "EngineEmbedder",
        "Chunk",
    ):
        assert not hasattr(research, name), name


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_research_web_refuses_a_caller_without_the_child_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, _ = research_context(
        FakeFleet(), monkeypatch, scopes=frozenset({"ingest:write"})
    )
    with pytest.raises(OperationRefused) as refused:
        await research.handle_research_web(context, research_params(), research_op())
    assert refused.value.code == "SCOPE_DENIED"


@pytest.mark.spec("ARBORA-OPS-R067.4")
def test_research_web_is_a_scoped_open_web_write_and_registered() -> None:
    from arbora.api.ops.registry_factory import get_registry

    op = get_registry().get("research.web")
    assert op is not None
    assert op.effect is Effect.WRITE and op.audit is AuditClass.EVENT
    assert op.content_origin is ContentOrigin.OPEN_WEB
    assert op.scopes == frozenset({"ingest:write", "mcp:delegate"})


@pytest.mark.spec("ARBORA-OPS-R067.4")
async def test_an_absent_engine_or_ingest_writer_refuses_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, _ = research_context(FakeFleet(), monkeypatch)
    context.client = None
    with pytest.raises(OperationRefused) as refused:
        await research.handle_research_web(context, research_params(), research_op())
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["reason"] == "engine_unavailable"


@pytest.mark.spec("ARBORA-OPS-R067.5")
async def test_research_web_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused_with(reason: str) -> OperationRefused:
        return OperationRefused("UNAVAILABLE", {"reason": reason})

    async def attempt(
        fleet: FakeFleet, engine: RecordingEngine | None = None, **kw: Any
    ) -> tuple[Exception, FakeFleet, Any]:
        context, ingest, _ = research_context(fleet, monkeypatch, engine=engine, **kw)
        try:
            await research.handle_research_web(
                context, research_params(), research_op()
            )
        except (OperationRefused, EngineRefusal) as exc:
            return exc, fleet, ingest
        raise AssertionError("expected a refusal")

    def fetched(fleet: FakeFleet) -> list[str]:
        return [t for _s, t, _a in fleet.calls if t == research.WEB_EXTRACT]

    # A caller without ingest:write.
    exc, fleet, ingest = await attempt(FakeFleet(), scopes=frozenset({"mcp:delegate"}))
    assert isinstance(exc, OperationRefused) and exc.code == "SCOPE_REQUIRED"
    assert not fleet.calls and not ingest.committed
    # An engine abstention passes through with its own code and enqueues no fetch.
    for code in (
        "WEB_SELECTION_QUERY_MISSING",
        "WEB_SELECTION_NO_CANDIDATES",
        "WEB_SELECTION_TOO_MANY_CANDIDATES",
        "WEB_SELECTION_BUDGET_INVALID",
    ):
        engine = RecordingEngine(SelectWebPages=EngineRefusal(code))
        exc, fleet, ingest = await attempt(FakeFleet(), engine)
        assert isinstance(exc, EngineRefusal) and exc.code == code
        assert not fetched(fleet) and not ingest.committed
        assert "work_item:commit:failed" in fakes.LOG
    # An unreachable engine.
    engine = RecordingEngine(SelectWebPages=ConnectionError("down"))
    exc, fleet, ingest = await attempt(FakeFleet(), engine)
    assert isinstance(exc, OperationRefused) and exc.code == "UNAVAILABLE"
    assert exc.details["reason"] == "decision_unavailable"
    assert not fetched(fleet) and not ingest.committed
    # An absent ingest writer.
    exc, fleet, ingest = await attempt(FakeFleet(), services={"web_ingest": None})
    assert isinstance(exc, OperationRefused)
    assert exc.details["reason"] == "ingest_unavailable"
    assert not fleet.calls
    # An unknown backend.
    context, _, _ = research_context(FakeFleet(), monkeypatch)
    with pytest.raises(OperationRefused) as bad:
        await research.handle_research_web(
            context, {**research_params(), "backend": "wget"}, research_op()
        )
    assert bad.value.code == "INVALID_ARGUMENT" and bad.value.details == {
        "field": "backend"
    }


@pytest.mark.spec("ARBORA-OPS-R067.6")
async def test_trace_names_decision_and_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    fleet = FakeFleet(
        webarchiver_fails={"https://a.example/pdb?utm_source=x"},
        reason="upstream_failed",
    )
    value, _, _ = await _run(monkeypatch, fleet, max_pages=2)
    args = research_params(max_pages=2)
    evidence = result_evidence(
        "research.web", args, SimpleNamespace(value=value, code="OK")
    )
    subject = "kubernetes pod disruption budget"
    assert evidence["query_digest"] == hashlib.sha256(subject.encode()).hexdigest()
    assert evidence["decision_id"] == value["decision_id"] != ""
    assert evidence["work_item_id"] == "wi-1"
    assert evidence["backends"] == "archivebox,webarchiver"
    assert evidence["fallback_count"] == "1"
    assert evidence["document_ids"] == ",".join(
        d["document_id"] for d in value["documents"]
    )
    body = " ".join(evidence.values())
    assert "Title of" not in body and fakes.ARTICLE[:20] not in body
    assert research_op().audit is AuditClass.EVENT


@pytest.mark.spec("ARBORA-OPS-R067.1")
def test_research_web_declares_its_replay_owner() -> None:
    """A governed write needs a replay owner, or invoke refuses it UNAVAILABLE."""
    from arbora.api.invoke.pipeline import _declared_owners

    assert _declared_owners()["research.web"] == "medulla:SourceIngest"


@pytest.mark.spec("ARBORA-OPS-R067.1")
def test_canonical_url_drops_tracking_and_fragments() -> None:
    assert (
        research.canonical_url("HTTPS://Ex.org/a/?b=1&utm_medium=z#f")
        == "https://ex.org/a?b=1"
    )
