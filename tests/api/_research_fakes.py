"""``research.web`` against fake fleet, ingest and embedding ports (ARBORA-OPS-R067)."""

from __future__ import annotations

import hashlib
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
        LOG.append(f"fleet:{server}:{tool}")
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
            url = arguments["request"]["url"]
            backend = (
                "webarchiver" if server == research.WEBARCHIVER_SERVER else "archivebox"
            )
            if backend == "webarchiver" and url in self.webarchiver_fails:
                return {"kind": "refusal", "reason": self.reason, "detail": url}
            return {
                "kind": "page",
                "url": url,
                "canonical_url": url,
                "final_url": url,
                "status": 200,
                "content_type": "text/html",
                "byte_count": len(ARTICLE),
                "content_digest": hashlib.sha256(url.encode()).hexdigest(),
                "retrieved_at": "2026-10-18T00:00:00+00:00"
                if backend == "webarchiver"
                else "2026-10-18T00:00:01+00:00",
                "title": f"Title of {url}",
                "author": "Ada",
                "text_markdown": ARTICLE + url,
                "backend": backend,
            }
        raise AssertionError((server, tool))


class FakeIngest:
    def __init__(self) -> None:
        self.committed: list[research.IngestedDocument] = []

    async def commit(self, documents: Any) -> dict[str, Any]:
        self.committed.extend(documents)
        return {
            "affected": len(documents),
            "relationships": 0,
        }


PACK_DIGEST = "a" * 64
FINGERPRINTS = {"tool://webarchiver/web_extract": "b" * 64}
NOW_MS = 1_000
#: The call log shared by the fleet and the engine, in the order the handler made them.
LOG: list[str] = []


def evidence_record(**changes: Any) -> dict[str, Any]:
    """The engine ``ConformanceEvidence`` of a connector deployed at revision 7."""
    record: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "connector": research.WEBARCHIVER_SERVER,
        "revision": 7,
        "pack_digest": PACK_DIGEST,
        "tool_fingerprints": dict(FINGERPRINTS),
        "issued_at": 0,
        "expires_at": 10 * NOW_MS,
        "state": "active",
    }
    return {**record, **changes}


def _canonical(url: str) -> str:
    return research.canonical_url(url)


def _select(params: dict[str, Any]) -> dict[str, Any]:
    """A stand-in engine selection: first candidate per canonical URL, in rank order."""
    request = params["request"]
    seen: set[str] = set()
    chosen: list[dict[str, Any]] = []
    for candidate in sorted(request["candidates"], key=lambda c: c["rank"]):
        canonical = _canonical(candidate["url"])
        if canonical in seen or len(chosen) >= request["max_fetches"]:
            continue
        seen.add(canonical)
        chosen.append({"canonical_url": canonical, "order": len(chosen)})
    return {
        "query_digest": "q" * 8,
        "now_ms": request["now_ms"],
        "selected": chosen,
        "skipped": [],
        "considered": len(request["candidates"]),
        "max_fetches": request["max_fetches"],
    }


class RecordingEngine:
    """The engine double: answers selection, enrichment and the conformance read."""

    authenticated = True

    def __init__(self, **answers: Any) -> None:
        self.answers = {
            "SelectWebPages": _select,
            "EnrichDocument": lambda p: {
                "document_id": p["request"]["document_id"],
                "chunk_ids": [f"{p['request']['document_id']}#c{i}" for i in range(3)],
            },
            "ConnectorPack": lambda p: evidence_record(
                connector=p["op"]["request"]["connector"],
                tenant_id=p["op"]["request"]["tenant_id"],
            ),
            **answers,
        }
        self.sent: list[tuple[str, Any]] = []

    def use_verified_context(self, claims: Any) -> Any:
        import contextlib

        return contextlib.nullcontext()

    async def _send(self, method: str, params: Any, graph: str, **kw: Any) -> Any:
        LOG.append(f"engine:{method}")
        self.sent.append((method, params))
        answer = self.answers[method]
        if isinstance(answer, BaseException):
            raise answer
        return answer(params) if callable(answer) else answer


