"""Decision evidence reads served over HTTP and MCP (ARBORA-OPS-R920)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import FastMCP
from medulla import EngineResponseError

from arbora.mcp_server import runtime as mcp_runtime
from arbora.security import request_identity
from tests.test_served_operation_api import (  # noqa: F401
    _actor,
    _call,
    _value,
    served,
)

TENANT = "tenant:one"
OTHER = "tenant:two"
BASE_MS = 1_700_000_000_000


def _record(n: int, tenant: str) -> dict[str, Any]:
    return {
        "tenant": tenant,
        "record_id": f"decision:{n}",
        "record_kind": "assembly",
        "question_kind": None,
        "resolution_kind": "assembly" if n % 2 else "statistical",
        "outcome": None,
        "assembly_outcome": "solved",
        "decided_at_ms": BASE_MS - n * 1000,
        "record_digest": f"digest-{n}",
        "inputs": "inline" if n < 3 else "retired",
    }


class RecordingEngine:
    """A recording engine for ``DecisionLog`` that isolates tenants as the engine does."""

    def __init__(self) -> None:
        self.records = [_record(n, TENANT) for n in range(1, 6)]
        self.records.append(_record(9, OTHER))
        self.sent: list[dict[str, Any]] = []

    def _mine(self, tenant: str, record_id: str) -> dict[str, Any]:
        for row in self.records:
            if row["record_id"] == record_id and row["tenant"] == tenant:
                return row
        raise EngineResponseError(
            "INVALID_ARGUMENT", "DECISION_NOT_FOUND: no committed decision with this id"
        )

    def decision_log(self, body: dict[str, Any]) -> Any:
        self.sent.append(body)
        tenant = body["tenant_id"]
        action = body["op"]
        if action == "list":
            rows = [r for r in self.records if r["tenant"] == tenant]
            if body.get("resolution_kind"):
                rows = [
                    r for r in rows if r["resolution_kind"] == body["resolution_kind"]
                ]
            if "from_ms" in body:
                rows = [r for r in rows if r["decided_at_ms"] >= body["from_ms"]]
            start = int(body.get("cursor") or 0)
            page = rows[start : start + body["limit"]]
            more = start + body["limit"] < len(rows)
            return {
                "records": [
                    {k: v for k, v in r.items() if k != "tenant"} for r in page
                ],
                "next_cursor": str(start + body["limit"]) if more else None,
            }
        row = self._mine(tenant, body["record_id"])
        if action == "provenance":
            return {
                "record_id": row["record_id"],
                "record_kind": "assembly",
                "record_digest": row["record_digest"],
                "premises": [
                    {"kind": "graph_row", "reference": "node:a", "class": "fact"}
                ],
                "facts": [
                    {"node_id": "node:a", "revision": 7, "digest": "fact-digest"}
                ],
                "rules": [{"rule_id": "rule:r", "version": 2, "digest": "rule-digest"}],
                "approver": None,
                "inputs": row["inputs"],
            }
        if row["inputs"] == "retired":
            raise EngineResponseError(
                "INVALID_ARGUMENT",
                "DECISION_INPUTS_RETIRED: the stored inputs are gone",
            )
        if body["option_id"] == "option:hidden":
            raise EngineResponseError(
                "INVALID_ARGUMENT", "OPTION_NOT_VISIBLE: not among the candidates"
            )
        return {
            "record_id": row["record_id"],
            "option_id": body["option_id"],
            "verdict": "worse",
            "reasons": [{"kind": "cost", "detail": "costs more"}],
            "budget_used_ms": min(body.get("budget_ms", 100), 100),
            "budget_exhausted": body.get("budget_ms") == 1,
        }


@pytest.fixture
def invocation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the served invocation the ``served`` fixture composes."""
    import tests.test_served_operation_api as harness

    captured: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def capturing(**kwargs: Any) -> Any:
        captured["invocation"] = compose(**kwargs)
        return captured["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", capturing)
    return captured


@pytest.fixture
def engine(invocation: dict[str, Any], served: Any) -> Any:  # noqa: F811
    app, client = served
    recording = RecordingEngine()
    original = client._send

    async def send(method: str, params: Any, graph: str | None, **kw: Any) -> Any:
        if method == "DecisionLog":
            return recording.decision_log(params["op"])
        return await original(method, params, graph, **kw)

    client._send = send
    return app, recording, invocation["invocation"]


@pytest.mark.spec("ARBORA-OPS-R920.1")
def test_decision_list_pages_filters_and_isolates_tenants(engine: Any) -> None:
    app, recording, _invocation = engine
    first = _value(_call(app, "decision.list", {"limit": 2}))
    assert [i["decision_id"] for i in first["items"]] == ["decision:1", "decision:2"]
    assert first["items"][0] == {
        "decision_id": "decision:1",
        "question_kind": None,
        "resolution_kind": "assembly",
        "outcome": "solved",
        "decided_at": "2023-11-14T22:13:19.000Z",
        "record_digest": "digest-1",
        "inputs_state": "inline",
    }
    second = _value(
        _call(app, "decision.list", {"limit": 2, "cursor": first["next_cursor"]})
    )
    assert [i["decision_id"] for i in second["items"]] == ["decision:3", "decision:4"]
    filtered = _value(
        _call(app, "decision.list", {"resolution_kind": "statistical", "limit": 50})
    )
    assert [i["decision_id"] for i in filtered["items"]] == ["decision:2", "decision:4"]
    ranged = _value(
        _call(app, "decision.list", {"from": "2023-11-14T22:13:17Z", "limit": 50})
    )
    assert [i["decision_id"] for i in ranged["items"]] == [
        "decision:1",
        "decision:2",
        "decision:3",
    ]
    # Another tenant's record never appears, and the tenant is the verified one.
    everything = _value(_call(app, "decision.list", {"limit": 200}))
    assert "decision:9" not in [i["decision_id"] for i in everything["items"]]
    assert {body["tenant_id"] for body in recording.sent} == {TENANT}


