"""The technique outcome projection forwards three ``RunMetrics`` queries (ARBORA-OPS-R059)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import evolution_metrics as metrics
from arbora.api.ops.registry_factory import get_registry
from arbora.pin_lag import medulla as pin_lag
from tests.api._lane_double import EngineDouble, context

OP = metrics.operations()[0]
QUERY = {"technique": "rerank", "arm": "a", "config_digest": "c" * 8}


def _answer(params: Any) -> dict[str, Any]:
    query = params["request"]["query"]
    if query == "score":  # no solved run reports a score
        return {"query": query, "groups": [], "abstained": "NO_MEASUREMENTS"}
    value = {"latency": 120.0, "cost": 0.25}[query]
    return {
        "query": query,
        "groups": [{"key": "all", "value": value, "trace_count": 3}],
    }


def _engine_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"RunMetrics"}
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


@pytest.mark.spec("ARBORA-OPS-R059.1")
def test_models_name_the_three_filters_and_a_window() -> None:
    parsed = metrics.EvolutionMetricsQuery(**QUERY)
    assert parsed.model_dump(exclude_none=True) == {**QUERY, "schema_version": 1}
    for name in QUERY:  # the three filters together are one technique measure
        with pytest.raises(ValidationError):
            metrics.EvolutionMetricsQuery(
                **{k: v for k, v in QUERY.items() if k != name}
            )
    with pytest.raises(ValidationError):
        metrics.EvolutionMetricsQuery(**QUERY, window_start_ms=5, window_end_ms=5)
    with pytest.raises(ValidationError):  # the retired proposal-join identity
        metrics.EvolutionMetricsQuery(**QUERY, proposal_id="p1")


@pytest.mark.spec("ARBORA-OPS-R059.2")
@pytest.mark.asyncio
async def test_projection_forwards_caller_scope_and_keeps_abstention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = EngineDouble(RunMetrics=_answer)
    answer = await metrics.handle_evolution_metrics(
        context(client, monkeypatch), QUERY, OP
    )
    value = answer["value"]
    assert (value["technique"], value["arm"]) == ("rerank", "a")
    assert list(value["measures"]) == ["latency", "cost", "score"]
    assert value["measures"]["latency"]["groups"][0]["value"] == 120.0
    # No solved run reports a score: the answer abstains, never a zero.
    assert value["measures"]["score"] == {
        "query": "score",
        "groups": [],
        "abstained": "NO_MEASUREMENTS",
    }
    assert [s[0] for s in client.sent] == ["RunMetrics"] * 3
    for _method, sent, graph, _key in client.sent:
        assert graph == "tenant-content"
        assert sent["request"]["tenant"] == "tenant-a"
        assert {k: sent["request"][k] for k in QUERY} == QUERY
    assert client.claims == [{"principal": "alice"}] * 3
    assert OP.scopes == {"loops:read"} and OP.effect.name == "READ"
    assert "evolution.proposals.metrics" in {op.id for op in get_registry()}


@pytest.mark.spec("ARBORA-OPS-R059.3")
@pytest.mark.asyncio
async def test_denied_or_incomplete_requests_read_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla import EngineResponseError

    # The pinned engine lacks the method: UNAVAILABLE naming it, zero engine calls.
    client = EngineDouble(RunMetrics=_answer)
    with pytest.raises(OperationRefused) as refused:
        await metrics.handle_evolution_metrics(context(client, monkeypatch), QUERY, OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "RunMetrics" and client.sent == []
    # An incomplete identity never reaches the handler: the model refuses it.
    with pytest.raises(ValidationError):
        metrics.EvolutionMetricsQuery(**{**QUERY, "config_digest": ""})
    # Engine refusals keep their code and disclose no measure.
    _engine_serves(monkeypatch)
    engine = EngineDouble(RunMetrics=EngineResponseError("ACCESS_DENIED", "no"))
    with pytest.raises(EngineRefusal) as engine_refused:
        await metrics.handle_evolution_metrics(context(engine, monkeypatch), QUERY, OP)
    assert engine_refused.value.code == "ACCESS_DENIED" and len(engine.sent) == 1


@pytest.mark.spec("ARBORA-OPS-R059", "ARBORA-OPS-R059.4")
@pytest.mark.asyncio
async def test_dashboard_joins_proposal_to_trace_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = EngineDouble(RunMetrics=_answer)
    answer = await metrics.handle_evolution_metrics(
        context(client, monkeypatch),
        {**QUERY, "window_start_ms": 1, "window_end_ms": 9},
        OP,
    )
    assert answer["value"]["measures"]["cost"]["groups"][0]["trace_count"] == 3
    assert client.sent[0][1]["request"]["window"] == {"start_ms": 1, "end_ms": 9}
