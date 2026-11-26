"""Harness session browse, search and cost reads (ARBORA-OPS-R060.3)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import sessions
from tests.api._knowledge_support import caller_context
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
        None,
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
        caller_context(), {"text": "s1", "page_size": 5, "cursor": "c"}, None
    )
    assert sent[1]["params"]["text"] == {"Str": "s1"}
    assert sent[1]["params"]["cursor"] == {"Str": "c"}
    # A later page has no summary row and claims no partial outcome.
    _record(monkeypatch, _answer(ENTRY))
    later = await sessions.handle_sessions_browse(
        caller_context(), {"page_size": 25}, None
    )
    assert later["summary"] is None and later["partial"] is False


@pytest.mark.spec("ARBORA-OPS-R060.3")
async def test_costs_keep_unknown_unknown_and_report_measured_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, _answer(SUMMARY))
    unknown = await sessions.handle_sessions_costs(caller_context(), {}, None)
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
    cost = await sessions.handle_sessions_costs(caller_context(), {}, None)
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
