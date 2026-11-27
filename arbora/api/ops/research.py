"""``research.web``: search, select, fetch, ingest and enrich web pages.

ARBORA-OPS-R067. One served operation composes the web-ingest pipeline under the
caller's own authority, inside one engine work item (``research_web``):

1. **Search** -- ``fleet.call`` to the searxng connector yields the candidate set. It
   is a short-lived source carrying query, engine, rank and time provenance; it is not
   ingested.
2. **Select** -- the engine decides which candidates to fetch (``SelectWebPages``,
   MEDULLA-DECISION-ENGINE-R802.3). Arbora computes no ranking and no de-duplication.
3. **Fetch** -- ``fleet.call`` to the webarchiver connector (``web_extract``), which
   enforces robots.txt, rate, size and content-type policy. ArchiveBox is the secondary
   backend: selectable with ``backend="archivebox"`` and the automatic fallback when
   webarchiver fails on a URL. Each call is first admitted by the engine's conformance
   evidence for the connector (ARBORA-OPS-R067.10).
4. **Ingest** -- each page is committed through ``SourceIngest`` as a Document with
   provenance (query, rank, engine, backend, fetch time).
5. **Enrich** -- the engine splits, embeds and links each Document (``EnrichDocument``,
   MEDULLA-UNIFIED-DATA-PLANE-R651.7). Arbora stores no page text beyond the Document
   it ingests and chunks nothing itself.

Every fleet call is admitted through the same effect authority ``fleet.call`` uses, so a
caller can only reach connectors its own scopes allow.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import re
import time
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
    ContentOrigin,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)

logger = logging.getLogger(__name__)

SEARCH_SERVER = "searxng-mcp"
SEARCH_TOOL = "web_search"
WEBARCHIVER_SERVER = "webarchiver-mcp"
#: The SDK web-source contract tool both backends serve.
WEB_EXTRACT = "web_extract"
ARCHIVEBOX_SERVER = "archivebox-mcp"

#: The SourceIngest streams pages and chunks are committed under. Each record
#: is bound to the fleet connector that fetched it. The Document and Chunk
#: record types resolve against the ``connector://resources`` declaration that
#: connector served and Arbora's fleet onboarding captured (ARBORA-FLEET-R041.13).
PAGE_STREAM = "web_page"
#: The engine work item kind one ``research.web`` call runs under (R067.2).
WORK_ITEM_KIND = "research_web"
#: The caller scope the operation's writes need; checked again before any engine effect.
INGEST_SCOPE = "ingest:write"
BACKEND_SERVERS = {"webarchiver": WEBARCHIVER_SERVER, "archivebox": ARCHIVEBOX_SERVER}

_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src)$")


#: webarchiver failures that are retried once through ArchiveBox; every other
#: refusal (robots_disallowed, policy_refused, egress_denied, ...) is final.
FALLBACK_REASONS = frozenset({"upstream_failed", "unreachable", "timeout"})
#: The bound on one backend's ``web_extract`` call (ARBORA-OPS-R067.9). A
#: backend that does not answer in time is a ``timeout`` failure, never a hang.
PAGE_TIMEOUT_S = 30.0
#: The other backend tried once after a fallback-eligible failure.
_FALLBACK_BACKEND = {"webarchiver": "archivebox", "archivebox": "webarchiver"}


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


@dataclass
class IngestedDocument:
    document_id: str
    page: WebPage
    hit: SearchHit
    #: The chunk node ids the engine's ``EnrichDocument`` made (empty until enriched).
    chunk_ids: tuple[str, ...] = ()
    fallback_from: str = ""
    query: str = ""
    #: ``done`` once the engine enriched the Document; ``pending`` past the deadline.
    enrichment: str = "pending"

    def citation(self, chunk_id: str) -> dict[str, Any]:
        """One chunk's citation: URL, retrieval time, content digest, backend, query, rank."""
        return {
            "document_id": self.document_id,
            "chunk_id": chunk_id,
            "url": self.page.canonical_url,
            "retrieved_at": self.page.fetched_at,
            "content_digest": self.page.digest(),
            "backend": self.page.backend,
            "query": self.query,
            "rank": self.hit.rank,
        }


@runtime_checkable
class WebIngest(Protocol):
    """Commits documents and their chunks through SourceIngest."""

    async def commit(self, documents: Sequence[IngestedDocument]) -> dict[str, Any]: ...


# ------------------------------------------------------------------ candidates


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


