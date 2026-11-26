"""Harness session browse, search and cost reads (ARBORA-OPS-R060.3)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal, to_envelope
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import sessions
from arbora.ingest.service import IngestJobRecord, IngestSourceInventory
from tests.api._knowledge_support import NO_OP, caller_context
from tests.test_served_operation_api import _call, served  # noqa: F401

SUMMARY = {
    "kind": "summary",
    "index_revision": "4",
    "declared": "3",
    "received": "2",
    "indexed": "1",
    "unavailable": "1",
    "pending": "1",
    "watermark": "cursor-9",
    "cost_status": "unknown",
    "cost_reason": "price_missing",
}
ENTRY = {
    "kind": "entry",
    "harness_id": "claude-code",
    "source_session_id": "s1",
    "status": "indexed",
    "reason": "",
    "run_trace_id": "trace-1",
    "end_ms": "900",
    "source_sequence": "7",
}


def _answer(*rows: dict[str, str], cursor: str | None = None) -> dict[str, Any]:
    warnings = [f"next_cursor: {cursor}"] if cursor else []
    return {
        "Rows": {
            "columns": [],
            "warnings": warnings,
            "rows": [
                {"id": r.get("kind", "x"), "channels": [], "provenance": r}
                for r in rows
            ],
        }
    }


def _record(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def engine_send(context: Any, method: str, params: dict[str, Any]) -> Any:
        assert method == "Uql"
        sent.append(params)
        return answer

    monkeypatch.setattr(sessions, "engine_send", engine_send)
    return sent


@pytest.mark.spec("ARBORA-OPS-R060.3")
async def test_denied_sources_are_partial_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(
        monkeypatch, _answer(SUMMARY, ENTRY, cursor="sessioncursor:v1:4:f:7:s1")
    )
    page = await sessions.handle_sessions_browse(
        caller_context(),
        {"harness_id": "claude-code", "page_size": 25, "from_ms": None},
        NO_OP,
    )
    assert sent[0] == {
        "text": "harness_session_search",
        "params": {"harness_id": {"Str": "claude-code"}, "page_size": {"Num": 25.0}},
    }
    assert page["partial"] is True
    assert page["summary"]["pending"] == 1 and page["summary"]["unavailable"] == 1
    assert page["sessions"] == [
        {
            "harness_id": "claude-code",
            "source_session_id": "s1",
            "status": "indexed",
            "reason": None,
            "run_trace_id": "trace-1",
            "end_ms": 900,
            "source_sequence": 7,
        }
    ]
    assert page["next_cursor"] == "sessioncursor:v1:4:f:7:s1"
    await sessions.handle_sessions_search(
        caller_context(), {"text": "s1", "page_size": 5, "cursor": "c"}, NO_OP
    )
    assert sent[1]["params"]["text"] == {"Str": "s1"}
    assert sent[1]["params"]["cursor"] == {"Str": "c"}
    # A later page has no summary row and claims no partial outcome.
    _record(monkeypatch, _answer(ENTRY))
    later = await sessions.handle_sessions_browse(
        caller_context(), {"page_size": 25}, NO_OP
    )
    assert later["summary"] is None and later["partial"] is False


@pytest.mark.spec("ARBORA-OPS-R060.3")
async def test_costs_keep_unknown_unknown_and_report_measured_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, _answer(SUMMARY))
    unknown = await sessions.handle_sessions_costs(caller_context(), {}, NO_OP)
    assert unknown["status"] == "unknown"
    assert unknown["reason"] == "price_missing"
    assert unknown["amount_minor"] is None
    measured = {
        **SUMMARY,
        "cost_status": "measured",
        "cost_minor": "1250",
        "currency": "USD",
        "accounting_revision": "rev1",
        "uncached_input": "100",
        "cached_input": "50",
        "output": "20",
        "reasoning_output": "5",
    }
    _record(monkeypatch, _answer(measured))
    cost = await sessions.handle_sessions_costs(caller_context(), {}, NO_OP)
    assert (cost["status"], cost["amount_minor"], cost["currency"]) == (
        "measured",
        1250,
        "USD",
    )
    assert cost["cached_input"] == 50 and cost["coverage"]["received"] == 2


@pytest.mark.spec("ARBORA-OPS-R060.3")
def test_session_operations_are_served_unavailable_until_the_scope_is_pinned(
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    reply = _call(app, "sessions.browse", {})
    assert reply.status_code == 503, reply.text
    assert reply.json()["error"]["code"] == "UNAVAILABLE"


class _Runner:
    """A composed connector sync with two harness streams and one other source."""

    def __init__(self, jobs: dict[str, str]) -> None:
        self.jobs = jobs

    async def list_sources(self, *, tenant: str, caller: Any = None) -> Any:
        def record(source_id: str, **extra: Any) -> dict[str, Any]:
            return {
                "source_id": source_id,
                "tenant": tenant,
                "state": "active",
                **extra,
            }

        return IngestSourceInventory.model_validate(
            {
                "sources": [
                    record("fleet:x/other", sync_state="idle"),
                    record(
                        "harness-mcp/harness-sessions/codex",
                        sync_state="idle",
                        records=3,
                        last_job_id="job-1",
                    ),
                    record(
                        "harness-mcp/harness-sessions/claude-code", sync_state="idle"
                    ),
                ]
            }
        )

    async def get_job_status(self, *, tenant: str, job_id: str) -> Any:
        if job_id not in self.jobs:
            raise OperationRefused("NOT_FOUND")
        return IngestJobRecord(
            job_id=job_id,
            tenant=tenant,
            status=self.jobs[job_id],  # type: ignore[arg-type]
        )


def _with_runner(runner: Any | None) -> Any:
    context = caller_context()
    context.services = {} if runner is None else {"ingest_runner": runner}
    return context


@pytest.mark.spec("ARBORA-OPS-R060.2")
async def test_manifest_and_discovery_status_name_every_declared_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _with_runner(_Runner({"job-1": "running"}))
    manifest = await sessions.handle_harness_manifest(context, {}, NO_OP)
    assert [h["harness_id"] for h in manifest["harnesses"]] == ["claude-code", "codex"]
    assert manifest["harnesses"][1]["server"] == "harness-mcp"
    assert manifest["harnesses"][1]["records"] == 3
    sent = _record(monkeypatch, _answer(SUMMARY))
    status = await sessions.handle_discovery_status(context, {}, NO_OP)
    by_id = {d["harness_id"]: d for d in status["discovery"]}
    assert by_id["codex"]["state"] == "running"
    assert by_id["claude-code"]["state"] == "not_started"
    assert by_id["codex"]["indexed_session_revision"] == 4
    assert sent[0]["params"]["harness_id"] == {"Str": "claude-code"}
    one = await sessions.handle_discovery_status(
        context, {"harness_id": "codex"}, NO_OP
    )
    assert [d["harness_id"] for d in one["discovery"]] == ["codex"]
    # A job the engine no longer knows reads as not started, not as a failure.
    gone = _with_runner(_Runner({}))
    again = await sessions.handle_discovery_status(gone, {"harness_id": "codex"}, NO_OP)
    assert again["discovery"][0]["state"] == "not_started"


@pytest.mark.spec("ARBORA-OPS-R060.2")
async def test_harness_reads_refuse_without_the_connector_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, _answer(SUMMARY))
    for handler in (sessions.handle_harness_manifest, sessions.handle_discovery_status):
        with pytest.raises(OperationRefused) as refused:
            await handler(_with_runner(None), {}, NO_OP)
        assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R060.4")
async def test_refusals_expose_no_cross_tenant_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stale(context: Any, method: str, params: dict[str, Any]) -> Any:
        raise EngineRefusal("SESSION_CURSOR_STALE")

    monkeypatch.setattr(sessions, "engine_send", stale)
    with pytest.raises(OperationRefused) as refused:
        await sessions.handle_sessions_browse(
            caller_context(), {"cursor": "old", "page_size": 5}, NO_OP
        )
    assert refused.value.code == "STALE_CURSOR"
    status, envelope = to_envelope(
        refused.value, op="sessions.browse", request_id="r", registry_digest="d"
    )
    assert status == 409 and envelope["error"]["code"] == "STALE_CURSOR"
    assert envelope["error"]["details"] == {}
    # An unknown harness names the field and lists no other harness.
    _record(monkeypatch, _answer(SUMMARY))
    with pytest.raises(OperationRefused) as unknown:
        await sessions.handle_discovery_status(
            _with_runner(_Runner({})), {"harness_id": "nope"}, NO_OP
        )
    assert unknown.value.code == "INVALID_ARGUMENT"
    assert unknown.value.details == {"field": "harness_id"}

    # Another engine refusal keeps its own code.
    async def other(context: Any, method: str, params: dict[str, Any]) -> Any:
        raise EngineRefusal("ACCESS_DENIED")

    monkeypatch.setattr(sessions, "engine_send", other)
    with pytest.raises(EngineRefusal):
        await sessions.handle_sessions_browse(caller_context(), {}, NO_OP)


@pytest.mark.spec("ARBORA-OPS-R060.1")
def test_models_are_closed_and_bound_page_size() -> None:
    from pydantic import ValidationError

    for size in (0, 101):
        with pytest.raises(ValidationError):
            sessions.SessionsBrowseParams(page_size=size)
    assert sessions.SessionsBrowseParams().page_size == 25
    assert sessions.SessionsBrowseParams(page_size=100).page_size == 100
    with pytest.raises(ValidationError):
        sessions.SessionsBrowseParams(unknown=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        sessions.DiscoveryProgress(harness_id="h", state="lost")  # type: ignore[arg-type]
    manifest = sessions.HarnessManifest(
        harnesses=[sessions.HarnessEntry(harness_id="h", server="s", sync_state="idle")]
    )
    assert sessions.HarnessManifest.model_validate(manifest.model_dump()) == manifest