@pytest.mark.spec("ARBORA-OPS-R920.1")
def test_decision_list_refuses_bad_arguments(engine: Any) -> None:
    app, recording, _invocation = engine
    for bad in (
        {"limit": 201},
        {"limit": 0},
        {"from": "2024-01-01T00:00:00Z", "to": "2023-01-01T00:00:00Z"},
        {"from": "not a time"},
        {"from": "2024-01-01T00:00:00"},
        {"tenant_id": OTHER},
    ):
        response = _call(app, "decision.list", bad)
        assert response.status_code == 400, (bad, response.text)
    assert recording.sent == []


@pytest.mark.spec("ARBORA-OPS-R920.2")
def test_provenance_returns_facts_at_used_revision_and_hides_other_tenants(
    engine: Any,
) -> None:
    app, _recording, _invocation = engine
    value = _value(_call(app, "decision.provenance.get", {"decision_id": "decision:1"}))
    assert value == {
        "decision_id": "decision:1",
        "record_digest": "digest-1",
        "premises": [{"kind": "graph_row", "ref": "node:a", "class": "fact"}],
        "facts": [{"node_id": "node:a", "revision": 7, "digest": "fact-digest"}],
        "rules": [{"rule_id": "rule:r", "version": 2, "digest": "rule-digest"}],
        "approver": None,
        "inputs_state": "inline",
    }
    for unknown in ("decision:9", "decision:404"):
        response = _call(app, "decision.provenance.get", {"decision_id": unknown})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "DECISION_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R920.3")
def test_why_not_is_bounded_and_refuses_retired_inputs(engine: Any) -> None:
    app, recording, _invocation = engine
    params = {"decision_id": "decision:1", "option_id": "option:b"}
    value = _value(_call(app, "decision.why_not", {**params, "budget_ms": 1}))
    assert value["verdict"] == "worse"
    assert value["budget_exhausted"] is True
    assert value["reasons"] == [{"kind": "cost", "detail": "costs more"}]
    # A budget over the u32 ceiling is clamped, never refused.
    _value(_call(app, "decision.why_not", {**params, "budget_ms": 2**40}))
    assert recording.sent[-1]["budget_ms"] == 2**32 - 1
    retired = _call(app, "decision.why_not", {**params, "decision_id": "decision:4"})
    assert retired.json()["error"]["code"] == "DECISION_INPUTS_RETIRED"
    hidden = _call(app, "decision.why_not", {**params, "option_id": "option:hidden"})
    assert hidden.json()["error"]["code"] == "OPTION_NOT_VISIBLE"
    unknown = _call(app, "decision.why_not", {**params, "decision_id": "decision:9"})
    assert unknown.json()["error"]["code"] == "DECISION_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R920.3")
def test_why_not_option_id_is_the_engine_component_id_unchanged(engine: Any) -> None:
    from arbora.api.ops.decision_evidence import DecisionWhyNotParams

    app, recording, _invocation = engine
    description = DecisionWhyNotParams.model_fields["option_id"].description or ""
    assert "component id" in description
    for component_id in ("option:b", "skill:triage@2", "tool/search.v1"):
        _call(
            app,
            "decision.why_not",
            {"decision_id": "decision:1", "option_id": component_id},
        )
        assert recording.sent[-1]["option_id"] == component_id


@pytest.mark.spec("ARBORA-OPS-R920", "ARBORA-OPS-R920.4")
def test_decision_evidence_journey_over_mcp_and_http(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _recording, invocation = engine
    process = request_identity.mint_graph_session(_actor())
    monkeypatch.setattr(mcp_runtime, "_PROCESS_SESSION", process)
    mcp = FastMCP("decision-evidence")
    mcp_runtime._register_served_intent_surface(mcp, invocation)

    async def call(op: str, params: dict[str, Any]) -> Any:
        result = await mcp.call_tool("ask", {"op": op, "params": params})
        text = "".join(getattr(part, "text", "") for part in result.content)
        return json.loads(text)

    def ask(op: str, params: dict[str, Any]) -> Any:
        return asyncio.run(call(op, params))

    listed = ask("decision.list", {"limit": 1})
    assert "error" not in listed, listed
    first = listed["result"]["items"][0]["decision_id"]
    provenance = ask("decision.provenance.get", {"decision_id": first})
    assert provenance["result"]["facts"][0]["revision"] == 7
    why = ask("decision.why_not", {"decision_id": first, "option_id": "option:b"})
    assert why["result"]["verdict"] == "worse"
    # The same read over HTTP agrees, and a second tenant's id reads as absent.
    http = _value(_call(app, "decision.provenance.get", {"decision_id": first}))
    assert http == provenance["result"]
    absent = ask("decision.provenance.get", {"decision_id": "decision:9"})
    assert absent["error"]["code"] == "DECISION_NOT_FOUND"
