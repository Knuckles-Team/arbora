"""``finance.receipts.list`` reads engine receipts over HTTP and MCP (ARBORA-DATA-MARKET-R009.4)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import (
    McpAsk,
    capture_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

VIEWS: list[dict[str, Any]] = [
    {
        "receipt_id": "r1",
        "receipt_kind": "alert_delivery",
        "receipt_state": "completed",
        "tenant_key": "tenant:one",
        "idempotency_key": "e1:s1",
        "event_id": "e1",
        "mode": "live",
        "channel": "chat",
        "attempt": 2,
        "recorded_at": "2026-11-01T06:00:00Z",
    },
    {
        "receipt_id": "r2",
        "receipt_kind": "dca_proposal",
        "receipt_state": "failed",
        "tenant_key": "tenant:one",
        "idempotency_key": "p:1:d",
        "event_id": "p:1:d",
        "mode": "paper",
        "channel": "none",
        "attempt": 1,
        "recorded_at": "2026-11-02T06:00:00Z",
    },
    {
        "receipt_id": "r3",
        "receipt_kind": "alert_delivery",
        "receipt_state": "due",
        "tenant_key": "tenant:two",
        "idempotency_key": "x",
        "event_id": "x",
        "mode": "live",
        "channel": "chat",
        "attempt": 1,
        "recorded_at": "2026-11-03T06:00:00Z",
    },
]


class Engine:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, params: dict[str, Any], _principal: str) -> Any:
        self.requests.append(params)
        rows = [
            v
            for v in VIEWS
            if v["tenant_key"] == params["tenant_key"]
            and params.get("state") in (None, v["receipt_state"])
            and params.get("kind") in (None, v["receipt_kind"])
        ]
        return {"receipts": rows[: params["limit"]]}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


@pytest.fixture
def app(captured: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    invocation = captured
    app, client = served
    engine = Engine()
    route_methods(
        client,
        {"FinanceReceiptList": engine},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("finance:read",))},
    )
    return app, engine, invocation, monkeypatch


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.4")
def test_states_filter_by_tenant_and_paper_stays_paper(app: Any) -> None:
    http, engine, _invocation, _mp = app
    everything = _value(_call(http, "finance.receipts.list", {}))
    assert [i["receipt_id"] for i in everything["items"]] == ["r1", "r2"]
    assert everything["items"][0] == {
        "receipt_id": "r1",
        "event_id": "e1",
        "kind": "alert_delivery",
        "state": "completed",
        "mode": "live",
        "occurred_at": "2026-11-01T06:00:00Z",
        "channel": "chat",
        "attempt": 2,
    }
    assert everything["items"][1]["mode"] == "paper"
    assert engine.requests[0] == {"tenant_key": "tenant:one", "limit": 50}
    failed = _value(_call(http, "finance.receipts.list", {"state": "failed"}))
    assert [i["receipt_id"] for i in failed["items"]] == ["r2"]
    dca = _value(_call(http, "finance.receipts.list", {"kind": "dca_proposal"}))
    assert [i["kind"] for i in dca["items"]] == ["dca_proposal"]


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.4")
def test_bad_arguments_are_refused_and_the_mcp_twin_reads(app: Any) -> None:
    http, _engine, invocation, monkeypatch = app
    for bad in ({"limit": 201}, {"limit": 0}, {"state": "replayed"}, {"kind": "x"}):
        assert _call(http, "finance.receipts.list", bad).status_code == 400
    ask = McpAsk(
        invocation["invocation"],
        monkeypatch,
        person("one", extra_scopes=("finance:read",)),
    )
    answer = ask("ask", "finance.receipts.list", {"limit": 1})
    assert answer["ok"] is True, answer
    assert [i["receipt_id"] for i in answer["result"]["items"]] == ["r1"]


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.4")
def test_engine_without_the_method_refuses_unavailable(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    route_methods(
        client,
        {},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("finance:read",))},
    )
    import medulla.generated as generated
    from medulla.generated import _runtime

    kept = frozenset(generated.METHOD_IDS) - {"FinanceReceiptList"}
    monkeypatch.setattr(generated, "METHOD_IDS", kept)
    monkeypatch.setattr(_runtime, "METHOD_IDS", kept)
    refused = _call(app, "finance.receipts.list", {})
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "UNAVAILABLE"