def document_id(canonical: str) -> str:
    return "web:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


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

    async def decision(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        """The caller-scope-checked effect decision for one fleet tool call."""
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
        return decision

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        decision = await self.decision(server, tool, arguments)
        caller = self.context.caller
        return await self.context.services["fleet_gateway"].call(
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


async def _admitted_extract(fleet: FleetCaller, server: str, url: str) -> Any:
    """One ``web_extract`` call, after the engine's conformance evidence admits it."""
    from arbora.api.domain.fleet.conformance import require_current_evidence

    await require_current_evidence(fleet.context, server)
    return await fleet.call(server, WEB_EXTRACT, {"request": {"url": url}})


async def _fetch_web_source(
    fleet: FleetCaller,
    backend: str,
    server: str,
    hit: SearchHit,
    timeout_s: float = PAGE_TIMEOUT_S,
) -> WebPage:
    """One page through a connector's SDK web-source contract ``web_extract``.

    The argument is ``{"request": WebFetchRequest}``; the answer is a
    ``WebPageResult`` (``kind: page``) or a ``WebFetchRefusal`` (``kind:
    refusal``) of ``scion_sdk.ports.web_source``.
    """
    if timeout_s <= 0:
        raise FetchFailed(backend, "timeout")
    try:
        raw = await asyncio.wait_for(
            _admitted_extract(fleet, server, hit.url),
            timeout=timeout_s,
        )
    except OperationRefused:
        raise
    except TimeoutError as exc:
        logger.warning("research.web %s web_extract timed out", backend)
        raise FetchFailed(backend, "timeout") from exc
    except Exception as exc:  # a child that cannot answer is unreachable
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


async def fetch_webarchiver(
    fleet: FleetCaller, hit: SearchHit, timeout_s: float = PAGE_TIMEOUT_S
) -> WebPage:
    return await _fetch_web_source(
        fleet, "webarchiver", WEBARCHIVER_SERVER, hit, timeout_s
    )


async def fetch_archivebox(
    fleet: FleetCaller, hit: SearchHit, timeout_s: float = PAGE_TIMEOUT_S
) -> WebPage:
    return await _fetch_web_source(
        fleet, "archivebox", ARCHIVEBOX_SERVER, hit, timeout_s
    )


def _clock() -> float:
    """The operation clock: the running loop's monotonic time (tests replace it)."""
    return asyncio.get_running_loop().time()


def _page_timeout(deadline: float | None) -> float:
    """The page bound, never past the operation deadline."""
    if deadline is None:
        return PAGE_TIMEOUT_S
    remaining = deadline - _clock()
    return min(PAGE_TIMEOUT_S, max(0.0, remaining))


async def fetch_page(
    fleet: FleetCaller,
    hit: SearchHit,
    params: Mapping[str, Any],
    deadline: float | None = None,
) -> tuple[WebPage, str]:
    """Fetch through the chosen backend (ARBORA-OPS-R067.3).

    webarchiver is primary. A page the chosen backend fails on upstream
    (unreachable, a ``web_extract`` call past ``PAGE_TIMEOUT_S``, HTTP or
    render failure) is fetched once more through the other backend; a policy
    refusal such as ``robots_disallowed`` is final and never retried. Every
    call is bounded by the page timeout and the operation deadline
    (ARBORA-OPS-R067.9).
    """
    fetchers = {"webarchiver": fetch_webarchiver, "archivebox": fetch_archivebox}
    chosen = str(params["backend"])
    try:
        return await fetchers[chosen](fleet, hit, _page_timeout(deadline)), ""
    except FetchFailed as failed:
        if failed.reason not in FALLBACK_REASONS:
            raise
        other = _FALLBACK_BACKEND[chosen]
        page = await fetchers[other](fleet, hit, _page_timeout(deadline))
        return page, failed.reason


# ------------------------------------------------------------ ingest / enrich


@dataclass
class SourceIngestWriter:
    """Commits pages through the SDK's SourceIngest facade; the engine chunks them."""

    client: Any
    #: The fleet's captured declarations (the ``fleet_declarations`` binding).
    declarations: Any = None

    async def commit(self, documents: Sequence[IngestedDocument]) -> dict[str, Any]:
        from scion_sdk.ingest import (
            ChangeSet,
            Document,
            IngestBinding,
            KnowledgeIngest,
        )
        from scion_sdk.ingest.transport import (
            MedullaIngestTransport,
        )

        ingest = KnowledgeIngest(MedullaIngestTransport(self.client))
        affected = relationships = 0
        for backend in sorted({doc.page.backend for doc in documents}):
            batch = [doc for doc in documents if doc.page.backend == backend]
            connector = BACKEND_SERVERS[backend]
            _require_declared_mappings(self.declarations, connector)
            records = [_page_document(Document, doc) for doc in batch]
            receipt = await ingest.submit(
                IngestBinding(connector=connector, stream=PAGE_STREAM),
                ChangeSet(documents=tuple(records)),
            )
            affected += receipt.affected_count
            relationships += receipt.relationship_count
        return {"affected": affected, "relationships": relationships}


def _require_declared_mappings(declarations: Any, connector: str) -> None:
    """Refuse when a captured declaration lacks the Document mapping.

    The mapping comes from the connector's own ``connector://resources``
    declaration, held by the fleet's ``Declarations``. A connector whose
    declaration was not captured yet (or a process with no fleet) is left to
    the engine, which refuses an unknown mapping itself.
    """
    if declarations is None or declarations.declaration_of(connector) is None:
        return
    missing = [
        record_type
        for record_type in ("Document",)
        if declarations.declared_mapping(connector, record_type) is None
    ]
    if missing:
        raise OperationRefused(
            "MAPPING_UNDECLARED",
            {"connector": connector, "record_types": missing},
        )


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
        "chunk_count": len(doc.chunk_ids),
    }


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


# ------------------------------------------------------------------ eg_enrich


@dataclass(frozen=True)
class Selection:
    """The engine's page-selection answer (``SelectWebPages``)."""

    decision_id: str
    selected: tuple[SearchHit, ...]
    skipped: tuple[dict[str, str], ...] = ()


@runtime_checkable
class EgEnrich(Protocol):
    """The engine-owned selection and enrichment seam, bound as ``eg_enrich``."""

    async def choose(
        self,
        candidates: Sequence[SearchHit],
        *,
        query: str,
        max_pages: int,
        freshness_window_s: int,
    ) -> Selection: ...

    async def enrich(self, document_id: str, backend: str) -> tuple[str, ...]: ...


def _selection_request(
    candidates: Sequence[SearchHit], query: str, max_pages: int, freshness_window_s: int
) -> dict[str, Any]:
    """The ``SelectWebPages`` request: candidates in rank order, never reordered here."""
    return {
        "request": {
            "query": query,
            "max_fetches": min(2 * max_pages, 64),
            "freshness_window_s": freshness_window_s,
            "now_ms": int(time.time() * 1000),
            "candidates": [
                {
                    "url": hit.url,
                    "rank": hit.rank,
                    "title": hit.title,
                    "snippet": hit.snippet,
                    "engine": hit.engine,
                }
                for hit in candidates
            ],
        }
    }


def _chosen_hits(
    selected: Sequence[Mapping[str, Any]], candidates: Sequence[SearchHit]
) -> tuple[SearchHit, ...]:
    """The selected pages as hits, in the engine's ``order``."""
    known: dict[str, SearchHit] = {}
    for hit in candidates:
        known.setdefault(hit.canonical_url, hit)
        known.setdefault(hit.url, hit)
    ordered = sorted(selected, key=lambda page: int(page.get("order", 0)))
    return tuple(_hit_for(page, known) for page in ordered)


def _hit_for(page: Mapping[str, Any], known: Mapping[str, SearchHit]) -> SearchHit:
    url = str(page["canonical_url"])
    return known.get(url) or SearchHit(
        url=url,
        canonical_url=canonical_url(url),
        title=str(page.get("title") or ""),
        snippet="",
        engine="engine",
        rank=int(page.get("order", 0)) + 1,
    )


def _engine_skips(answer: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "url": str(item.get("url") or ""),
            "backend": "",
            "reason": str(item.get("reason") or "skipped"),
        }
        for item in answer.get("skipped") or ()
        if isinstance(item, Mapping)
    )


