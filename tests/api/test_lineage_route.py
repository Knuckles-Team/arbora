"""The OpenLineage run-event route reaches the engine under the caller (ARBORA-OPS-R889)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from arbora.api.http.lineage import MAX_LINEAGE_EVENTS, LineageWireError, lineage_events
from tests.api import test_sparql_protocol as served

INGEST = ("ingest:write",)
EVENT = {
    "eventType": "COMPLETE",
    "eventTime": "2026-11-26T10:00:00.000Z",
    "run": {"runId": "run-1"},
    "job": {"namespace": "etl", "name": "load_orders"},
    "inputs": [{"namespace": "pg", "name": "public.raw_orders"}],
    "outputs": [{"namespace": "pg", "name": "public.orders"}],
}


class _LineageEngine(served._RecordingEngine):
    async def _send(
        self, method: str, params: Any, graph: str | None, **kwargs: Any
    ) -> Any:
        if method == "LineageIngest":
            self.sent.append((method, params, graph))
            return {"ingested": len(params["events"])}
        return await super()._send(method, params, graph, **kwargs)


def _app(monkeypatch: pytest.MonkeyPatch, roles: tuple[str, ...]) -> Any:
    monkeypatch.setattr(served, "_RecordingEngine", _LineageEngine)
    return served._app(monkeypatch, roles)


@pytest.mark.spec("ARBORA-OPS-R889")
def test_run_event_reaches_engine_under_caller_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = _app(monkeypatch, INGEST)

    one = served._request(app, "POST", "/api/v1/lineage", content=json.dumps(EVENT))
    many = served._request(
        app, "POST", "/api/v1/lineage", content=json.dumps([EVENT, EVENT])
    )

    assert one.status_code == 200, one.text
    assert many.status_code == 200, many.text
    sent = [(method, params) for method, params, _graph in client.sent]
    assert [method for method, _ in sent] == ["LineageIngest", "LineageIngest"]
    event = sent[0][1]["events"][0]
    assert event["run"]["run_id"] == "run-1"
    assert event["run"]["job"] == "etl/load_orders"
    assert event["run"]["ended_at_ms"] == event["run"]["started_at_ms"]
    assert event["inputs"] == [{"namespace": "pg", "name": "public.raw_orders"}]
    assert len(sent[1][1]["events"]) == 2


@pytest.mark.spec("ARBORA-OPS-R889")
def test_route_refuses_bad_bodies_oversized_batches_and_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = _app(monkeypatch, INGEST)
    for body in ('"text"', "42", "[]", "not json", json.dumps([{"run": 1}])):
        refused = served._request(app, "POST", "/api/v1/lineage", content=body)
        assert refused.status_code == 400, body
    too_many = json.dumps([EVENT] * (MAX_LINEAGE_EVENTS + 1))
    assert (
        served._request(app, "POST", "/api/v1/lineage", content=too_many).status_code
        == 413
    )
    assert client.sent == []

    app, client = _app(monkeypatch, ())
    forbidden = served._request(
        app, "POST", "/api/v1/lineage", content=json.dumps(EVENT)
    )
    assert forbidden.status_code == 403
    assert client.sent == []


@pytest.mark.spec("ARBORA-OPS-R889")
def test_wire_mapping_is_total_and_stable() -> None:
    first = lineage_events(EVENT)[0]
    assert first == lineage_events([EVENT])[0]
    assert first["event_id"].startswith("openlineage:")
    with pytest.raises(LineageWireError):
        lineage_events({"run": {"runId": "r"}, "job": {"name": "j"}})
