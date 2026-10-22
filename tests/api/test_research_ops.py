"""``research.web`` selection, ingest, citations and wiring (ARBORA-OPS-R067.3)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import research
from arbora.api.registry import Effect
from tests.api._research_fakes import (
    ARTICLE,
    FakeFleet,
    research_context,
    research_op,
    research_params,
)


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_research_web_selects_fetches_ingests_and_cites() -> None:
    fleet = FakeFleet()
    context, ingest, embedder = research_context(fleet)
    result = (
        await research.handle_research_web(context, research_params(), research_op())
    )["value"]
    docs = result["documents"]
    assert [d["url"] for d in docs] == [
        "https://a.example/pdb",
        "https://b.example/pdb",
        "https://d.example/pdb",
    ]
    assert all(
        d["backend"] == "webarchiver" and d["document_id"].startswith("web:")
        for d in docs
    )
    assert all(d["chunks"] > 1 and d["embedded"] == d["chunks"] for d in docs)
    citation = docs[0]["citations"][0]
    assert citation["document_id"] == docs[0]["document_id"]
    assert citation["url"] == "https://a.example/pdb"
    assert citation["char_end"] > citation["char_start"] == 0
    assert len(ingest.committed) == 3
    props = research._document_properties(ingest.committed[0])
    assert props["search_query"] == "kubernetes pod disruption budget"
    assert props["search_engine"] == "duckduckgo" and props["backend"] == "webarchiver"
    assert len(embedder.chunks) == sum(d["chunks"] for d in docs)
    # The duplicate canonical URL and the ftp result were never fetched.
    fetched = [
        a["request"]["url"]
        for s, _t, a in fleet.calls
        if s == research.WEBARCHIVER_SERVER
    ]
    assert fetched == [
        "https://a.example/pdb?utm_source=x",
        "https://b.example/pdb",
        "https://d.example/pdb",
    ]


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_research_web_refuses_a_caller_without_the_child_scope() -> None:
    context, _, _ = research_context(FakeFleet(), scopes=frozenset({"ingest:write"}))
    with pytest.raises(OperationRefused) as refused:
        await research.handle_research_web(context, research_params(), research_op())
    assert refused.value.code == "SCOPE_DENIED"


@pytest.mark.spec("ARBORA-OPS-R067.3")
def test_research_web_is_a_scoped_write_and_registered() -> None:
    from arbora.api.ops.registry_factory import get_registry

    op = get_registry().get("research.web")
    assert op is not None
    assert op.effect is Effect.WRITE
    assert op.scopes == frozenset({"ingest:write", "mcp:delegate"})


@pytest.mark.spec("ARBORA-OPS-R067.3")
def test_chunks_cover_the_text_with_exact_offsets() -> None:
    text = ARTICLE * 2
    chunks = research.chunk_text("web:x", text)
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    for chunk in chunks:
        assert chunk.text == text[chunk.start : chunk.end].strip()
    assert (
        research.canonical_url("HTTPS://Ex.org/a/?b=1&utm_medium=z#f")
        == "https://ex.org/a?b=1"
    )


@pytest.mark.spec("ARBORA-OPS-R067.3")
def test_research_web_declares_its_replay_owner() -> None:
    """A governed write needs a replay owner, or invoke refuses it UNAVAILABLE."""
    from arbora.api.invoke.pipeline import _declared_owners

    assert _declared_owners()["research.web"] == "epistemic_graph:SourceIngest"