@dataclass
class EngineEnrich:
    """Selection and enrichment through the caller's engine client only (R067.2).

    Arbora ranks nothing and chunks nothing: ``SelectWebPages``
    (MEDULLA-DECISION-ENGINE-R802.3) picks the pages and ``EnrichDocument``
    (MEDULLA-UNIFIED-DATA-PLANE-R651.7) makes the chunks and vectors.
    """

    context: Any

    async def _send(self, method: str, params: dict[str, Any]) -> Any:
        from arbora.api.ops.engine_calls import engine_send

        try:
            return await engine_send(self.context, method, params)
        except OperationRefused as refused:
            if refused.code != "UNAVAILABLE":
                raise
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "decision_unavailable", "method": method}
            ) from refused
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "decision_unavailable", "method": method}
            ) from exc

    async def choose(
        self,
        candidates: Sequence[SearchHit],
        *,
        query: str,
        max_pages: int,
        freshness_window_s: int,
    ) -> Selection:
        answer = await self._send(
            "SelectWebPages",
            _selection_request(candidates, query, max_pages, freshness_window_s),
        )
        if not isinstance(answer, Mapping) or not isinstance(
            answer.get("selected"), list
        ):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "decision_unavailable", "shape": "unknown"}
            )
        decision = f"{answer.get('query_digest', '')}@{answer.get('now_ms', 0)}"
        return Selection(
            decision_id=decision,
            selected=_chosen_hits(answer["selected"], candidates),
            skipped=_engine_skips(answer),
        )

    async def enrich(self, document_id: str, backend: str) -> tuple[str, ...]:
        receipt = await self._send(
            "EnrichDocument",
            {"request": {"document_id": document_id, "fetch_backend": backend}},
        )
        ids = receipt.get("chunk_ids") if isinstance(receipt, Mapping) else None
        if not isinstance(ids, list):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "enrichment_unavailable", "shape": "unknown"}
            )
        return tuple(str(i) for i in ids)


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


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _work_item_key(tenant: str, subject: str, freshness_window_s: int) -> str:
    """Query digest plus caller tenant (plus the freshness bucket, so a stale query reruns)."""
    bucket = int(time.time() // freshness_window_s) if freshness_window_s else 0
    return _digest(f"{WORK_ITEM_KIND}|{tenant}|{_digest(subject)}|{bucket}")


def _research_precheck(context: Any, params: Mapping[str, Any]) -> None:
    """Refuse before any search, fetch or ingest (ARBORA-OPS-R067.4, R067.5)."""
    if params.get("backend", "webarchiver") not in BACKEND_SERVERS:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "backend"})
    if INGEST_SCOPE not in set(context.caller.effective_scopes):
        raise OperationRefused("SCOPE_REQUIRED", {"missing": INGEST_SCOPE})
    if context.client is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine_unavailable"})
    if "web_ingest" in context.services and context.services["web_ingest"] is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "ingest_unavailable"})


