"""``research.web`` against fake fleet, ingest and embedding ports (ARBORA-OPS-R067)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from arbora.api.ops import research
from arbora.api.registry import Effect

ARTICLE = (
    "A pod disruption budget limits how many pods of a replicated application "
    "can be down at once during voluntary disruptions such as a node drain.\n"
) * 30


class FakeFleet:
    """Answers the three connectors; records every call."""

    def __init__(
        self,
        *,
        webarchiver_fails: frozenset[str] | set[str] = frozenset(),
        reason: str = "upstream_failed",
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.webarchiver_fails = webarchiver_fails
        self.reason = reason

    async def effect(self, op: Any, params: dict[str, Any], caller: Any) -> Any:
        effect = (
            Effect.WRITE
            if params["server"] == research.ARCHIVEBOX_SERVER
            else Effect.READ
        )
        return SimpleNamespace(
            effect=effect, required_scopes=frozenset({"mcp:delegate"})
        )

    async def call(
        self, caller: Any, server: str, tool: str, arguments: dict[str, Any], **kw: Any
    ) -> Any:
        self.calls.append((server, tool, dict(arguments)))
        assert kw["expected_effect"] in (Effect.READ, Effect.WRITE)
        if server == research.SEARCH_SERVER:
            return {
                "results": [
                    {
                        "url": "https://a.example/pdb?utm_source=x",
                        "title": "A",
                        "engine": "duckduckgo",
                        "content": "s",
                    },
                    {
                        "url": "https://A.example/pdb/",
                        "title": "A dup",
                        "engine": "bing",
                    },
                    {"url": "https://b.example/pdb", "title": "B", "engine": "bing"},
                    {"url": "ftp://c.example/pdb", "title": "C", "engine": "bing"},
                    {"url": "https://d.example/pdb", "title": "D", "engine": "brave"},
                ]
            }
        if server in (research.WEBARCHIVER_SERVER, research.ARCHIVEBOX_SERVER):
            assert tool == research.WEB_EXTRACT
            url = arguments["request"]["url"]
            backend = (
                "webarchiver" if server == research.WEBARCHIVER_SERVER else "archivebox"
            )
            if backend == "webarchiver" and url in self.webarchiver_fails:
                return {"kind": "refusal", "reason": self.reason, "detail": url}
            return _contract_page(url, backend)
        raise AssertionError((server, tool))


class FakeIngest:
    def __init__(self) -> None:
        self.committed: list[research.IngestedDocument] = []

    async def commit(self, documents: Any) -> dict[str, Any]:
        self.committed.extend(documents)
        return {
            "affected": sum(1 + len(d.chunks) for d in documents),
            "relationships": 0,
        }


class FakeEmbedder:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def embed(self, chunks: Any) -> int:
        self.chunks.extend(c.chunk_id for c in chunks)
        return len(chunks)


def research_context(
    fleet: FakeFleet,
    *,
    scopes: frozenset[str] = frozenset({"mcp:delegate", "ingest:write"}),
):
    ingest, embedder = FakeIngest(), FakeEmbedder()
    context = SimpleNamespace(
        caller=SimpleNamespace(
            tenant="tenant-a", principal="caller-a", effective_scopes=scopes
        ),
        services={"fleet_gateway": fleet, "web_ingest": ingest, "embedder": embedder},
        client=None,
        owner="caller-a",
        service_identity=False,
        owner_ref="principal:sha256:x",
        registry_digest="d",
        idempotency_key=None,
    )
    return context, ingest, embedder


def research_params(**overrides: Any) -> dict[str, Any]:
    return research.ResearchWebParams(
        query="kubernetes pod disruption budget", **overrides
    ).model_dump()


def research_op() -> Any:
    return research.operations()[0]


def _contract_page(url: str, backend: str) -> dict[str, Any]:
    """A web-source contract ``WebPageResult`` as either connector returns it."""
    import hashlib

    text = ARTICLE + url
    return {
        "kind": "page",
        "url": url,
        "canonical_url": url,
        "final_url": url,
        "status": 200,
        "content_type": "text/html",
        "byte_count": len(text),
        "content_digest": hashlib.sha256(text.encode()).hexdigest(),
        "retrieved_at": "2026-10-18T00:00:00+00:00",
        "title": f"Title of {url}",
        "author": "Ada",
        "published_at": "",
        "outbound_links": [],
        "text_markdown": text,
        "backend": backend,
    }
