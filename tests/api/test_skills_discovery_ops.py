"""Skill measures, rank, peers and traces served over HTTP and MCP (ARBORA-OPS-R954)."""

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

MEASURES = {
    "invocations": 1000,
    "distinct_users": 30,
    "retained_users_ppm": 400000,
    "agent_chosen_ppm": 600000,
    "user_chosen_ppm": 400000,
    "error_rate_ppm": 50000,
    "latency_p50_ms": 120,
    "latency_p95_ms": 900,
    "cost_mean_micro_usd": None,
    "cost_p95_micro_usd": None,
    "eval_pass_rate_ppm": 0,
    "eval_runs": 0,
}


class Engine:
    """A recording engine with the ``SkillTelemetry`` read shapes."""

    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []
        self.cohort = 30

    def __call__(self, params: dict[str, Any], _principal: str) -> Any:
        op = params["op"]
        self.ops.append(op)
        kind = op["op"]
        if kind == "measures":
            return MEASURES
        if kind == "rank":
            refs = op["skill_refs"]
            rows = [
                {"skill_ref": r, "quality_ppm": 0, "eval_runs": 0, "invocations": n}
                for r, n in zip(refs, (30, 20, 10), strict=False)
            ]
            return {"skills": rows[: op.get("limit") or len(rows)]}
        if kind == "peers":
            if self.cohort < 5:
                raise EngineResponseError("PEER_COHORT_TOO_SMALL", "cohort is 4")
            return {"skills": [{"skill_ref": "skill:b@1", "cohort_users": 12}]}
        assert kind == "traces_by_eval"
        return {"trace_ids": ["trace-1"], "next_cursor": None}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


@pytest.fixture
def app(captured: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    engine = Engine()
    route_methods(
        client,
        {"SkillTelemetry": engine},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("telemetry:read",))},
    )
    return app, engine, captured, monkeypatch


WINDOW = {"start_ms": 0, "end_ms": 1000}


@pytest.mark.spec("ARBORA-OPS-R954")
def test_measures_rank_find_peers_and_small_cohort_refusal(app: Any) -> None:
    http, engine, captured, monkeypatch = app
    measures = _value(
        _call(http, "skills.measures.read", {"skill_ref": "skill:a@1", **WINDOW})
    )
    assert measures == MEASURES
    assert engine.ops[0]["window"] == WINDOW
    assert engine.ops[0]["graph"]
    ranked = _value(
        _call(
            http,
            "skills.rank",
            {"skill_refs": ["a", "b", "c"], "limit": 2, **WINDOW},
        )
    )
    assert [s["skill_ref"] for s in ranked["skills"]] == ["a", "b"]
    peers = _value(_call(http, "skills.peers.recommend", WINDOW))
    assert peers["skills"] == [{"skill_ref": "skill:b@1", "cohort_users": 12}]
    assert "user" not in engine.ops[2]
    traces = _value(
        _call(
            http,
            "skills.traces.by_eval",
            {"skill_ref": "skill:a@1", "outcome": "fail"},
        )
    )
    assert traces == {"trace_ids": ["trace-1"], "next_cursor": None}
    engine.cohort = 4
    refused = _call(http, "skills.peers.recommend", WINDOW)
    assert refused.json()["error"]["code"] == "PEER_COHORT_TOO_SMALL"
    ask = McpAsk(
        captured["invocation"],
        monkeypatch,
        person("one", extra_scopes=("telemetry:read",)),
    )
    answer = ask("ask", "skills.measures.read", {"skill_ref": "skill:a@1", **WINDOW})
    assert answer["ok"] is True, answer


@pytest.mark.spec("ARBORA-OPS-R954")
def test_bad_window_and_outcome_are_refused_before_the_engine(app: Any) -> None:
    http, engine, _captured, _mp = app
    for body in (
        {"skill_ref": "s", "start_ms": 5, "end_ms": 5},
        {"skill_ref": "", **WINDOW},
        {"skill_ref": "s", "start_ms": -1, "end_ms": 2},
    ):
        assert _call(http, "skills.measures.read", body).status_code == 400
    bad = {"skill_ref": "s", "outcome": "maybe"}
    assert _call(http, "skills.traces.by_eval", bad).status_code == 400
    assert engine.ops == []
