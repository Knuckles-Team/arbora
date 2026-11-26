"""Specification documents and knowledge-base articles (ARBORA-OPS-R906)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import documents
from arbora.api.ops.source import SourceBlobOutcome, SourceTreeEntry, SourceTreeOutcome
from tests.api._knowledge_support import NO_OP

pytestmark = pytest.mark.spec("ARBORA-OPS-R906")


class Reader:
    def __init__(self, files: dict[str, bytes], dirs: list[str]) -> None:
        self.files, self.dirs, self.calls = files, dirs, []

    async def tree(self, *, repository: str, ref: str, path: str, context: Any) -> Any:
        self.calls.append(("tree", repository, ref, path))
        return SourceTreeOutcome(
            tenant="t",
            repository=repository,
            ref=ref,
            path=path,
            found=True,
            entries=tuple(SourceTreeEntry(path=d, kind="directory") for d in self.dirs),
        )

    async def blob(self, *, repository: str, ref: str, path: str, context: Any) -> Any:
        self.calls.append(("blob", repository, ref, path))
        data = self.files.get(path)
        return SourceBlobOutcome(
            tenant="t",
            repository=repository,
            ref=ref,
            path=path,
            found=data is not None,
            content=data or b"",
            content_digest="d1" if data is not None else None,
            size=len(data or b""),
        )


def _context(**services: Any) -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t", principal="p"), services=services
    )


@pytest.fixture(autouse=True)
def repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARBORA_SDD_REPOSITORY", "specs-repo")
    monkeypatch.delenv("ARBORA_SDD_REF", raising=False)


async def test_sdd_documents_read_through_source_operations() -> None:
    reader = Reader(
        {
            ".specify/memory/constitution.md": b"# Constitution\nrules",
            "specs/a/spec.md": b"# Spec A\n",
            "specs/a/plan.md": b"# Plan A\n",
            "specs/b/spec.md": b"# Spec B\n",
        },
        ["specs/a", "specs/b"],
    )
    context = _context(source_reader=reader)
    constitution = await documents.handle_sdd_documents(
        context, {"kind": "constitution"}, NO_OP
    )
    (doc,) = constitution["value"]["documents"]
    assert doc["title"] == "Constitution" and doc["revision"] == "d1"
    listing = await documents.handle_sdd_documents(context, {"kind": "spec"}, NO_OP)
    assert [d["id"] for d in listing["value"]["documents"]] == ["a", "b"]
    one = await documents.handle_sdd_documents(
        context, {"kind": "plan", "id": "a"}, NO_OP
    )
    assert one["value"]["documents"][0]["path"] == "specs/a/plan.md"
    assert all(call[1:3] == ("specs-repo", "main") for call in reader.calls)


async def test_sdd_never_reads_the_host_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import pathlib

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the host disk was read")

    reader = Reader({"specs/a/spec.md": b"# A"}, ["specs/a"])
    monkeypatch.setattr(pathlib.Path, "read_bytes", refuse)
    monkeypatch.setattr(pathlib.Path, "read_text", refuse)
    monkeypatch.setattr(builtins, "open", refuse)
    answer = await documents.handle_sdd_documents(
        _context(source_reader=reader), {"kind": "spec"}, NO_OP
    )
    assert answer["value"]["documents"][0]["id"] == "a"


async def test_absent_document_is_not_found_and_no_repository_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(source_reader=Reader({}, []))
    with pytest.raises(OperationRefused) as refused:
        await documents.handle_sdd_documents(
            context, {"kind": "spec", "id": "zzz"}, NO_OP
        )
    assert refused.value.code == "NOT_FOUND"
    monkeypatch.delenv("ARBORA_SDD_REPOSITORY")
    with pytest.raises(OperationRefused) as unavailable:
        await documents.handle_sdd_documents(context, {"kind": "constitution"}, NO_OP)
    assert unavailable.value.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as unbound:
        await documents.handle_sdd_documents(_context(), {"kind": "spec"}, NO_OP)
    assert unbound.value.code == "UNAVAILABLE"


def _rows(*articles: tuple[str, dict[str, Any]]) -> list[Any]:
    return [[node_id, msgpack.packb(props)] for node_id, props in articles]


async def test_kb_articles_list_get_and_search(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(
        ("kb:1", {"title": "One", "summary": "s", "body": "b", "tags": ["x"]}),
        ("kb:2", {"title": "Two"}),
        ("kb:3", {"title": "Three"}),
    )
    sent: list[tuple[str, dict[str, Any]]] = []

    async def engine_send(context: Any, method: str, params: dict[str, Any]) -> Any:
        sent.append((method, params))
        return rows

    monkeypatch.setattr(documents, "engine_send", engine_send)
    context = _context()
    listing = await documents.handle_kb_articles(context, {"limit": 2}, NO_OP)
    assert [a["id"] for a in listing["value"]["articles"]] == ["kb:1", "kb:2"]
    assert listing["value"]["truncated"] is True
    assert sent[0] == ("GetNodesByLabel", {"label": "KbArticle", "limit": 3})
    got = await documents.handle_kb_articles(context, {"article_id": "kb:1"}, NO_OP)
    assert got["value"]["articles"][0]["tags"] == ["x"]
    with pytest.raises(OperationRefused) as refused:
        await documents.handle_kb_articles(context, {"article_id": "kb:9"}, NO_OP)
    assert refused.value.code == "NOT_FOUND"

    class Retriever:
        async def search(self, *, tenant: str, query: str, context_budget: int) -> Any:
            hits = [SimpleNamespace(ref="kb:3"), SimpleNamespace(ref="doc:other")]
            return SimpleNamespace(hits=hits)

    searched = await documents.handle_kb_articles(
        _context(retriever=Retriever()), {"query": "three"}, NO_OP
    )
    assert [a["id"] for a in searched["value"]["articles"]] == ["kb:3"]
    with pytest.raises(OperationRefused) as unbound:
        await documents.handle_kb_articles(context, {"query": "q"}, NO_OP)
    assert unbound.value.code == "UNAVAILABLE"
