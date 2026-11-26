"""Quality, profile and column lineage operations (ARBORA-OPS-R856)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import require_scopes
from arbora.api.ops import governance
from arbora.api.registry import Effect
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble, context

_OPS = {op.id: op for op in governance.operations()}
_QUALITY = (
    "quality.rule.put",
    "quality.evaluate",
    "quality.scores",
    "profile.run",
    "lineage.columns",
)
_RULE = {
    "id": "r1",
    "target": "table:orders",
    "dimension": "completeness",
    "expression": "not_null(id)",
    "threshold": 0.99,
    "severity": "error",
}


@pytest.mark.spec("ARBORA-OPS-R856.1")
def test_registry_lists_quality_operations() -> None:
    for op_id in _QUALITY:
        assert op_id in _OPS, op_id
    assert _OPS["quality.rule.put"].scopes == frozenset({"kg:write"})
    for op_id in ("quality.evaluate", "profile.run"):
        assert _OPS[op_id].effect is Effect.WRITE
        assert op_id in governance.NATIVE_IDEMPOTENCY
    for op_id in ("quality.scores", "lineage.columns"):
        assert _OPS[op_id].effect is Effect.READ
        assert _OPS[op_id].scopes == frozenset({"kg:read"})


@pytest.mark.spec("ARBORA-OPS-R856.1")
async def test_a_pinned_engine_without_the_method_refuses_naming_it() -> None:
    from medulla.generated import METHOD_IDS, models

    for handler, params, missing in (
        ("handle_quality_rule_put", _RULE, "QualityRulePut"),
        ("handle_quality_scores", {"target": "t"}, "QualityScores"),
        ("handle_lineage_columns", {"column": "c"}, "ColumnLineage"),
        ("handle_quality_evaluate", {"target": "t"}, "JobKindQualityEvaluate"),
        ("handle_profile_run", {"dataset": "d"}, "JobKindDatasetProfile"),
    ):
        if missing in METHOD_IDS or hasattr(models, missing):
            continue
        op_id = next(
            i for i, op in _OPS.items() if op.binding.handler.endswith(handler)
        )
        with pytest.raises(OperationRefused) as refused:
            await getattr(governance, handler)(context(), params, _OPS[op_id])
        assert refused.value.code == "UNAVAILABLE"
        assert refused.value.details["missing"] == missing


@pytest.mark.spec("ARBORA-OPS-R856", "ARBORA-OPS-R856.2")
async def test_quality_incident_served(monkeypatch: pytest.MonkeyPatch) -> None:
    from medulla.generated import models

    for name in ("JobKindQualityEvaluate", "JobKindDatasetProfile"):
        monkeypatch.setattr(models, name, object, raising=False)

    async def graph(_context: Any) -> str:
        return "tenant-content"

    monkeypatch.setattr(governance, "caller_dispatch_graph", graph)
    engine = EngineDouble(monkeypatch)
    state = {"evaluations": 0, "incidents": 0, "arbora_incident_writes": 0}
    rules: dict[str, Any] = {}

    def evaluate(p: dict[str, Any]) -> Any:
        kind = p["op"]["Submit"]["kind"]
        if "QualityEvaluate" in kind:
            state["evaluations"] += 1
            if state["evaluations"] >= 3 and not state["incidents"]:
                state["incidents"] = 1
        return {"job_id": f"job-{state['evaluations']}"}

    engine.serve(
        "QualityRulePut", lambda p: rules.setdefault(p["rule"]["id"], p["rule"])
    )
    engine.serve("AnalyticsJob", evaluate)
    engine.serve(
        "QualityScores",
        lambda p: {
            "evaluations": state["evaluations"],
            "incidents": [{"open": True}] * state["incidents"],
        },
    )
    engine.serve("ColumnLineage", lambda p: {"columns": ["orders.id"]})
    engine.install(pin_lag)
    ctx = context()

    await governance.handle_quality_rule_put(ctx, _RULE, _OPS["quality.rule.put"])
    for _ in range(3):
        started = await governance.handle_quality_evaluate(
            ctx, {"target": "table:orders"}, _OPS["quality.evaluate"]
        )
    profiled = await governance.handle_profile_run(
        ctx, {"dataset": "orders", "columns": ["id"]}, _OPS["profile.run"]
    )
    scores = await governance.handle_quality_scores(
        ctx, {"target": "table:orders", "limit": 10}, _OPS["quality.scores"]
    )
    lineage = await governance.handle_lineage_columns(
        ctx, {"column": "orders.id", "direction": "downstream"}, _OPS["lineage.columns"]
    )

    assert started["value"]["job_id"] == "job-3"
    assert profiled["value"]["job_id"] == "job-3"
    assert scores["value"]["evaluations"] == 3
    assert len(scores["value"]["incidents"]) == 1
    assert lineage["value"]["columns"] == ["orders.id"]
    assert state["arbora_incident_writes"] == 0
    # The engine only: no node or incident write is sent by this module.
    assert {m for m, _, _ in engine.sent} == {
        "QualityRulePut",
        "AnalyticsJob",
        "QualityScores",
        "ColumnLineage",
    }
    submitted = [p["op"]["Submit"] for m, p, _ in engine.sent if m == "AnalyticsJob"]
    assert submitted[0]["actor"] == "principal:one"
    assert submitted[0]["kind"]["QualityEvaluate"]["tenant"] == "tenant:one"
    assert "DatasetProfile" in submitted[-1]["kind"]
    caller = SimpleNamespace(effective_scopes=frozenset({"kg:read"}))
    assert require_scopes(_OPS["quality.evaluate"], caller).code == "SCOPE_REQUIRED"