async def _open_work_item(
    context: Any, key: str, deadline_s: int
) -> tuple[Any, str, Mapping[str, Any]]:
    """Submit and claim the ``research_web`` work item (``submit_work_item_atomic``)."""
    from arbora import work_items

    engine = context.services.get("task_plan_work_engine")
    if engine is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "work_item_unavailable"})
    item_id, _created = await asyncio.to_thread(
        work_items.submit_work_item_atomic,
        engine,
        kind=WORK_ITEM_KIND,
        idempotency_key=key,
        tenant=str(context.caller.tenant),
        description="research.web",
    )
    claim = await asyncio.to_thread(
        work_items.claim_specific,
        engine,
        item_id,
        token=f"arbora-research-{item_id}",
        lease_ttl_s=float(deadline_s + 60),
    )
    if claim is None:
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": "work_item_not_claimable", "work_item_id": item_id},
        )
    return engine, item_id, claim


async def _commit_work_item(
    engine: Any, item_id: str, claim: Mapping[str, Any], outcome: str
) -> None:
    from arbora import work_items

    await asyncio.to_thread(
        work_items.commit_result, engine, item_id, claim, outcome=outcome
    )


async def _fetch_selected(
    fleet: FleetCaller,
    selection: Selection,
    params: Mapping[str, Any],
    query: str,
    deadline: float,
) -> tuple[list[IngestedDocument], list[dict[str, str]], bool]:
    """Fetch only the engine-selected URLs, bounded by the operation deadline."""
    documents: list[IngestedDocument] = []
    skipped: list[dict[str, str]] = list(selection.skipped)
    digests: set[str] = set()
    for index, hit in enumerate(selection.selected):
        if len(documents) >= int(params["max_pages"]):
            break
        if _clock() >= deadline:
            skipped.extend(
                {"url": rest.url, "backend": "", "reason": "deadline"}
                for rest in selection.selected[index:]
            )
            return documents, skipped, True
        try:
            page, fallback_from = await fetch_page(fleet, hit, params, deadline)
        except FetchFailed as failed:
            skipped.append(
                {"url": hit.url, "backend": failed.backend, "reason": failed.reason}
            )
            continue
        if page.digest() in digests:
            skipped.append(
                {"url": hit.url, "backend": page.backend, "reason": "duplicate_content"}
            )
            continue
        digests.add(page.digest())
        documents.append(
            IngestedDocument(
                document_id=document_id(page.canonical_url),
                page=page,
                hit=hit,
                fallback_from=fallback_from,
                query=query,
            )
        )
    return documents, skipped, False


