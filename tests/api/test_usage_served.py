"""Session usage and measured timing served over HTTP and MCP (ARBORA-OPS-R024.3.1, .3.2)."""

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError

from tests.api._served_support import (
    McpAsk,
    capture_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

SESSIONS: dict[tuple[str, str], dict[str, Any]] = {
    ("tenant:one", "ctx-1"): {
        "context_id": "ctx-1",
        "counts": {"input_tokens": 1200, "output_tokens": 300},
        "count_coverage": {"input_tokens": 3, "output_tokens": 2},
        "expected_runs": 3,
        "recorded_runs": 2,
        "missing_runs": 1,
        "inventory_complete": False,
        "turn_timings": [
            {"run_id": "r1", "turn_index": 0, "phase": "request", "duration_ms": 900},
            {"run_id": "r1", "turn_index": 0, "phase": "run", "duration_ms": 700},
        ],
    },
    ("tenant:one", "ctx-bad"): {
        "context_id": "ctx-bad",
        "counts": {"input_tokens": 5},
        "count_coverage": {"input_tokens": 1},
        "expected_runs": 1,
        "recorded_runs": 1,
        "missing_runs": 0,
        "inventory_complete": True,
        "turn_timings": [
            {"run_id": "r1", "turn_index": 0, "phase": "run", "duration_ms": 86_400_001}
        ],
    },
    ("tenant:two", "ctx-2"): {"context_id": "ctx-2", "counts": {}},
}


class Engine:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, params: dict[str, Any], _principal: str) -> Any:
        self.requests.append(params)
        found = SESSIONS.get((params["tenant"], params["context_id"]))
        if found is None:
            raise EngineResponseError("ACCESS_DENIED", "no such context")
        return found


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


@pytest.fixture
def app(captured: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    engine = Engine()
    route_methods(
        client,
        {"GetSessionUsage": engine},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("usage:read",))},
    )
    return app, engine, captured, monkeypatch


@pytest.mark.spec("ARBORA-OPS-R024.3.1")
def test_session_usage_forwards_caller_and_keeps_partial_coverage(app: Any) -> None:
    http, engine, _captured, _mp = app
    usage = _value(_call(http, "usage.read", {"context_id": "ctx-1"}))["session"]
    assert engine.requests == [{"context_id": "ctx-1", "tenant": "tenant:one"}]
    assert usage["counts"] == {"input_tokens": 1200, "output_tokens": 300}
    assert usage["count_coverage"] == {"input_tokens": 3, "output_tokens": 2}
    assert usage["inventory_complete"] is False
    assert (usage["expected_runs"], usage["recorded_runs"], usage["missing_runs"]) == (
        3,
        2,
        1,
    )
    foreign = _call(http, "usage.read", {"context_id": "ctx-2"})
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "ACCESS_DENIED"
    for bad in ("", "a b", "x" * 129):
        assert _call(http, "usage.read", {"context_id": bad}).status_code == 400


@pytest.mark.spec("ARBORA-OPS-R024.3.2")
def test_timing_is_validated_and_correlated_to_turn_receipts(app: Any) -> None:
    http, _engine, captured, monkeypatch = app
    good = _value(_call(http, "usage.read", {"context_id": "ctx-1"}))["session"]
    assert [t["phase"] for t in good["turn_timings"]] == ["request", "run"]
    assert good["timing_refusal"] is None
    bad = _value(_call(http, "usage.read", {"context_id": "ctx-bad"}))["session"]
    assert bad["turn_timings"] is None
    assert bad["timing_refusal"] == "TIMING_INVALID"
    assert bad["counts"] == {"input_tokens": 5}
    assert not any("rate" in key or "tokens_per" in key for key in bad)
    ask = McpAsk(
        captured["invocation"], monkeypatch, person("one", extra_scopes=("usage:read",))
    )
    answer = ask("ask", "usage.read", {"context_id": "ctx-1"})
    assert answer["ok"] is True, answer
    assert answer["result"]["session"]["recorded_runs"] == 2


@pytest.mark.spec("ARBORA-OPS-R024.3")
def test_usage_read_reaches_engine_ledger_for_the_caller_tenant(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, client = served
    seen: list[dict[str, Any]] = []
    rollup = {
        "view": "rollup",
        "days": [
            {
                "day": 20,
                "model_key": "m",
                "calls": 3,
                "errors": 1,
                "tokens_in": 10,
                "tokens_out": 4,
                "tokens_cached": 2,
                "cost_micro": None,
            }
        ],
    }

    def usage(params: dict[str, Any], _principal: str) -> Any:
        seen.append(params)
        return rollup

    route_methods(
        client,
        {"Usage": usage},
        monkeypatch,
        {
            "fixture-verified": person("one", extra_scopes=("usage:read",)),
        },
    )
    answer = _value(_call(http, "usage.read", {"from_ms": 5, "to_ms": 99}))
    assert answer == rollup
    assert seen == [{"op": {"op": "read", "view": "rollup", "from_ms": 5, "to_ms": 99}}]
    backwards = _call(http, "usage.read", {"from_ms": 9, "to_ms": 3})
    assert backwards.status_code == 400


def _group(key: str, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "calls": 2,
        "tokens_in": 10,
        "tokens_out": 4,
        "tokens_cached": 1,
        "errors": 0,
        "cost_micro": 1500,
        "currency": "USD",
        "latency_p95_ms": 420,
    }
    row.update(changes)
    return row


@pytest.mark.spec("ARBORA-OPS-R024.3.3")
def test_grouped_usage_forwards_group_by_and_refuses_a_missing_shape(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated.models as models

    http, client = served
    seen: list[dict[str, Any]] = []
    answer: dict[str, Any] = {}

    def usage(params: dict[str, Any], _principal: str) -> Any:
        seen.append(params)
        return answer

    route_methods(
        client,
        {"Usage": usage},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("usage:read",))},
    )
    body = {"from_ms": 5, "to_ms": 99, "group_by": "project"}

    # An installed engine contract without grouped rollups refuses before sending.
    monkeypatch.delattr(models, "UsageGroupRollup", raising=False)
    refused = _call(http, "usage.read", body)
    assert refused.status_code == 503 and seen == []
    assert refused.json()["error"]["code"] == "UNAVAILABLE"

    monkeypatch.setattr(models, "UsageGroupRollup", object, raising=False)
    answer.update(
        view="rollup",
        days=[],
        index_watermark_ms=77,
        groups=[_group("p1"), _group("p2", cost_micro=None, currency=None)],
    )
    value = _value(_call(http, "usage.read", body))
    assert seen == [
        {
            "op": {
                "op": "read",
                "view": "rollup",
                "from_ms": 5,
                "to_ms": 99,
                "group_by": "project",
            }
        }
    ]
    assert value["index_watermark_ms"] == 77
    assert [g["key"] for g in value["groups"]] == ["p1", "p2"]
    assert value["groups"][0]["currency"] == "USD"
    assert value["groups"][1]["cost_micro"] is None
    assert not {"observed_from_ms", "observed_to_ms"} & value.keys()

    for broken in (
        {"groups": [_group("p1", currency=None)]},  # cost without a currency
        {"groups": [_group("p1", currency="usd")]},
        {"groups": [{"key": "p1"}]},
        {"index_watermark_ms": -1},
    ):
        answer.update(
            view="rollup", days=[], index_watermark_ms=77, groups=[_group("p1")]
        )
        answer.update(broken)
        assert _call(http, "usage.read", body).status_code == 503
    del answer["groups"]
    assert _call(http, "usage.read", body).status_code == 503
    assert _call(http, "usage.read", {**body, "group_by": "color"}).status_code == 400
