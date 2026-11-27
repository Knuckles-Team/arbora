"""``research.web`` served over HTTP and MCP through ``invoke`` (ARBORA-OPS-R067)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import research
from tests.api import _research_fakes as fakes
from tests.api._research_fakes import FakeFleet, RecordingEngine, research_context
from tests.api._served_pair import serve_both

SCOPES = frozenset({"ingest:write", "mcp:delegate"})
QUERY = {"query": "kubernetes pod disruption budget", "max_pages": 2}
FIRST = "https://a.example/pdb?utm_source=x"


async def _serve(
    monkeypatch: pytest.MonkeyPatch,
    fleet: FakeFleet,
    params: dict[str, Any],
    *,
    engine: RecordingEngine | None = None,
    deployed: bool = True,
) -> tuple[Any, Any, Any]:
    base, ingest, _ = research_context(
        fleet, monkeypatch, engine=engine, deployed=deployed
    )
    op = research.operations()[0]

    async def handler(operation: Any, arguments: Any, execution: Any) -> Any:
        caller = execution.caller
        context = SimpleNamespace(**{**vars(base), "caller": caller})
        context.caller = SimpleNamespace(
            tenant="tenant-a",
            principal=caller.principal,
            effective_scopes=caller.effective_scopes,
            engine_claims=getattr(caller, "engine_claims", {"principal": "p"}),
        )
        return await research.handle_research_web(context, arguments, operation)

    response, mcp, _caller = await serve_both(op, handler, params, scopes=SCOPES)
    return response, mcp, ingest


def _docs(payload: Any) -> list[dict[str, Any]]:
    return payload["result"]["value"]["documents"]


@pytest.mark.spec("ARBORA-OPS-R067")
@pytest.mark.spec("ARBORA-OPS-R067.7")
async def test_research_web_default_archivebox_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: dict[str, list[dict[str, Any]]] = {}
    cases = {
        "default": ({}, FakeFleet()),
        "archivebox": ({"backend": "archivebox"}, FakeFleet()),
        "fallback": (
            {},
            FakeFleet(webarchiver_fails={FIRST}, reason="upstream_failed"),
        ),
    }
    for name, (extra, fleet) in cases.items():
        response, mcp, _ = await _serve(monkeypatch, fleet, {**QUERY, **extra})
        assert response.status_code == 200, response.text
        assert _docs(response.json()) == _docs(mcp)
        runs[name] = _docs(response.json())
        for doc in runs[name]:
            assert doc["document_id"].startswith("web:") and doc["citations"]
            assert all(c["url"] and c["backend"] for c in doc["citations"])
    assert {d["backend"] for d in runs["archivebox"]} == {"archivebox"}
    assert runs["fallback"][0]["backend"] == "archivebox"
    assert runs["fallback"][0]["fallback_from"] == "upstream_failed"

    def stored(docs: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
        return [(d["document_id"], d["url"], d["chunks"]) for d in docs]

    assert (
        stored(runs["default"])
        == stored(runs["archivebox"])
        == stored(runs["fallback"])
    )


@pytest.mark.spec("ARBORA-OPS-R067.4")
async def test_served_invoke_reaches_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = FakeFleet()
    response, mcp, _ = await _serve(monkeypatch, fleet, QUERY)
    assert response.status_code == 200 and mcp["ok"] is True
    value = response.json()["result"]["value"]
    assert value["work_item_id"] == "wi-1" and value["ingest"]["affected"] == 2
    # Search, work item, selection, the fetch (after the screen) and enrichment, in order.
    assert fakes.LOG.index("fleet:searxng-mcp:web_search") < fakes.LOG.index(
        "work_item:submit"
    )
    assert fakes.LOG.index("engine:SelectWebPages") < fakes.LOG.index(
        "fleet:webarchiver-mcp:web_extract"
    )
    assert fakes.LOG.index("engine:EnrichDocument") < fakes.LOG.index(
        "work_item:commit:succeeded"
    )


@pytest.mark.spec("ARBORA-OPS-R067.10")
async def test_web_conformance_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = FakeFleet()
    permitted, _, _ = await _serve(monkeypatch, fleet, {**QUERY, "max_pages": 1})
    assert permitted.status_code == 200
    assert [t for _s, t, _a in fleet.calls if t == research.WEB_EXTRACT]
    bad = {
        "conformance_evidence_missing": EngineRefusalFor("CONFORMANCE_EVIDENCE_ABSENT"),
        "conformance_evidence_expired": fakes.evidence_record(expires_at=fakes.NOW_MS),
        "conformance_evidence_revoked": fakes.evidence_record(
            state="revoked", revoked_at=5
        ),
        "conformance_evidence_mismatch": fakes.evidence_record(revision=8),
    }
    changed = {
        "pack": fakes.evidence_record(pack_digest="c" * 64),
        "tool": fakes.evidence_record(
            tool_fingerprints={"tool://webarchiver/web_extract": "d" * 64}
        ),
        "tenant": fakes.evidence_record(tenant_id="tenant-b"),
        "connector": fakes.evidence_record(connector="other-mcp"),
    }
    for reason, answer in bad.items():
        await _refused(monkeypatch, answer, reason)
    for answer in changed.values():
        await _refused(monkeypatch, answer, "conformance_evidence_mismatch")
    await _refused(
        monkeypatch,
        fakes.evidence_record(),
        "conformance_evidence_missing",
        deployed=False,
    )


def EngineRefusalFor(code: str) -> Exception:
    from arbora.api.errors import EngineRefusal

    return EngineRefusal(code)


async def _refused(
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
    reason: str,
    *,
    deployed: bool = True,
) -> None:
    fleet = FakeFleet()
    engine = RecordingEngine(ConnectorPack=lambda _p: _raise_or(answer))
    response, mcp, _ = await _serve(
        monkeypatch, fleet, {**QUERY, "max_pages": 1}, engine=engine, deployed=deployed
    )
    assert reason in response.text and reason in str(mcp)
    assert not [c for c in fleet.calls if c[1] == research.WEB_EXTRACT]


def _raise_or(answer: Any) -> Any:
    if isinstance(answer, Exception):
        raise answer
    return answer
