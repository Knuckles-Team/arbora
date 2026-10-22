"""``research.web``: search, select, fetch, ingest and enrich web pages.

ARBORA-OPS-R067.3. One served operation composes the web-ingest pipeline under
the caller's own authority:

1. **Search** -- ``fleet.call`` to the searxng connector. Results are a
   short-lived source carrying query, engine, rank and time provenance; they
   are not ingested.
2. **Select** -- rank order, then de-duplication by canonical URL and by
   content digest, then the request budget (``max_documents``). Only http(s)
   URLs are eligible.
3. **Fetch** -- ``fleet.call`` to the webarchiver connector (``extract``),
   which enforces robots.txt, rate, size and content-type policy. ArchiveBox
   is the secondary backend: selectable with ``backend="archivebox"``, and the
   automatic fallback when webarchiver refuses or fails on a URL.
4. **Ingest** -- each page is committed through ``SourceIngest`` as a
   Document with provenance (query, rank, engine, backend, fetch time).
5. **Enrich** -- the Document is split into chunks linked to it, and each
   chunk is embedded into the vector index when an embedding model is
   configured.

Every fleet call is admitted through the same effect authority ``fleet.call``
uses, so a caller can only reach connectors its own scopes allow.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, model_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)

SEARCH_SERVER = "searxng-mcp"
SEARCH_TOOL = "web_search"
WEBARCHIVER_SERVER = "webarchiver-mcp"
#: The SDK web-source contract tool both backends serve.
WEB_EXTRACT = "web_extract"
ARCHIVEBOX_SERVER = "archivebox-mcp"

#: The SourceIngest streams pages and chunks are committed under. Each record
#: is bound to the fleet connector that fetched it, so its mapping reference is
#: ``manifest:<fleet server>#schema_mappings/<Document|Chunk>`` in the pack
#: Arbora's fleet onboarding imported for that server.
PAGE_STREAM = "web_page"
CHUNK_STREAM = "web_chunk"
BACKEND_SERVERS = {"webarchiver": WEBARCHIVER_SERVER, "archivebox": ARCHIVEBOX_SERVER}

_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src)$")
_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 150


#: webarchiver failures that are retried once through ArchiveBox; every other
#: refusal (robots_disallowed, policy_refused, egress_denied, ...) is final.
FALLBACK_REASONS = frozenset({"upstream_failed", "unreachable"})


class ResearchWebParams(Params):
    query: str | None = Field(default=None, min_length=1, max_length=512)
    urls: tuple[str, ...] = Field(default=(), max_length=32)
    max_pages: int = Field(default=5, ge=1, le=32)
    backend: Literal["webarchiver", "archivebox"] = "webarchiver"
    freshness_window_s: int = Field(default=86_400, ge=0, le=31_536_000)
    deadline_s: int = Field(default=60, ge=1, le=300)

    @model_validator(mode="after")
    def _query_or_urls(self) -> ResearchWebParams:
        if not self.query and not self.urls:
            raise ValueError("research.web needs a query or 1 to 32 urls")
        return self


class ResearchWebResult(Params):
    value: dict[str, Any]


class FetchFailed(Exception):
    """A backend could not produce a page for one URL."""

    def __init__(self, backend: str, reason: str) -> None:
        self.backend = backend
        self.reason = reason
        super().__init__(f"{backend}: {reason}")


@dataclass(frozen=True)
class SearchHit:
    url: str
    canonical_url: str
    title: str
    snippet: str
    engine: str
    rank: int
    score: float = 0.0


@dataclass(frozen=True)
class WebPage:
    """One fetched page, identical in shape whichever backend produced it."""

    url: str
    canonical_url: str
    title: str
    text: str
    backend: str
    fetched_at: str
    author: str = ""
    published: str = ""
    site_name: str = ""
    content_sha256: str = ""
    fetch_mode: str = "http"

    def digest(self) -> str:
        return self.content_sha256 or hashlib.sha256(self.text.encode()).hexdigest()


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    index: int
    start: int
    end: int
    text: str


@dataclass
class IngestedDocument:
    document_id: str
    page: WebPage
    hit: SearchHit
    chunks: tuple[Chunk, ...]
    embedded: int = 0
    fallback_from: str = ""
    query: str = ""

    def citation(self, chunk: Chunk) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_id": chunk.chunk_id,
            "url": self.page.canonical_url,
            "title": self.page.title,
            "char_start": chunk.start,
            "char_end": chunk.end,
            "fetched_at": self.page.fetched_at,
            "content_sha256": self.page.digest(),
            "backend": self.page.backend,
            "query": self.query,
            "rank": self.hit.rank,
        }


@runtime_checkable
class WebIngest(Protocol):
    """Commits documents and their chunks through SourceIngest."""

    async def commit(self, documents: Sequence[IngestedDocument]) -> dict[str, Any]: ...


@runtime_checkable
class Embedder(Protocol):
    """Embeds chunk text and writes the vectors into the engine's index."""

    async def embed(self, chunks: Sequence[Chunk]) -> int: ...


