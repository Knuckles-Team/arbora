"""Specification documents and knowledge-base articles as reads (ARBORA-OPS-R906).

``sdd.documents.read`` reads the spec files of the configured specification
repository through the ``source_reader`` port of ``source.tree`` and
``source.blob``; it never reads the host disk. The repository and ref are the
settings ``ARBORA_SDD_REPOSITORY`` and ``ARBORA_SDD_REF``. ``kb.articles.read``
lists or gets ``KbArticle`` nodes with ``GetNodesByLabel``; a query asks the
retriever and keeps the hits that are KB articles. Neither writes: the web
interface writes specifications by pull request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops._engine import decode, engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.ops.source import SourceReader
from arbora.api.ops.workstreams import _props
from arbora.api.registry import OpSpec, Verb

SDD_SCOPE = "node:read"
KB_SCOPE = "node:read"
MAX_DOCUMENTS = 100
MAX_BODY_BYTES = 256 * 1024
KB_LABEL = "KbArticle"
CONSTITUTION_PATH = ".specify/memory/constitution.md"
_FILE = {"spec": "spec.md", "plan": "plan.md", "task": "tasks.md"}
_Id = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._-]+$")


class SddDocumentsParams(Params):
    kind: Literal["constitution", "spec", "plan", "task"]
    id: str | None = _Id
    repository: str | None = Field(default=None, min_length=1, max_length=1024)


class KbArticlesParams(Params):
    article_id: str | None = Field(default=None, min_length=1, max_length=256)
    query: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=20, ge=1, le=MAX_DOCUMENTS)


def _settings(params: Mapping[str, Any]) -> tuple[str, str]:
    from arbora.core.settings import setting

    repository = params.get("repository") or str(
        setting("ARBORA_SDD_REPOSITORY", "") or ""
    )
    if not repository:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no specification repository is configured"}
        )
    return str(repository), str(setting("ARBORA_SDD_REF", "main") or "main")


async def _document(
    context: Any,
    reader: SourceReader,
    repository: str,
    ref: str,
    kind: str,
    path: str,
    doc_id: str,
) -> dict[str, Any] | None:
    blob = await reader.blob(repository=repository, ref=ref, path=path, context=context)
    if not blob.found:
        return None
    raw = blob.content[:MAX_BODY_BYTES]
    text = raw.decode("utf-8", errors="replace")
    title = next(
        (line.lstrip("# ").strip() for line in text.splitlines() if line.strip()),
        doc_id,
    )
    return {
        "id": doc_id,
        "kind": kind,
        "title": title,
        "body": text,
        "path": path,
        "revision": blob.content_digest,
        "updated_at": None,
        "_truncated": len(blob.content) > MAX_BODY_BYTES,
    }


async def handle_sdd_documents(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    reader: SourceReader = bound_service(
        context, "source_reader", reason="source reader is not composed"
    )
    repository, ref = _settings(params)
    kind, doc_id = params["kind"], params.get("id")
    if kind == "constitution":
        targets = [("constitution", CONSTITUTION_PATH)]
        truncated = False
    elif doc_id is not None:
        targets = [(doc_id, f"specs/{doc_id}/{_FILE[kind]}")]
        truncated = False
    else:
        tree = await reader.tree(
            repository=repository, ref=ref, path="specs", context=context
        )
        if not tree.found:
            raise OperationRefused("NOT_FOUND")
        directories = [e for e in tree.entries if e.kind != "file"]
        truncated = len(directories) > MAX_DOCUMENTS
        targets = [
            (e.path.rsplit("/", 1)[-1], f"{e.path}/{_FILE[kind]}")
            for e in directories[:MAX_DOCUMENTS]
        ]
    documents: list[dict[str, Any]] = []
    for name, path in targets:
        document = await _document(context, reader, repository, ref, kind, path, name)
        if document is None:
            if doc_id is not None or kind == "constitution":
                raise OperationRefused("NOT_FOUND")
            continue
        truncated = truncated or document.pop("_truncated")
        document.pop("_truncated", None)
        documents.append(document)
    return {"value": {"documents": documents, "truncated": truncated}}


def _article(node_id: str, props: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "title": props.get("title") or "",
        "summary": props.get("summary") or "",
        "body": props.get("body") or "",
        "tags": list(props.get("tags") or ()),
        "updated_at": props.get("updated_at"),
    }


async def _page(context: Any, limit: int, after: str | None) -> list[Any]:
    request: dict[str, Any] = {"label": KB_LABEL, "limit": limit}
    if after is not None:
        request["after"] = after
    return list(decode(await engine_send(context, "GetNodesByLabel", request)) or ())


def _articles(rows: list[Any]) -> list[dict[str, Any]]:
    found = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) == 2:
            props = _props(row[1])
            if props is not None:
                found.append(_article(str(row[0]), props))
    return found


async def handle_kb_articles(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    limit = int(params.get("limit", 20))
    if params.get("query") is not None:
        retriever = bound_service(
            context, "retriever", reason="retriever is not composed"
        )
        known = {a["id"]: a for a in _articles(await _page(context, 500, None))}
        outcome = await retriever.search(
            tenant=context.caller.tenant,
            query=params["query"],
            context_budget=1_000_000,
        )
        hits = [known[h.ref] for h in outcome.hits if h.ref in known]
        return {"value": {"articles": hits[:limit], "truncated": len(hits) > limit}}
    rows = await _page(context, 500 if params.get("article_id") else limit + 1, None)
    articles = _articles(rows)
    if params.get("article_id") is not None:
        articles = [a for a in articles if a["id"] == params["article_id"]]
        if not articles:
            raise OperationRefused("NOT_FOUND")
        return {"value": {"articles": articles, "truncated": False}}
    return {"value": {"articles": articles[:limit], "truncated": len(articles) > limit}}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.ASK,
            op_id="sdd.documents.read",
            summary="Read specification documents from the specification repository",
            examples=("read the constitution", "show this specification"),
            params=SddDocumentsParams,
            result=EngineReply,
            handler="arbora.api.ops.documents.handle_sdd_documents",
            scope=SDD_SCOPE,
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="kb.articles.read",
            summary="List, read or search knowledge-base articles",
            examples=("find knowledge base articles",),
            params=KbArticlesParams,
            result=EngineReply,
            handler="arbora.api.ops.documents.handle_kb_articles",
            scope=KB_SCOPE,
        ),
    )


specs = operations