class FakeWorkEngine:
    """The work item namespace the handler submits, claims and commits through."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.committed: list[str] = []


def _patch_engine(monkeypatch: Any) -> None:
    """Serve the research methods through the recording client."""
    import medulla.generated as generated

    from arbora.api.ops import _engine

    methods = {"SelectWebPages", "EnrichDocument", "ConnectorPack"}
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | methods
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any) -> Any:
        return await client._send(method, params, graph, **kw)

    async def graph(_context: Any) -> str:
        return "tenant-content"

    monkeypatch.setattr(generated, "send_by_id", send)
    monkeypatch.setattr(_engine, "caller_dispatch_graph", graph)


def _patch_work_items(monkeypatch: Any, work: FakeWorkEngine) -> None:
    from arbora import work_items

    def submit(engine: Any, **kw: Any) -> tuple[str, bool]:
        LOG.append("work_item:submit")
        work.submitted.append(kw)
        return "wi-1", True

    def claim(engine: Any, item_id: str, **kw: Any) -> dict[str, Any]:
        return {"claimed": True, "tenant": "tenant-a"}

    def commit(engine: Any, item_id: str, claim: Any, **kw: Any) -> str:
        LOG.append(f"work_item:commit:{kw['outcome']}")
        work.committed.append(kw["outcome"])
        return "committed"

    monkeypatch.setattr(work_items, "submit_work_item_atomic", submit)
    monkeypatch.setattr(work_items, "claim_specific", claim)
    monkeypatch.setattr(work_items, "commit_result", commit)


def _declarations(deployed: bool) -> Any:
    from arbora.fleet.conformance import DeployedPack
    from arbora.fleet.declarations import Declarations

    declarations = Declarations()
    servers = (research.WEBARCHIVER_SERVER, research.ARCHIVEBOX_SERVER)
    for server in servers if deployed else ():
        declarations.remember_deployed(
            server, DeployedPack(7, PACK_DIGEST, dict(FINGERPRINTS))
        )
    return declarations


def build_research_context(
    fleet: FakeFleet,
    monkeypatch: Any,
    *,
    scopes: frozenset[str] = frozenset({"mcp:delegate", "ingest:write"}),
    engine: RecordingEngine | None = None,
    deployed: bool = True,
    services: dict[str, Any] | None = None,
):
    """A served context: fake fleet, recording engine, work items and an ingest port."""
    LOG.clear()
    engine = engine or RecordingEngine()
    work = FakeWorkEngine()
    _patch_engine(monkeypatch)
    _patch_work_items(monkeypatch, work)
    ingest = FakeIngest()
    context = SimpleNamespace(
        caller=SimpleNamespace(
            tenant="tenant-a",
            principal="caller-a",
            effective_scopes=scopes,
            engine_claims={"principal": "caller-a"},
        ),
        services={
            "fleet_gateway": fleet,
            "web_ingest": ingest,
            "task_plan_work_engine": work,
            "fleet_declarations": _declarations(deployed),
            "now_ms": lambda: NOW_MS,
            **(services or {}),
        },
        client=engine,
        owner="caller-a",
        service_identity=False,
        owner_ref="principal:sha256:x",
        registry_digest="d",
        idempotency_key=None,
    )
    return context, ingest, engine


def research_context(
    fleet: FakeFleet,
    monkeypatch: Any,
    *,
    scopes: frozenset[str] = frozenset({"mcp:delegate", "ingest:write"}),
    engine: RecordingEngine | None = None,
    deployed: bool = True,
    services: dict[str, Any] | None = None,
):
    """A served context: fake fleet, recording engine, work items and an ingest port."""
    return build_research_context(
        fleet,
        monkeypatch,
        scopes=scopes,
        engine=engine,
        deployed=deployed,
        services=services,
    )


def research_params(**overrides: Any) -> dict[str, Any]:
    return research.ResearchWebParams(
        query="kubernetes pod disruption budget", **overrides
    ).model_dump()


def research_op() -> Any:
    return research.operations()[0]