# --------------------------------------------------------------------- select


def canonical_url(url: str) -> str:
    """Lower-cased host, no fragment, no tracking parameters, no trailing slash."""
    parts = urlsplit(url.strip())
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAMS.match(k)
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def search_hits(payload: Any) -> list[SearchHit]:
    """SearXNG results as ranked hits; non-http(s) results are not eligible."""
    data = _unwrap(payload)
    results = data.get("results") if isinstance(data, Mapping) else None
    hits: list[SearchHit] = []
    for item in results or ():
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "")
        if urlsplit(url).scheme not in {"http", "https"}:
            continue
        hits.append(
            SearchHit(
                url=url,
                canonical_url=canonical_url(url),
                title=str(item.get("title") or ""),
                snippet=str(item.get("content") or "")[:500],
                engine=str(item.get("engine") or ""),
                rank=len(hits) + 1,
                score=float(item.get("score") or 0.0),
            )
        )
    return hits


def select_hits(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Rank order, then the first hit per canonical URL."""
    seen: set[str] = set()
    chosen: list[SearchHit] = []
    for hit in sorted(hits, key=lambda h: h.rank):
        if hit.canonical_url in seen:
            continue
        seen.add(hit.canonical_url)
        chosen.append(hit)
    return chosen


def document_id(canonical: str) -> str:
    return "web:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def chunk_text(doc_id: str, text: str) -> tuple[Chunk, ...]:
    """Overlapping, paragraph-aligned windows with exact character offsets."""
    chunks: list[Chunk] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + _CHUNK_CHARS)
        if end < length:
            cut = text.rfind("\n", start + _CHUNK_CHARS // 2, end)
            if cut == -1:
                cut = text.rfind(". ", start + _CHUNK_CHARS // 2, end)
            if cut != -1:
                end = cut + 1
        body = text[start:end].strip()
        if body:
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#c{len(chunks)}",
                    index=len(chunks),
                    start=start,
                    end=end,
                    text=body,
                )
            )
        if end >= length:
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return tuple(chunks)


# ---------------------------------------------------------------------- fleet


def _unwrap(value: Any) -> Any:
    """The structured payload of a fleet tool result."""
    if isinstance(value, Mapping):
        for key in ("structured_content", "structuredContent"):
            inner = value.get(key)
            if isinstance(inner, Mapping):
                return _unwrap(inner)
        if set(value) == {"result"}:
            return _unwrap(value["result"])
        content = value.get("content")
        if isinstance(content, list) and content and "results" not in value:
            first = content[0]
            text = first.get("text") if isinstance(first, Mapping) else None
            if isinstance(text, str):
                import json

                try:
                    return _unwrap(json.loads(text))
                except ValueError:
                    return {"text": text}
        return value
    return value


def _reason(error: str) -> str:
    """The stable reason code a webarchiver refusal starts with."""
    head = error.strip().split(":", 1)[0].strip().strip("[]'\"")
    return (
        head
        if head.replace("_", "").isalpha() and head.islower()
        else "upstream_failed"
    )


def _is_error(value: Any) -> str:
    if isinstance(value, Mapping):
        if value.get("isError") or value.get("is_error"):
            return str(value.get("content") or "tool error")[:300]
        if "error" in value and len(value) <= 3:
            return str(value["error"])[:300]
    return ""


@dataclass
class FleetCaller:
    """Calls fleet children through the same authority ``fleet.call`` uses."""

    context: Any

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        from arbora.api.ops import fleet

        gateway = self.context.services.get("fleet_gateway")
        if gateway is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "fleet gateway is not bound"}
            )
        call_op = next(op for op in fleet.operations() if op.id == "fleet.call")
        params = {"server": server, "tool": tool, "arguments": arguments}
        caller = self.context.caller
        decision = await gateway.effect(call_op, params, caller)
        missing = set(decision.required_scopes) - set(caller.effective_scopes)
        if missing:
            raise OperationRefused(
                "SCOPE_DENIED", {"server": server, "missing": sorted(missing)}
            )
        return await gateway.call(
            caller,
            server,
            tool,
            arguments,
            expected_effect=decision.effect,
            service_identity=self.context.service_identity,
            owner=self.context.owner,
            owner_ref=getattr(self.context, "owner_ref", None),
            fleet_decision=decision,
            registry_digest=self.context.registry_digest,
        )


async def _fetch_web_source(
    fleet: FleetCaller, backend: str, server: str, hit: SearchHit
) -> WebPage:
    """One page through a connector's SDK web-source contract ``web_extract``.

    The argument is ``{"request": WebFetchRequest}``; the answer is a
    ``WebPageResult`` (``kind: page``) or a ``WebFetchRefusal`` (``kind:
    refusal``) of ``agent_connector_sdk.ports.web_source``.
    """
    try:
        raw = await fleet.call(server, WEB_EXTRACT, {"request": {"url": hit.url}})
    except OperationRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - a child that cannot answer is unreachable
        raise FetchFailed(backend, "unreachable") from exc
    error = _is_error(raw)
    if error:
        raise FetchFailed(backend, _reason(error))
    data = _unwrap(raw)
    if not isinstance(data, Mapping):
        raise FetchFailed(backend, "upstream_failed")
    if data.get("kind") == "refusal":
        raise FetchFailed(backend, _reason(str(data.get("reason") or "")))
    text = str(data.get("text_markdown") or "")
    if not text.strip():
        raise FetchFailed(backend, "extract_empty")
    return WebPage(
        url=hit.url,
        canonical_url=canonical_url(
            str(data.get("canonical_url") or data.get("final_url") or hit.url)
        ),
        title=str(data.get("title") or hit.title),
        text=text,
        backend=backend,
        fetched_at=str(data.get("retrieved_at") or _now()),
        author=str(data.get("author") or ""),
        published=str(data.get("published_at") or ""),
        site_name="",
        content_sha256=str(data.get("content_digest") or ""),
        fetch_mode=str(data.get("backend") or backend),
    )


async def fetch_webarchiver(fleet: FleetCaller, hit: SearchHit) -> WebPage:
    return await _fetch_web_source(fleet, "webarchiver", WEBARCHIVER_SERVER, hit)


async def fetch_archivebox(fleet: FleetCaller, hit: SearchHit) -> WebPage:
    return await _fetch_web_source(fleet, "archivebox", ARCHIVEBOX_SERVER, hit)


async def fetch_page(
    fleet: FleetCaller, hit: SearchHit, params: Mapping[str, Any]
) -> tuple[WebPage, str]:
    """Fetch through the chosen backend (ARBORA-OPS-R067.3).

    webarchiver is primary. A page it fails on upstream (unreachable, timeout,
    HTTP or render failure) is fetched once more through ArchiveBox; a policy
    refusal such as ``robots_disallowed`` is final and never retried.
    """
    if params["backend"] == "archivebox":
        return await fetch_archivebox(fleet, hit), ""
    try:
        return await fetch_webarchiver(fleet, hit), ""
    except FetchFailed as failed:
        if failed.reason not in FALLBACK_REASONS:
            raise
        return await fetch_archivebox(fleet, hit), failed.reason


# ------------------------------------------------------------ ingest / enrich


@dataclass
class SourceIngestWriter:
    """Commits pages and their chunks through the SDK's SourceIngest facade."""

    client: Any

    async def commit(self, documents: Sequence[IngestedDocument]) -> dict[str, Any]:
        from agent_connector_sdk.ingest import (
            ChangeSet,
            Document,
            IngestBinding,
            KnowledgeIngest,
        )
        from agent_connector_sdk.ingest.transport import (
            EpistemicGraphIngestTransport,
        )

        ingest = KnowledgeIngest(EpistemicGraphIngestTransport(self.client))
        affected = relationships = 0
        for backend in sorted({doc.page.backend for doc in documents}):
            batch = [doc for doc in documents if doc.page.backend == backend]
            connector = BACKEND_SERVERS[backend]
            for binding, records in (
                (
                    IngestBinding(connector=connector, stream=PAGE_STREAM),
                    [_page_document(Document, doc) for doc in batch],
                ),
                (
                    IngestBinding(
                        connector=connector, stream=CHUNK_STREAM, document_type="Chunk"
                    ),
                    [
                        _chunk_document(Document, doc, chunk)
                        for doc in batch
                        for chunk in doc.chunks
                    ],
                ),
            ):
                if not records:
                    continue
                receipt = await ingest.submit(
                    binding, ChangeSet(documents=tuple(records))
                )
                affected += receipt.affected_count
                relationships += receipt.relationship_count
        return {"affected": affected, "relationships": relationships}


def _page_document(document_cls: Any, doc: IngestedDocument) -> Any:
    page = doc.page
    return document_cls(
        id=doc.document_id,
        text=page.text,
        title=page.title,
        source_uri=page.canonical_url,
        updated_at=page.fetched_at,
        properties=_document_properties(doc),
    )


def _chunk_document(document_cls: Any, doc: IngestedDocument, chunk: Chunk) -> Any:
    page = doc.page
    return document_cls(
        id=chunk.chunk_id,
        text=chunk.text,
        title=f"{page.title} [{chunk.index}]",
        source_uri=page.canonical_url,
        updated_at=page.fetched_at,
        properties={
            "document_id": doc.document_id,
            "chunk_index": chunk.index,
            "char_start": chunk.start,
            "char_end": chunk.end,
            "backend": page.backend,
        },
    )


def _document_properties(doc: IngestedDocument) -> dict[str, Any]:
    page, hit = doc.page, doc.hit
    return {
        "url": page.url,
        "canonical_url": page.canonical_url,
        "author": page.author,
        "published": page.published,
        "site_name": page.site_name,
        "content_sha256": page.digest(),
        "backend": page.backend,
        "fetch_mode": page.fetch_mode,
        "fallback_from": doc.fallback_from,
        "search_query": doc.query,
        "search_engine": hit.engine,
        "search_rank": hit.rank,
        "chunk_count": len(doc.chunks),
    }


@dataclass
class EngineEmbedder:
    """Embeds chunks with the configured model and writes ``AddEmbedding``."""

    client: Any
    model: Any

    async def embed(self, chunks: Sequence[Chunk]) -> int:
        if not chunks:
            return 0
        vectors = await asyncio.to_thread(
            self.model.get_text_embedding_batch, [c.text for c in chunks]
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            await self.client._send(
                "AddEmbedding", {"node_id": chunk.chunk_id, "embedding": list(vector)}
            )
        return len(chunks)


def _embedder(context: Any) -> tuple[Embedder | None, str]:
    bound = context.services.get("embedder")
    if bound is not None:
        return bound, ""
    try:
        from agent_connector_sdk.embeddings import create_embedding_model

        return EngineEmbedder(context.client, create_embedding_model()), ""
    except ValueError as exc:
        return None, f"embedding model not configured: {exc}"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


# ------------------------------------------------------------------ eg_enrich


@runtime_checkable
class EgEnrich(Protocol):
    """The engine-owned selection and enrichment seam, bound as ``eg_enrich``."""

    async def select(
        self, hits: Sequence[SearchHit], *, max_pages: int, freshness_window_s: int
    ) -> list[SearchHit]: ...

    def chunk(self, doc_id: str, text: str) -> tuple[Chunk, ...]: ...


class InterimEnrich:
    """arbora-side stand-ins until the engine serves these methods.

    TODO(R067.1): replace ``select`` with the engine page-selection decision
    (EG-DECISION-ENGINE-R802) and honour ``freshness_window_s`` there.
    TODO(R651.7): replace ``chunk`` with the engine enrichment pipeline
    (EG-UNIFIED-DATA-PLANE-R651.7) once epistemic-graph 2.27.4 serves it.
    """

    async def select(
        self, hits: Sequence[SearchHit], *, max_pages: int, freshness_window_s: int
    ) -> list[SearchHit]:
        del freshness_window_s
        return select_hits(hits)[: max_pages * 2]

    def chunk(self, doc_id: str, text: str) -> tuple[Chunk, ...]:
        return chunk_text(doc_id, text)


def url_hits(urls: Sequence[str]) -> list[SearchHit]:
    """Caller-named URLs as the candidate set, in the order given."""
    return [
        SearchHit(
            url=url,
            canonical_url=canonical_url(url),
            title="",
            snippet="",
            engine="caller",
            rank=rank,
        )
        for rank, url in enumerate(urls, start=1)
        if urlsplit(url).scheme in {"http", "https"}
    ]


# -------------------------------------------------------------------- handler


async def handle_research_web(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Search, select, fetch, ingest and enrich; return Documents with citations."""
    fleet = FleetCaller(context)
    enrich: EgEnrich = context.services.get("eg_enrich") or InterimEnrich()
    searched_at = _now()
    query = params.get("query") or ""
    if params.get("urls"):
        candidates = url_hits(params["urls"])
    else:
        raw = await fleet.call(SEARCH_SERVER, SEARCH_TOOL, {"query": query})
        candidates = search_hits(raw)
    hits = await enrich.select(
        candidates,
        max_pages=params["max_pages"],
        freshness_window_s=params["freshness_window_s"],
    )
    budget = params["max_pages"]
    deadline = asyncio.get_running_loop().time() + params["deadline_s"]
    documents: list[IngestedDocument] = []
    skipped: list[dict[str, str]] = []
    digests: set[str] = set()
    timed_out = False
    for hit in hits:
        if len(documents) >= budget:
            break
        if asyncio.get_running_loop().time() >= deadline:
            timed_out = True
            break
        try:
            page, fallback_from = await fetch_page(fleet, hit, params)
        except FetchFailed as failed:
            skipped.append(
                {"url": hit.url, "backend": failed.backend, "reason": failed.reason}
            )
            continue
        digest = page.digest()
        if digest in digests:
            skipped.append(
                {"url": hit.url, "backend": page.backend, "reason": "duplicate_content"}
            )
            continue
        digests.add(digest)
        doc_id = document_id(page.canonical_url)
        documents.append(
            IngestedDocument(
                document_id=doc_id,
                page=page,
                hit=hit,
                chunks=enrich.chunk(doc_id, page.text),
                fallback_from=fallback_from,
                query=query,
            )
        )
    receipt: dict[str, Any] = {}
    embedding_note = ""
    if documents:
        writer: WebIngest = context.services.get("web_ingest") or SourceIngestWriter(
            context.client
        )
        receipt = await writer.commit(documents)
        embedder, embedding_note = _embedder(context)
        if embedder is not None:
            for doc in documents:
                doc.embedded = await embedder.embed(doc.chunks)
    if timed_out or (skipped and documents):
        state = "partial"
    elif documents:
        state = "complete"
    else:
        state = "partial"
    return {
        "value": {
            # The engine work item and decision IDs arrive with ARBORA-OPS-R067.1.
            "work_item_id": None,
            "decision_id": None,
            "state": state,
            "query": query,
            "search": {
                "server": SEARCH_SERVER,
                "searched_at": searched_at,
                "results": len(hits),
            },
            "documents": [
                {
                    "document_id": doc.document_id,
                    "url": doc.page.canonical_url,
                    "title": doc.page.title,
                    "backend": doc.page.backend,
                    "fallback_from": doc.fallback_from,
                    "search_rank": doc.hit.rank,
                    "search_engine": doc.hit.engine,
                    "chunks": len(doc.chunks),
                    "embedded": doc.embedded,
                    "citations": [doc.citation(c) for c in doc.chunks],
                }
                for doc in documents
            ],
            "skipped": skipped,
            "fallbacks": sum(1 for doc in documents if doc.fallback_from),
            "ingest": receipt,
            "embedding": embedding_note or "ok",
        }
    }


#: Operations whose replay safety the engine's SourceIngest authority owns:
#: every page and chunk record has a deterministic id (from its canonical URL)
#: under its connector and stream, so a replayed call upserts the same records.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "research.web": "epistemic_graph:SourceIngest",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="research.web",
            verb=Verb.ACT,
            summary="Search the web, fetch and ingest the best pages, and return cited documents",
            examples=("research kubernetes pod disruption budgets on the web",),
            params=ResearchWebParams,
            result=ResearchWebResult,
            binding=Composite(handler="arbora.api.ops.research.handle_research_web"),
            scopes=frozenset({"ingest:write", "mcp:delegate"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.NATURAL,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations

__all__ = [
    "Chunk",
    "EgEnrich",
    "InterimEnrich",
    "FALLBACK_REASONS",
    "EngineEmbedder",
    "FetchFailed",
    "FleetCaller",
    "IngestedDocument",
    "NATIVE_IDEMPOTENCY",
    "ResearchWebParams",
    "ResearchWebResult",
    "SearchHit",
    "SourceIngestWriter",
    "WebPage",
    "canonical_url",
    "chunk_text",
    "document_id",
    "handle_research_web",
    "operations",
    "search_hits",
    "select_hits",
    "specs",
]
