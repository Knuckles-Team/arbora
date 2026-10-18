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
            Effect.WRITE if params["tool"] == research.ARCHIVEBOX_ADD else Effect.READ
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
        if server == research.WEBARCHIVER_SERVER:
            if arguments["url"] in self.webarchiver_fails:
                return {
                    "isError": True,
                    "content": f"{self.reason}: {arguments['url']}",
                }
            return {
                "backend": "webarchiver",
                "fetch_mode": "http",
                "fetched_at": "2026-10-18T00:00:00+00:00",
                "page": {
                    "title": f"Title of {arguments['url']}",
                    "canonical_url": arguments["url"],
                    "text": ARTICLE + arguments["url"],
                    "author": "Ada",
                    "content_sha256": "",
                },
            }
        if tool == research.ARCHIVEBOX_ADD:
            return {"status": "ok"}
        if tool == research.ARCHIVEBOX_TEXT:
            return {
                "url": arguments["url"],
                "title": f"Title of {arguments['url']}",
                "text": ARTICLE + arguments["url"],
                "archived_at": "2026-10-18T00:00:01+00:00",
                "extractor": "readability",
            }
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