async def _enrich_all(
    enrich: EgEnrich, documents: Sequence[IngestedDocument], deadline: float
) -> bool:
    """Enrich each stored Document until the deadline; ``True`` when all settled."""
    for doc in documents:
        remaining = deadline - _clock()
        if remaining <= 0:
            return False
        try:
            doc.chunk_ids = await asyncio.wait_for(
                enrich.enrich(doc.document_id, doc.page.backend), timeout=remaining
            )
        except TimeoutError:
            return False
        doc.enrichment = "done"
    return True


def _document_entry(doc: IngestedDocument) -> dict[str, Any]:
    return {
        "document_id": doc.document_id,
        "url": doc.page.canonical_url,
        "title": doc.page.title,
        "backend": doc.page.backend,
        "fallback_from": doc.fallback_from,
        "search_rank": doc.hit.rank,
        "search_engine": doc.hit.engine,
        "chunks": len(doc.chunk_ids),
        "enrichment": doc.enrichment,
        "citations": [doc.citation(chunk_id) for chunk_id in doc.chunk_ids],
    }


async def handle_research_web(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Search, select (engine), fetch, ingest and enrich (engine); return cited Documents."""
    _research_precheck(context, params)
    fleet = FleetCaller(context)
    enrich: EgEnrich = context.services.get("eg_enrich") or EngineEnrich(context)
    query = params.get("query") or ""
    subject = query or " ".join(params["urls"])
    searched_at = _now()
    if params.get("urls"):
        candidates = url_hits(params["urls"])
    else:
        raw = await fleet.call(SEARCH_SERVER, SEARCH_TOOL, {"query": query})
        candidates = search_hits(raw)
    engine, item_id, claim = await _open_work_item(
        context,
        _work_item_key(
            str(context.caller.tenant), subject, params.get("freshness_window_s", 0)
        ),
        params["deadline_s"],
    )
    try:
        selection = await enrich.choose(
            candidates,
            query=subject,
            max_pages=params["max_pages"],
            freshness_window_s=params["freshness_window_s"],
        )
        deadline = _clock() + params["deadline_s"]
        documents, skipped, timed_out = await _fetch_selected(
            fleet, selection, params, query, deadline
        )
        receipt: dict[str, Any] = {}
        enriched = True
        if documents:
            writer: WebIngest = context.services.get(
                "web_ingest"
            ) or SourceIngestWriter(
                context.client, context.services.get("fleet_declarations")
            )
            receipt = await writer.commit(documents)
            enriched = await _enrich_all(enrich, documents, deadline)
    except BaseException:
        await _commit_work_item(engine, item_id, claim, "failed")
        raise
    if enriched:
        await _commit_work_item(engine, item_id, claim, "succeeded")
    state = (
        "enriching"
        if not enriched
        else "partial"
        if timed_out or skipped or not documents
        else "complete"
    )
    return {
        "value": {
            "work_item_id": item_id,
            "decision_id": selection.decision_id,
            "state": state,
            "query": query,
            "search": {
                "server": SEARCH_SERVER,
                "searched_at": searched_at,
                "results": len(candidates),
            },
            "documents": [_document_entry(doc) for doc in documents],
            "skipped": skipped,
            "fallbacks": sum(1 for doc in documents if doc.fallback_from),
            "ingest": receipt,
            "embedding": "engine",
        }
    }


#: Operations whose replay safety the engine's SourceIngest authority owns:
#: every page and chunk record has a deterministic id (from its canonical URL)
#: under its connector and stream, so a replayed call upserts the same records.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "research.web": "medulla:SourceIngest",
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
            content_origin=ContentOrigin.OPEN_WEB,
        ),
    )


specs = operations

__all__ = [
    "EgEnrich",
    "EngineEnrich",
    "FALLBACK_REASONS",
    "FetchFailed",
    "FleetCaller",
    "IngestedDocument",
    "NATIVE_IDEMPOTENCY",
    "ResearchWebParams",
    "ResearchWebResult",
    "SearchHit",
    "Selection",
    "SourceIngestWriter",
    "WebPage",
    "canonical_url",
    "document_id",
    "handle_research_web",
    "operations",
    "search_hits",
    "specs",
]
