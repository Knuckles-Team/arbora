"""Backend choice and fallback for ``research.web`` (ARBORA-OPS-R067.3)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import research
from tests.api._research_fakes import (
    FakeFleet,
    research_context,
    research_op,
    research_params,
)

FIRST = "https://a.example/pdb?utm_source=x"


async def _run(fleet: FakeFleet, **params: Any) -> tuple[dict[str, Any], Any]:
    context, ingest, _ = research_context(fleet)
    result = await research.handle_research_web(
        context, research_params(**params), research_op()
    )
    return result["value"], ingest


def _without_backend(value: dict[str, Any]) -> list[dict[str, Any]]:
    """Documents with every backend-identifying field removed."""
    stripped = []
    for doc in value["documents"]:
        doc = {k: v for k, v in doc.items() if k not in {"backend", "fallback_from"}}
        doc["citations"] = [
            {k: v for k, v in c.items() if k not in {"backend", "fetched_at"}}
            for c in doc["citations"]
        ]
        stripped.append(doc)
    return stripped


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_explicit_archivebox_run() -> None:
    primary, _ = await _run(FakeFleet(), max_pages=2)
    archived, ingest = await _run(FakeFleet(), max_pages=2, backend="archivebox")
    assert {d["backend"] for d in archived["documents"]} == {"archivebox"}
    assert all(
        c["backend"] == "archivebox"
        for d in archived["documents"]
        for c in d["citations"]
    )
    assert _without_backend(archived) == _without_backend(primary)
    assert {doc.page.backend for doc in ingest.committed} == {"archivebox"}
    assert research._document_properties(ingest.committed[0])["backend"] == "archivebox"


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_forced_fallback_run() -> None:
    primary, _ = await _run(FakeFleet(), max_pages=2)
    fleet = FakeFleet(webarchiver_fails={FIRST}, reason="upstream_failed")
    fallback, _ = await _run(fleet, max_pages=2)
    first, second = fallback["documents"]
    assert first["backend"] == "archivebox"
    assert first["fallback_from"] == "upstream_failed"
    assert second["backend"] == "webarchiver"
    assert fallback["fallbacks"] == 1
    assert _without_backend(fallback) == _without_backend(primary)
    retried = [
        a["request"]["url"]
        for s, _t, a in fleet.calls
        if s == research.ARCHIVEBOX_SERVER
    ]
    assert retried == [FIRST]


@pytest.mark.spec("ARBORA-OPS-R067.3")
@pytest.mark.parametrize(
    "reason",
    [
        "robots_disallowed",
        "policy_refused",
        "egress_denied",
        "unsupported_type",
        "size_exceeded",
        "rate_limited",
        "extract_empty",
    ],
)
async def test_policy_refusal_is_not_retried(reason: str) -> None:
    fleet = FakeFleet(webarchiver_fails={FIRST}, reason=reason)
    value, _ = await _run(fleet, max_pages=1)
    assert value["skipped"] == [
        {"url": FIRST, "backend": "webarchiver", "reason": reason}
    ]
    assert value["documents"][0]["url"] == "https://b.example/pdb"
    assert not any(s == research.ARCHIVEBOX_SERVER for s, _t, _a in fleet.calls)


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_unreachable_webarchiver_falls_back() -> None:
    class Unreachable(FakeFleet):
        async def call(
            self,
            caller: Any,
            server: str,
            tool: str,
            arguments: dict[str, Any],
            **kw: Any,
        ) -> Any:
            if server == research.WEBARCHIVER_SERVER:
                self.calls.append((server, tool, dict(arguments)))
                raise ConnectionError("child down")
            return await super().call(caller, server, tool, arguments, **kw)

    value, _ = await _run(Unreachable(), max_pages=1)
    assert value["documents"][0]["backend"] == "archivebox"
    assert value["documents"][0]["fallback_from"] == "unreachable"


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_caller_urls_skip_the_search() -> None:
    fleet = FakeFleet()
    context, _, _ = research_context(fleet)
    params = research.ResearchWebParams(urls=("https://b.example/pdb",)).model_dump()
    value = (await research.handle_research_web(context, params, research_op()))[
        "value"
    ]
    assert [d["url"] for d in value["documents"]] == ["https://b.example/pdb"]
    assert not any(s == research.SEARCH_SERVER for s, _t, _a in fleet.calls)
    with pytest.raises(ValueError):
        research.ResearchWebParams()
    with pytest.raises(ValueError):
        research.ResearchWebParams(query="q", max_pages=33)
